// Exercise the real mod methods with synthetic Herdr data; no user terminal is touched.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("./main.js", import.meta.url), "utf8")
  .replace(/^import .*;\r?$/gm, "")
  .replace("export default class AgentTodos", "class AgentTodos") + "\nglobalThis.AgentTodos = AgentTodos;";
const ok = value => ({ code: 0, stdout: JSON.stringify(value), stderr: "" });

function fixture(states = ["working", "idle"], overrides = {}) {
  const a = { address: "t-test", app: "codex", status: states[0] };
  const calls = [], notices = [], tasks = [], timers = [], sharedStore = {};
  const watchWaiters = [];
  let reads = 0;
  const context = vm.createContext({
    View: class {}, agents: () => [a], focused: () => ({ terminal: a.address }),
    appHome: () => "/synthetic/herdr", notify: value => notices.push(value),
    watchFolder: path => new Promise(resolve => watchWaiters.push({ path, resolve })),
    nextCommand: () => new Promise(() => {}),
    shared: key => sharedStore[key],
    setShared: (key, value) => { sharedStore[key] = value; return null; },
    hc: async args => {
      calls.push(args);
      if (overrides[args[0]]) return overrides[args[0]](args);
      if (args[0] === "get") return ok({ state: "live", status: { app: a.app, state: states[Math.min(reads++, states.length - 1)] } });
      if (args[0] === "interrupt") return ok({ outcome: "stopped", key: "esc" });
      if (args[0] === "send") return ok({ started: true });
      throw new Error("Unexpected command: " + args[0]);
    },
  });
  vm.runInContext(source, context);
  const mod = new context.AgentTodos();
  mod.sending = new Set();
  mod.collect = async () => mod.data || {};
  const cx = { notify() {}, spawn(fn) { tasks.push(fn(cx)); }, timer: { after(_ms, fn) { timers.push(fn); } } };
  return { a, mod, cx, calls, notices, tasks, timers, sharedStore, watchWaiters, finish: () => Promise.all(tasks) };
}

test("working agent is interrupted immediately, generates Todo and resumes original work", async () => {
  const f = fixture();
  f.mod.generate(f.cx);
  await f.finish();
  assert.deepEqual(f.calls.map(c => c[0]), ["get", "interrupt", "get", "send"]);
  const send = f.calls.at(-1);
  assert.equal(send.at(-2), f.a.address);
  assert.ok(send.includes("--if") && send.includes("idle"));
  assert.match(send.at(-1), /保留已有任务和完成状态/);
  assert.match(send.at(-1), /立即继续.*被打断.*原始用户消息/);
  assert.doesNotMatch(send.at(-1), /先不要执行这些任务/);
  assert.ok(!f.calls.some(c => c.includes("--force")));
});

test("idle agent gets generation request without interruption", async () => {
  const f = fixture(["idle"]);
  f.mod.generate(f.cx);
  await f.finish();
  assert.deepEqual(f.calls.map(c => c[0]), ["get", "send"]);
});

test("finished turns accept input without hc's incompatible exact idle predicate", async () => {
  const f = fixture(["done"]);
  f.mod.generate(f.cx);
  await f.finish();
  assert.deepEqual(f.calls.map(c => c[0]), ["get", "send"]);
  assert.ok(!f.calls.at(-1).includes("--if"));
  assert.ok(!f.calls.at(-1).includes("--force"));
});

test("duplicate clicks and stale button targets cannot interrupt twice or steer another agent", async () => {
  const f = fixture();
  f.mod.generate(f.cx, "some-other-terminal");
  assert.equal(f.calls.length, 0);
  f.mod.generate(f.cx);
  f.mod.generate(f.cx);
  await f.finish();
  assert.equal(f.calls.filter(c => c[0] === "interrupt").length, 1);
  assert.equal(f.calls.filter(c => c[0] === "send").length, 1);
});

test("blocked agents and vanished or replaced targets never receive input", async () => {
  for (const terminal of [
    { state: "live", status: { app: "codex", state: "blocked" } },
    { state: "finished", status: { app: "codex", state: "idle" } },
    { state: "live", status: { app: "claude-code", state: "idle" } },
  ]) {
    const f = fixture(["working"], { get: () => ok(terminal) });
    f.mod.generate(f.cx);
    await f.finish();
    assert.ok(!f.calls.some(c => ["send", "interrupt"].includes(c[0])));
    assert.ok(f.notices.some(n => n.level === "error"));
  }
});

test("exited or unknown interruption outcomes never send the Todo prompt", async () => {
  for (const outcome of ["exited", "unknown"]) {
    const f = fixture(["working"], { interrupt: () => ok({ outcome, key: "esc" }) });
    f.mod.generate(f.cx);
    await f.finish();
    assert.ok(!f.calls.some(c => c[0] === "send"));
    assert.ok(f.notices.some(n => n.level === "error"));
  }
});

