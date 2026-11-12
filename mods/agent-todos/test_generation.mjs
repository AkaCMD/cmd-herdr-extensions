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
  let reads = 0;
  const context = vm.createContext({
    View: class {}, agents: () => [a], focused: () => ({ terminal: a.address }),
    appHome: () => "/synthetic/herdr", notify: value => notices.push(value),
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
  const cx = { notify() {}, spawn(fn) { tasks.push(fn(cx)); }, timer: { after(_ms, fn) { timers.push(fn); } } };
  return { a, mod, cx, calls, notices, tasks, timers, sharedStore, finish: () => Promise.all(tasks) };
}

// Auto mode state as init leaves it, with a ready, empty conversation.
function autoFixture(states = ["idle"], data = { tasks: [], session: "s1" }) {
  const f = fixture(states);
  f.mod.auto = true;
  f.mod.generated = new Set();
  f.mod.selected = {};
  f.mod.key = `${f.a.address}:${f.a.app}:`;
  f.mod.data = data;
  return f;
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

test("auto mode generates once for an empty conversation, without a manual click", async () => {
  const f = autoFixture();
  f.mod.maybeAuto(f.cx);
  f.mod.maybeAuto(f.cx);
  await f.finish();
  assert.equal(f.calls.filter(c => c[0] === "send").length, 1);
  assert.equal(f.calls.filter(c => c[0] === "interrupt").length, 0);
});

test("auto mode leaves conversations that already have tasks alone", async () => {
  const f = autoFixture(["idle"], { tasks: [{ id: 1, subject: "x", status: "pending" }], session: "s1" });
  f.mod.maybeAuto(f.cx);
  await f.finish();
  assert.ok(!f.calls.some(c => ["get", "send"].includes(c[0])));
});

test("auto mode skips blocked agents without marking them, then retries", async () => {
  const f = autoFixture(["idle"], { tasks: [], session: "s1" });
  f.a.status = "blocked";
  f.mod.maybeAuto(f.cx);
  await f.finish();
  assert.ok(!f.calls.some(c => ["get", "send"].includes(c[0])));
  f.a.status = "idle";
  f.mod.maybeAuto(f.cx);
  await f.finish();
  assert.equal(f.calls.filter(c => c[0] === "send").length, 1);
});

test("auto mode ignores unsupported agents and collector setup hints", async () => {
  for (const data of [{ message: "需要 /reload" }, { error: "boom" }, { tasks: [], message: "多个会话" }]) {
    const f = autoFixture(["idle"], { tasks: [], ...data });
    f.mod.maybeAuto(f.cx);
    await f.finish();
    assert.ok(!f.calls.some(c => ["get", "send"].includes(c[0])));
  }
  const g = autoFixture(["idle"], { tasks: [], session: "s1" });
  g.a.app = "nvim";
  g.mod.maybeAuto(g.cx);
  await g.finish();
  assert.ok(!g.calls.some(c => ["get", "send"].includes(c[0])));
});

test("auto toggle flips and persists its state", () => {
  const f = fixture(["blocked"]);
  f.mod.auto = false;
  f.mod.generated = new Set();
  f.mod.selected = {};
  f.mod.data = {};
  f.mod.key = `${f.a.address}:${f.a.app}:`;
  f.mod.toggleAuto(f.cx);
  assert.equal(f.mod.auto, true);
  assert.equal(f.sharedStore["agent-todos.auto"], true);
  f.mod.toggleAuto(f.cx);
  assert.equal(f.mod.auto, false);
  assert.equal(f.sharedStore["agent-todos.auto"], false);
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

test("auto mode generates once per session for an agent without an OSC app", async () => {
  const f = fixture(["idle"]);
  f.a.app = null;
  f.a.program = { key: "opencode" };
  f.mod.auto = true;
  f.mod.generated = new Set();
  f.mod.selected = {};
  f.mod.key = "t-test:opencode:";
  f.mod.data = {};
  f.mod.maybeAuto(f.cx);
  await f.finish();
  assert.deepEqual(f.calls.map(c => c[0]), ["get", "send"]);
  assert.match(f.calls.at(-1).at(-1), /todowrite/);
  // A repeated sync in the same session never sends twice.
  f.mod.sending.clear();
  f.mod.maybeAuto(f.cx);
  await f.finish();
  assert.equal(f.calls.filter(c => c[0] === "send").length, 1);
});
