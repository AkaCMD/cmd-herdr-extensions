import { View, div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { agents, focused, theme, exec, hc, appHome, layout, run, props, notify, nextCommand, shared, setShared, watchFolder } from "herdr";

const labels = { pending: "待处理", in_progress: "进行中", completed: "已完成", blocked: "受阻" };
const statusNames = { working: "工作中", blocked: "等待你回复", idle: "空闲", done: "本轮结束" };
const todoTools = new Map([
  ["dsh-tui", () => "用 todo_write 写入当前会话的任务清单。"],
  ["pi", () => "用 todo 工具查看并更新当前会话的任务清单。"],
  ["claude-code", () => `用 TodoWrite 或 TaskCreate/TaskUpdate 写入任务。没有这些工具时，用临时 Markdown 清单和 ${JSON.stringify(appHome() + "/mods/agent-todos/md_checklist_sync.py")}，以 --session 传入已确认的当前 Claude 会话 ID。无法确认会话 ID 就询问我。`],
  ["codex", request => `用 update_plan 写入计划。没有该工具时，通过 stdin 把 JSON ${JSON.stringify({ plan: [{ step: "步骤", status: "pending" }], ...(request ? { request } : {}) })} 传给 ${JSON.stringify(appHome() + "/mods/agent-todos/codex_plan.py")}，并传入 --app-home ${JSON.stringify(appHome())}。后续状态更新沿用 request 标识。用当前会话的环境变量关联，不要猜测会话 ID。`],
  ["opencode", () => "用 todowrite 工具写入当前会话的任务清单。"],
]);
// Some agents (e.g. OpenCode) don't report an OSC 7501 `app`; fall back to their program key.
const agentKind = a => a?.app || a?.program?.key || "";
// Agents prefix the terminal title with a live status glyph; show the readable part.
const plainTitle = s => (s || "").replace(/^[\u2800-\u28ff●○◉✓✔✗✕⏺⏵⏸\s]+/u, "");

function hasTodoToolbar(node) {
  if (!node || node.hidden) return false;
  return node.content?.mod === "tabs-session" || (node.children ?? []).some(hasTodoToolbar);
}

export default class AgentTodos extends View {
  init(_props, cx) {
    this.data = {}; this.selected = {}; this.showDone = true; this.busy = false;
    this.sending = new Set();
    this.watchers = new Set(); this.watchFiles = [];
    cx.spawn(async cx => {
      for (;;) {
        if (await nextCommand() === "generate") this.generate(cx);
      }
    });
    this.refresh(cx);
  }
  target() {
    const f = focused(); const list = agents();
    return list.find(a => a.address === f?.terminal) || list.find(a => a.focused) || list.find(a => a.address === f?.agent && a.tab === f?.tab) || null;
  }
  generationUnavailable(a) {
    if (!a || a.address.includes("/") || !todoTools.has(agentKind(a))) return "请选中支持 Todo 的本机代理";
    if (a.status === "blocked") return "代理正在等待回复，请先处理终端中的提示";
    if (this.sending.has(a.address)) return "正在暂停当前轮并发送 Todo 请求";
    return "";
  }
  generate(cx, address) {
    const a = this.target();
    if (this.generationUnavailable(a) || (address && address !== a.address)) return;
    const prompt = [
      "[herdr-todo-request]",
      "请根据当前对话中最近一项明确的工作生成 Todo 清单。保留已有任务和完成状态，只补充缺少的步骤。",
      "任务描述或目标已经改变时，按最新用户要求重写清单，移除已不适用的步骤；仅保留仍适用的完成状态。执行每一步时及时更新进行中和完成状态。",
      todoTools.get(agentKind(a))(this.data?.request),
      "这是 Todo 面板插入的临时请求，不取代原始用户任务。请立即执行生成或更新清单，不要停在计划或请求再次确认。",
      "清单写入后，立即继续本次被打断或尚未完成的原始用户消息，从已完成的进度接着做，沿用原有范围、授权和验证要求；不要重复已经完成的操作，不要因生成清单就结束本轮。",
      "如果原始任务已经完成，只更新清单并简短报告；没有明确任务时才询问我要规划什么，不要凭空生成。",
    ].join(" ");
    this.sendGeneration(cx, a, prompt);
  }
  get auto() { return shared("agent-todos.auto") !== false; }
  set auto(value) { setShared("agent-todos.auto", value); }
  toggleAuto(cx) {
    this.auto = !this.auto;
    cx.notify();
    if (this.auto) this.refresh(cx);
  }
  async generationCall(args) {
    const result = await hc([args[0], "--json", ...args.slice(1)]);
    let reply;
    try { reply = JSON.parse(result.stdout || result.stderr || "{}"); } catch (_) { /* Report the CLI error below. */ }
    if (result.code !== 0 || !reply) {
      throw new Error(reply?.error?.message || result.stderr?.trim() || "Herdr 未返回有效结果");
    }
    return reply;
  }
  async generationTerminal(a) {
    const terminal = await this.generationCall(["get", a.address]);
    if (terminal.state !== "live" || terminal.status?.app !== a.app) {
      throw new Error("原代理已退出或切换，Todo 请求已取消。");
    }
    if (terminal.status.state === "blocked") {
      throw new Error("代理正在等待回复，请先处理终端中的提示，再生成 Todo。");
    }
    return terminal;
  }
  sendGeneration(cx, a, prompt) {
    this.sending.add(a.address);
    cx.notify();
    cx.spawn(async cx => {
      try {
        let terminal = await this.generationTerminal(a);
        let stopSent = false;
        if (terminal.status.state === "working") {
          const stopped = await this.generationCall(["interrupt", "--timeout", "10", a.address]);
          if (stopped.key !== "esc" || !["stopped", "still_working"].includes(stopped.outcome)) {
            throw new Error("未能向原代理发送停止键，Todo 请求未发送；请查看终端。");
          }
          stopSent = true;
          terminal = await this.generationTerminal(a);
        }
        if (!["idle", "done"].includes(terminal.status.state) && !(stopSent && terminal.status.state === "working")) {
          throw new Error("代理尚未准备好接收 Todo 请求，请查看终端。");
        }
        // hc's exact --if idle predicate excludes done, even when the app shows it as idle.
        // Codex can keep reporting working for a background tool after esc has opened its prompt.
        // Guard the observed word; every send still refuses blocked dialogs (no --force).
        const guard = terminal.status.state === "done" ? [] : ["--if", terminal.status.state];
        const reply = await this.generationCall(["send", ...guard, a.address, prompt]);
        if (!reply.started) {
          throw new Error(reply.error?.message || "代理未开始处理请求，请查看终端；不要盲目重发。");
        }
        notify({ title: "已请求生成 Todo", body: "代理将先更新清单，再继续原任务；面板会自动同步。" });
      } catch (e) {
        notify({ title: "Todo 请求发送失败", body: e.message || String(e), level: "error" });
      } finally {
        // The terminal can still report idle briefly after accepting the prompt.
        cx.timer.after(1500, cx => { this.sending.delete(a.address); cx.notify(); });
      }
    });
  }
  async collect(a) {
    const args = [appHome() + "/mods/agent-todos/collector.py", a.address, agentKind(a), this.selected[a.address] || "", a.title || ""];
    const r = await exec(props()?.python || "python", args, { timeout: 15000 });
    if (r.code !== 0) throw new Error(r.stderr || "采集器执行失败");
    return JSON.parse(r.stdout);
  }
  startWatching(cx, files) {
    this.watchFiles = files;
    const directories = new Set(files.map(file => file.replace(/\\/g, "/").replace(/\/[^/]+$/, "")));
    for (const directory of directories) {
      if (this.watchers.has(directory)) continue;
      this.watchers.add(directory);
      cx.spawn(async cx => {
        try {
          for (;;) {
            const changed = await watchFolder(directory);
            this.watchError = null;
            const activeFiles = this.watchFiles.map(file => file.replace(/\\/g, "/"));
            if (!activeFiles.some(file => file.startsWith(directory + "/"))) break;
            if (this.auto && changed.some(path => {
              const normalized = path.replace(/\\/g, "/");
              return normalized === directory || activeFiles.some(file => file === normalized || (file.endsWith("/*") && normalized.startsWith(file.slice(0, -1))));
            })) {
              this.refresh(cx);
            }
          }
        } catch (error) {
          this.watchError = String(error); cx.notify();
          cx.timer.after(10000, cx => {
            if (this.auto) { this.startWatching(cx, this.watchFiles); this.refresh(cx); }
          });
        } finally { this.watchers.delete(directory); }
      });
      // Re-read after installing a watcher to cover writes between the first read and subscription.
      this.pendingRefresh = true;
    }
  }
  refresh(cx) {
    const a = this.target(); if (!a) return;
    if (this.busy) { this.pendingRefresh = true; return; }
    const key = `${a.address}:${agentKind(a)}:${this.selected[a.address] || ""}`;
    this.busy = true;
    cx.spawn(async cx => {
      try {
        const value = await this.collect(a); const packed = JSON.stringify(value);
        const current = this.target();
        if (!current || `${current.address}:${agentKind(current)}:${this.selected[current.address] || ""}` !== key || plainTitle(current.title) !== plainTitle(a.title)) return;
        this.title = plainTitle(a.title); this.seq = a.seq;
        this.startWatching(cx, value.watchFiles || []);
        if (this.key !== key || this.packed !== packed) {
          this.key = key; this.data = value; this.packed = packed; cx.notify();
        }
      } catch (e) {
        const current = this.target();
        if (!current || `${current.address}:${agentKind(current)}:${this.selected[current.address] || ""}` !== key) return;
        this.title = plainTitle(a.title); this.seq = a.seq;
        this.key = key; this.data = { error: String(e), tasks: [] }; this.packed = null; cx.notify();
        cx.timer.after(10000, cx => { if (this.auto) this.refresh(cx); });
      } finally {
        this.busy = false; cx.notify();
        if (this.pendingRefresh) { this.pendingRefresh = false; this.refresh(cx); }
      }
    });
  }
  button(id, text, action, t) {
    return div().id(id).px(7).py(4).rounded(5).text_size(11).text_color(t.muted).cursor_pointer().hover(s => s.bg(t.hover).text_color(t.foreground)).on_click(action).child(text);
  }
  render(cx) {
    const t = theme(); const a = this.target();
    const key = a ? `${a.address}:${agentKind(a)}:${this.selected[a.address] || ""}` : null;
    const d = this.key === key ? this.data : {};
    const unavailable = this.generationUnavailable(a);
    if (a && (this.key !== key || (this.auto && (this.title !== plainTitle(a.title) || this.seq !== a.seq))) && !this.busy) this.refresh(cx);
    const tasks = d.tasks || []; const done = tasks.filter(x => x.status === "completed").length;
    const ongoing = tasks.filter(x => x.status === "in_progress");
    const row = task => {
      const blocked = (task.blockedBy || []).filter(id => tasks.some(x => String(x.id) === String(id) && x.status !== "completed"));
      const color = task.status === "completed" ? t.done : task.status === "in_progress" ? t.working : blocked.length ? t.blocked : t.muted;
      const glyph = task.status === "completed" ? "✓" : task.status === "in_progress" ? "▶" : blocked.length ? "!" : "○";
      return v_flex().id(`todo-${task.id}`).w_full().flex_none().p(10).gap(4).rounded(7).bg(task.status === "in_progress" ? t.raised : t.surface)
        .child(h_flex().gap(7).child(div().flex_none().text_color(color).child(glyph)).child(div().flex_1().min_w(0).text_color(task.status === "completed" ? t.dim : t.foreground).child(task.subject)))
        .child(div().pl(19).text_size(10.5).text_color(color).child(`#${task.id} · ${labels[task.status] || task.status}${blocked.length ? " · 等待 #" + blocked.join("、#") : ""}`))
        .when(task.status === "in_progress" && !!task.activeForm, el => el.child(div().pl(19).text_size(11).text_color(t.working).child(task.activeForm)))
        .when(!!task.description, el => el.tooltip(task.description));
    };
    return v_flex().id("todo-root").on_size_change((size, cx) => { if (this.width !== size.width) { this.width = size.width; cx.notify(); } }).size_full().min_h(0).bg(t.chrome).rounded(9).text_size(12)
      .child(h_flex().h(42).px(12).flex_none().items_center().justify_between()
        .child(div().font_semibold().text_color(t.foreground).child("Agent 进度"))
        .child(h_flex().items_center().gap(4)
          .child(div().id("todo-auto").px(7).py(4).rounded(5).text_size(11)
            .bg(this.auto ? t.accent : t.raised).text_color(this.auto ? t.accent_foreground : t.muted)
            .cursor_pointer().hover(s => s.bg(this.auto ? t.accent : t.hover))
            .tooltip(this.auto ? "文件事件触发原生 Todo / Plan 同步（点击暂停）" : "开启原生 Todo / Plan 文件事件同步")
            .on_click((_e, cx) => this.toggleAuto(cx))
            .child(this.auto ? "自动同步 ✓" : "自动同步"))
          .child(div().id("todo-generate").px(7).py(4).rounded(5).text_size(11).bg(t.raised)
            .text_color(unavailable ? t.dim : t.foreground).tooltip(unavailable || "立即生成 Todo；工作中的代理先暂停，生成后继续原任务")
            .when(!unavailable, el => el.cursor_pointer().hover(s => s.bg(t.hover)).on_click((_e, cx) => this.generate(cx, a.address)))
            .child(this.sending.has(a?.address) ? "处理中…" : "生成 Todo"))
          .when(!hasTodoToolbar(layout()), el => el.child(this.button("todo-close", "隐藏",
            () => run("layout.hide", { id: "agent-todos" }), t)))))
      .child(v_flex().px(12).pb(12).gap(6).flex_none()
        .child(div().text_color(t.foreground).child(a ? `${a.program?.name || a.app} · ${a.address}` : "请选中一个代理终端"))
        .when(!!a, el => el.child(div().text_size(11).text_color(t[a.status] || t.dim).child(statusNames[a.status] || a.status)))
        .when(!!a?.title, el => el.child(div().text_size(11).text_color(t.dim).truncate().tooltip(a.title).child(plainTitle(a.title))))
        .when(!!d.source, el => el.child(div().text_size(10).text_color(t.dim).child(d.source)))
        .when(tasks.length > 0, el => el
          .child(h_flex().justify_between().child(div().text_color(t.foreground).child(`Todo ${done}/${tasks.length}`)).child(div().text_color(t.dim).child(`${Math.round(done / tasks.length * 100)}%`)))
          .child(div().h(4).w_full().rounded(3).bg(t.border).child(div().h(4).w(Math.max(0, ((this.width || 320) - 24) * done / tasks.length)).max_w_full().rounded(3).bg(t.done)))
          .when(ongoing.length > 0, el => el.child(div().text_size(11).text_color(t.working).child("正在：" + ongoing.map(x => x.activeForm || x.subject).join(" · "))))))
      .child(v_flex().id("todo-scroll").flex_1().min_h(0).px(10).gap(6).overflow_y_scrollbar()
        .when(!!d.error, el => el.child(div().text_color(t.blocked).child("读取失败：" + d.error)))
        .when(!!d.message, el => el.child(div().p(6).text_color(t.muted).child(d.message)))
        .when(!!d.session, el => el.child(div().text_size(10).text_color(t.dim).truncate().tooltip(d.session).child("会话 " + d.session)))
        .when((d.choices || []).length > 1, el => el.children(d.choices.map((c, i) => this.button(`session-${i}`, c.name, (_e, cx) => { this.selected[a.address] = c.path; this.key = null; this.refresh(cx); cx.notify(); }, t))))
        .when(!!a && !d.error && !d.message && tasks.length === 0, el => el.child(div().p(6).text_color(t.dim).child(this.key === key ? "此会话暂无任务。可点击「生成 Todo」。" : "正在关联会话…")))
        .children(tasks.filter(x => x.status !== "completed").map(row))
        .when(done > 0, el => el.child(this.button("todo-done", `${this.showDone ? "▾" : "▸"} 已完成 (${done})`, (_e, cx) => { this.showDone = !this.showDone; cx.notify(); }, t)))
        .when(this.showDone, el => el.children(tasks.filter(x => x.status === "completed").map(row))))
      .child(h_flex().px(10).py(7).flex_none().justify_between()
        .child(div().text_size(10).text_color(t.dim).child(this.watchError ? "监听失败 · 10 秒后重试" : "本机 · 文件事件同步"))
        .child(this.button("todo-refresh", "刷新", (_e, cx) => this.refresh(cx), t)));
  }
}