test("Codex background tools do not prevent Todo insertion after the stop key was sent", async () => {
  const f = fixture(["working"], { interrupt: () => ok({ outcome: "still_working", key: "esc" }) });
  f.mod.generate(f.cx);
  await f.finish();
  assert.deepEqual(f.calls.map(c => c[0]), ["get", "interrupt", "get", "send"]);
  assert.ok(f.calls.at(-1).includes("--if") && f.calls.at(-1).includes("working"));
  assert.ok(!f.calls.at(-1).includes("--force"));
});

test("state changes after interruption cannot type into a permission dialog", async () => {
  const f = fixture(["working", "blocked"]);
  f.mod.generate(f.cx);
  await f.finish();
  assert.ok(!f.calls.some(c => c[0] === "send"));
  assert.ok(f.notices.some(n => n.level === "error"));
});

test("a rejected send is not blindly retried or announced as delivered", async () => {
  const f = fixture(["idle"], { send: () => ({ code: 10, stdout: "", stderr: '{"error":{"message":"status changed"}}' }) });
  f.mod.generate(f.cx);
  await f.finish();
  assert.equal(f.calls.filter(c => c[0] === "send").length, 1);
  assert.ok(f.notices.some(n => n.level === "error"));
  assert.ok(!f.notices.some(n => n.title === "已请求生成 Todo"));
});

test("agents without an OSC app fall back to their program key (OpenCode)", async () => {
  const f = fixture(["idle"]);
  f.a.app = null;
  f.a.program = { key: "opencode" };
  f.mod.generate(f.cx);
  await f.finish();
  assert.deepEqual(f.calls.map(c => c[0]), ["get", "send"]);
  assert.match(f.calls.at(-1).at(-1), /todowrite/);
});

test("automatic synchronization refreshes snapshots without sending terminal input", async () => {
  const f = fixture(["working"]);
  f.mod.selected = {};
  f.mod.data = { session: "s1", tasks: [] };
  f.mod.collect = async () => ({ session: "s1", request: "changed", tasks: [{ id: 1, subject: "new task", status: "in_progress" }] });
  f.mod.refresh(f.cx);
  await f.finish();
  assert.equal(f.mod.data.tasks[0].subject, "new task");
  assert.equal(f.calls.length, 0);
  f.mod.refresh(f.cx);
  await f.finish();
  assert.equal(f.calls.length, 0);
});

test("sync is enabled by default and the saved opt-out survives another view", () => {
  const f = fixture();
  assert.equal(f.mod.auto, true);
  f.sharedStore["agent-todos.auto"] = false;
  assert.equal(f.mod.auto, false);
});

test("stale asynchronous collector results are discarded after a session selection changes", async () => {
  const f = fixture();
  f.mod.selected = {};
  f.mod.data = { session: "current" };
  let complete;
  f.mod.collect = () => new Promise(resolve => { complete = resolve; });
  f.mod.refresh(f.cx);
  f.mod.selected[f.a.address] = "new-choice";
  complete({ tasks: [], session: "old" });
  await f.finish();
  assert.equal(f.mod.data.session, "current");
  assert.equal(f.calls.length, 0);
});

const settle = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

test("file events drive snapshot replacement while unrelated cache writes are ignored", async () => {
  const f = fixture();
  f.mod.selected = {}; f.mod.watchers = new Set(); f.mod.watchFiles = [];
  let reads = 0;
  f.mod.collect = async () => ({ session: "s1", watchFiles: ["/synthetic/native/session.jsonl"], tasks: [{ id: 1, subject: `snapshot-${++reads}`, status: "pending" }] });
  f.mod.refresh(f.cx);
  await settle();
  assert.equal(reads, 2);
  f.watchWaiters.shift().resolve(["/synthetic/native/cache.tmp"]);
  await settle();
  assert.equal(reads, 2);
  f.watchWaiters.shift().resolve(["/synthetic/native/session.jsonl"]);
  await settle();
  assert.equal(reads, 3);
  assert.equal(f.mod.data.tasks[0].subject, "snapshot-3");
  assert.equal(f.calls.length, 0);
  f.mod.watchFiles = [];
  f.watchWaiters.shift().resolve(["/synthetic/native/session.jsonl"]);
  await f.finish();
});

test("initialization uses event subscriptions without a periodic collection timer", async () => {
  const f = fixture();
  f.cx.timer.every = () => assert.fail("periodic polling must not be scheduled");
  f.mod.init({}, f.cx);
  await settle();
  assert.equal(f.calls.length, 0);
});
