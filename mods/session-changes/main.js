import { View, div } from "gpui-kit";
import { v_flex, h_flex, Scrollbar, ScrollHandle } from "gpui-base";
import { agents, focused, exec, hc, appHome, theme, settings, openOverlay, closeOverlay, props, nextCommand } from "herdr";

// The agent this panel follows: the focused agent pane, else the last one followed in
// this tab, else the tab's first agent.
function pickAgent(last) {
  const f = focused(), all = agents();
  if (!f) return null;
  const here = all.filter((a) => a.tab === f.tab);
  return here.find((a) => a.address === f.terminal || a.address === f.agent)
    ?? here.find((a) => a.address === last)
    ?? here[0] ?? null;
}

const statusColor = (s, t) => s === "A" ? t.ansi2 : s === "D" ? t.ansi1 : t.blocked;

export default class SessionChanges extends View {
  init(_props, cx) {
    _props = _props ?? props() ?? {};
    this.expanded = Boolean(_props.expanded); this.pinnedAgent = _props.agent;
    this.pinnedSession = _props.session; this.selectedSession = ""; this.sessionChoices = [];
    this.requestedPath = _props.file;
    this.files = _props.fileData ? [_props.fileData] : []; this.open = _props.file ?? null;
    this.showScripts = false; this.info = _props.info ?? null;
    this.error = null; this.address = this.expanded ? this.pinnedAgent : null; this.signature = ""; this.handle = cx.focus_handle();
    this.panelWidth = 360; this.mode = "auto"; this.wrap = false; this.context = false;
    this.scroll = ScrollHandle.new(); this.cursor = -1; this.confirmUndo = null;
    this.refresh(cx);
    // Unreal assets get saved without the transcript changing: rebuild fully every 20 s.
    this.polls = 0;
    cx.timer.every(4000, (cx) => {
      if (++this.polls % 5 === 0 && this.info?.agent !== "DeepSeek") this.signature = "";
      this.refresh(cx);
    });
    cx.spawn(async (cx) => { for (;;) {
      const command = await nextCommand();
      if (command === "expand" && !this.expanded) this.expand();
      if (command === "close" && this.expanded) closeOverlay(props().overlay);
    } });
  }

  followedAgent() {
    return this.expanded ? agents().find((a) => a.address === this.pinnedAgent) : pickAgent(this.address);
  }

  expand() {
    if (!this.open || !this.address) return;
    openOverlay({ mod: "session-changes", kind: "dialog", modal: true, frame: "none", width: 0.94, height: 0.92,
      props: { expanded: true, agent: this.address, file: this.open,
        session: this.info?.agent === "DeepSeek" ? this.info.session : undefined,
        fileData: this.files.find((f) => f.path === this.open), info: this.info } });
  }

  // Called from render too: only the spawned task may notify.
  refresh(cx) {
    if (this.loading) return;
    const agent = this.followedAgent(), address = agent?.address ?? null;
    if (address !== this.address) {
      this.address = address; this.files = []; this.open = this.expanded ? this.requestedPath : null; this.info = null;
      this.error = null; this.signature = ""; this.closed = false;
      this.selectedSession = ""; this.sessionChoices = [];
    }
    if (!address) return;
    if (address.includes("/")) { this.error = "Agents on other machines aren't supported yet"; return; }
    this.loading = true;
    cx.spawn(async (cx) => {
      try {
        // pid and folder don't change; the title does (Codex names its thread there).
        const got = await hc(["get", address, "--json"]);
        if (got.code !== 0) throw new Error(got.stderr || got.stdout);
        const term = JSON.parse(got.stdout);
        const script = `${appHome()}/mods/session-changes/session_changes.py`;
        const r = await exec("python", [script, String(term.pid), term.cwd ?? "", term.title ?? "",
          this.signature, agent.app ?? "", this.pinnedSession ?? this.selectedSession, String(Boolean(this.pinnedSession))],
          { timeout: 30000 });
        if (address !== this.address) return;
        if (r.code !== 0) throw new Error((r.stderr || r.stdout).trim().split("\n").pop());
        const out = JSON.parse(r.stdout);
        this.sessionChoices = out.choices ?? [];
        if (out.same) return;
        if (out.error) { this.error = out.error; this.files = []; this.info = null; this.signature = ""; return; }
        if (this.info?.session !== out.session) {
          this.open = this.expanded ? this.requestedPath : null;
          this.closed = false;
        }
        this.error = null; this.signature = out.signature;
        this.info = { agent: out.agent, session: out.session, name: out.name, cwd: out.cwd, title: term.title };
        this.files = out.files;
        const shown = this.visible();
        if (this.open && !out.files.some((f) => f.path === this.open)) this.open = null;
        if (!this.open && !this.closed && shown.length) this.open = shown[0].path;
      } catch (e) { if (address === this.address) this.error = String(e); }
      finally { this.loading = false; cx.notify(); }
    });
  }

  visible() { return this.showScripts ? this.files : this.files.filter((f) => !f.script); }

  pick(path, cx) {
    this.open = path;
    const index = this.stream?.findIndex((r) => r.file.path === path) ?? -1;
    if (index >= 0) this.scroll.scroll_to_item(index);
    this.closed = false;
    cx.notify();
  }

  button(id, label, action, t) {
    return div().id(id).px(7).h(24).flex().items_center().rounded(4).bg(t.raised)
      .cursor_pointer().hover((s) => s.bg(t.hover)).on_click(action).child(label);
  }

  navigate(delta, cx) {
    const points = this.stream.map((r, i) => r.row[0] === "@" ? i : -1).filter((i) => i >= 0);
    if (!points.length) return;
    this.cursor = (this.cursor + delta + points.length) % points.length;
    const index = points[this.cursor];
    this.open = this.stream[index].file.path;
    this.scroll.scroll_to_item(index); cx.notify();
  }

  undo(file, block, cx) {
    if (this.undoing) return;
    const key = `${file.path}:${block ?? "file"}`;
    if (this.confirmUndo !== key) { this.confirmUndo = key; cx.notify(); return; }
    this.confirmUndo = null; this.undoing = true;
    cx.spawn(async (cx) => {
      try {
        const result = await exec("python", [`${appHome()}/mods/session-changes/review_changes.py`,
          JSON.stringify({ review: file.review, block })], { timeout: 30000 });
        const out = JSON.parse(result.stdout);
        if (result.code !== 0 || out.error) throw new Error(out.error || result.stderr);
        this.error = null; this.signature = "";
      } catch (error) { this.error = String(error); }
      finally { this.undoing = false; this.refresh(cx); cx.notify(); }
    });
  }

  code(text, t) {
    const tokens = (text || " ").split(/("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\b(?:const|let|var|function|return|if|else|class|import|from|def|for|while|true|false|None|null|async|await|export|public|private|void|int|new)\b|\b\d+(?:\.\d+)?\b|\/\/.*$|#.*$)/g);
    return h_flex().flex_1().min_w(0).children(tokens.filter(Boolean).map((token) =>
      div().flex_none().whitespace_nowrap().text_color(/^["']/.test(token) ? t.ansi2 : /^(\/\/|#)/.test(token) ? t.dim : /^(\d|const$|let$|var$|function$|return$|if$|else$|class$|import$|from$|def$|for$|while$|true$|false$|None$|null$|async$|await$|export$|public$|private$|void$|int$|new$)/.test(token) ? t.ansi4 : t.foreground).child(token)));
  }

  diffRow([kind, number, text], t) {
    if (kind === "@") return div().w_full().px(12).h(this.rowHeight).flex_none().truncate().bg(t.raised).text_color(t.dim).child(text);
    return h_flex().w_full().h(this.rowHeight).flex_none().gap(6).when(kind === "+", (e) => e.bg(t.ansi2 + "18")).when(kind === "-", (e) => e.bg(t.ansi1 + "18"))
      .child(div().w(34).flex_none().text_right().text_color(t.dim).child(number == null ? "" : String(number)))
      .child(div().w(10).flex_none().text_color(kind === "+" ? t.ansi2 : kind === "-" ? t.ansi1 : t.dim).child(kind === " " ? "" : kind))
      .child(this.code(text, t));
  }

  compareRow(row, t) {
    if (row[0] !== "C") return this.diffRow(row, t);
    return h_flex().h(this.rowHeight).flex_none().flex_none().w_full().text_size(this.expanded ? 14 : 11)
      .child(div().w(this.locationWidth).flex_none().px(8).truncate().text_color(t.muted).tooltip(row[1]).child(row[1]))
      .child(div().w(this.valueWidth).flex_none().px(8).h_full().bg(t.ansi1 + "12").text_color(row[2] === "∅" ? t.dim : t.ansi1).truncate().tooltip(row[2]).child(row[2]))
      .child(div().flex_1().min_w(this.valueWidth).px(8).h_full().bg(t.ansi2 + "12").text_color(row[3] === "∅" ? t.dim : t.ansi2).truncate().tooltip(row[3]).child(row[3]));
  }

  render(cx) {
    if (this.followedAgent()?.address !== this.address && !this.loading) this.refresh(cx);
    const t = theme(), shown = this.expanded ? this.files.filter((f) => f.path === this.open) : this.visible(), hidden = this.expanded ? 0 : this.files.length - this.files.filter((f) => !f.script).length;
    const add = shown.reduce((n, f) => n + f.add, 0), del = shown.reduce((n, f) => n + f.del, 0);
    const file = this.files.find((f) => f.path === this.open);
    const split = this.mode === "split" || (this.mode === "auto" && this.panelWidth >= 900);
    this.rowHeight = this.expanded ? 30 : 24;
    const stream = [];
    for (const entry of shown) {
      stream.push({ file: entry, row: ["F", null, entry.rel] });
      const source = this.context && entry.full_rows ? entry.full_rows : entry.rows;
      let block = -1;
      const paired = [];
      for (let i = 0; i < source.length; i++) {
        const row = source[i];
        if (row[0] === "@") block++;
        if (split && row[0] === "-") {
          const removed = [], added = [];
          while (i < source.length && source[i][0] === "-") removed.push(source[i++]);
          while (i < source.length && source[i][0] === "+") added.push(source[i++]);
          i--;
          for (let j = 0; j < Math.max(removed.length, added.length); j++) paired.push({ row: ["S"], left: removed[j], right: added[j], block });
        } else if (split && ["+", " "].includes(row[0])) {
          paired.push({ row: ["S"], left: row[0] === " " ? row : null, right: row, block });
        } else paired.push({ row, block });
      }
      const columns = Math.max(12, Math.floor((this.panelWidth / (split ? 2 : 1) - 75) / (this.expanded ? 8.4 : 6.6)));
      for (const item of paired) {
        const texts = item.row[0] === "S" ? [item.left?.[2] || "", item.right?.[2] || ""] : [item.row[2] || ""];
        const count = this.wrap && !["@", "C"].includes(item.row[0]) ? Math.max(1, ...texts.map((text) => Math.ceil(text.length / columns))) : 1;
        for (let part = 0; part < count; part++) {
          const slice = (row) => row && [row[0], part ? null : row[1], this.wrap ? row[2].slice(part * columns, (part + 1) * columns) : row[2]];
          stream.push({ ...item, file: entry, row: item.row[0] === "S" ? item.row : slice(item.row), left: slice(item.left), right: slice(item.right) });
        }
      }
      if (entry.cut) stream.push({ file: entry, row: ["", null, "… truncated transcript diff"] });
    }
    this.stream = stream;
    const width = this.wrap ? Math.max(280, this.panelWidth - 4) : Math.max(this.panelWidth - 4, ...stream.map((r) => (Math.max(r.row[2]?.length || 0, r.left?.[2]?.length || 0, r.right?.[2]?.length || 0) * 8 + 90) * (split ? 2 : 1)));
    this.locationWidth = Math.round(Math.max(330, this.panelWidth) * 0.35);
    this.valueWidth = Math.max(120, Math.floor((this.panelWidth - this.locationWidth) / 2));
    const empty = this.error || (!this.address ? "No agent in this tab" : this.loading && !this.info ? "Reading conversation…" : this.info ? (hidden ? "Only tool scripts changed in this conversation" : "No files changed in this conversation yet") : "Reading conversation…");

    return v_flex().id("session-changes").size_full().min_h(0).rounded(10).overflow_hidden().bg(t.background).border_1().border_color(t.border).text_color(t.foreground).text_size(13).track_focus(this.handle)
      .on_key_down((event, cx) => { if (event.key === "f7") this.navigate(event.modifiers.shift ? -1 : 1, cx); })
      .on_size_change((size, cx) => { if (Math.abs(size.width - this.panelWidth) > 1) { this.panelWidth = size.width; cx.notify(); } })
      .child(h_flex().h(42).flex_none().px(14).gap(8).items_center().border_b(1).border_color(t.border)
        .child(div().font_semibold().child(this.expanded ? "文件对比" : "Session"))
        .when(!this.expanded, (e) => e.child(div().px(5).py(1).rounded(4).border_1().border_color(t.border).text_size(10).text_color(t.muted).child(`${shown.length} files`)))
        .when(this.expanded, (e) => e.child(div().flex_1().min_w(0).truncate().tooltip(file?.source_path || file?.path || "").child(file?.rel || "Reading…")))
        .child(div().flex_1()).child(div().text_size(11).text_color(t.ansi2).child(`+${add}`)).child(div().text_size(11).text_color(t.ansi1).child(`−${del}`)))
      .child(h_flex().flex_none().h(32).px(6).gap(4).text_size(11)
        .child(this.button("review-mode", this.mode === "auto" ? "自动布局" : this.mode === "split" ? "左右并排" : "统一对比", (_e, cx) => { this.mode = { auto: "split", split: "unified", unified: "auto" }[this.mode]; cx.notify(); }, t))
        .child(this.button("review-prev", "↑", (_e, cx) => this.navigate(-1, cx), t).tooltip("上一处改动 · Shift+F7"))
        .child(this.button("review-next", "↓", (_e, cx) => this.navigate(1, cx), t).tooltip("下一处改动 · F7"))
        .child(this.button("review-wrap", this.wrap ? "换行 ✓" : "换行", (_e, cx) => { this.wrap = !this.wrap; cx.notify(); }, t))
        .child(this.button("review-context", this.context ? "完整上下文 ✓" : "上下文", (_e, cx) => { this.context = !this.context; cx.notify(); }, t)))
      .when(Boolean(this.error && this.info), (e) => e.child(div().px(8).text_color(t.ansi1).child(this.error)))
      .child(h_flex().h(this.expanded ? 32 : 28).flex_none().px(10).justify_end()
        .child(div().id("session-expand").px(8).h(24).flex().items_center().rounded(5).text_size(12)
          .bg(t.raised).cursor_pointer().hover((s) => s.bg(t.hover))
          .when(!this.expanded && !file, (e) => e.opacity(0.4))
          .on_click(() => { if (this.expanded) closeOverlay(props().overlay); else this.expand(); })
          .child(this.expanded ? "关闭 · Esc" : "放大对比 ↗")))
      .when(!this.expanded && this.sessionChoices.length > 0, (e) => e.child(v_flex().id("deepseek-conversations")
        .max_h(160).overflow_y_scroll().px(8).gap(3).children(this.sessionChoices.map((session) =>
          div().id(`conversation-${session.id}`).px(6).py(4).rounded(4).truncate().cursor_pointer()
            .bg(t.raised).hover((s) => s.bg(t.hover)).tooltip(session.name).on_click((_e, cx) => {
              this.selectedSession = session.id; this.signature = ""; this.refresh(cx);
            }).child(session.name)))))
      .when(!this.expanded, (e) => e.child(div().flex_1().min_h(44).max_h(Math.max(1, shown.length) * 28 + 12).relative().border_b(1).border_color(t.border)
        .child(v_flex().id("session-changes-list").absolute().inset_0().overflow_y_scroll().p(6).children(shown.map((f) => {
          const parts = f.rel.split("/"), name = parts.pop();
          return h_flex().id(`sc-${f.path}`).h(28).flex_none().px(8).gap(8).items_center().rounded(7).cursor_pointer()
            .when(this.open === f.path, (e) => e.bg(t.selected)).hover((s) => s.bg(t.hover)).on_click((_e, cx) => this.pick(f.path, cx))
            .tooltip(f.label ? `${f.source_path || f.path}\n${f.label}\n${f.changes ?? 0} changed cells / fields` : f.asset ? `${f.path}\nUnreal asset saved after this conversation edited it` : f.script ? `${f.path}\nTool script: ${f.script === "run by the agent" ? "run by the agent" : f.script === "helper name" ? "helper-style name" : "in a temp folder"}` : `${f.path}\n${f.edits} ${f.edits === 1 ? "edit" : "edits"}`)
            .child(div().w(14).text_size(11).text_color(statusColor(f.status, t)).child(f.status))
            .child(h_flex().flex_1().min_w(0).when(Boolean(f.script), (e) => e.opacity(0.55))
              .child(div().text_color(t.dim).truncate().child(parts.length ? parts.join("/") + "/" : ""))
              .child(div().max_w(260).min_w(0).flex_none().truncate().child(name)))
            .when(Boolean(f.asset), (e) => e.child(div().px(4).rounded(3).border_1().border_color(t.accent).text_size(10).text_color(t.accent).child("UE")))
            .when(Boolean(f.structured), (e) => e.child(div().px(4).rounded(3).border_1().border_color(t.accent).text_size(10).text_color(t.accent).child(f.scope === "comparison" ? "Compare" : f.kind)))
            .when(Boolean(f.script), (e) => e.child(div().px(4).rounded(3).border_1().border_color(t.border).text_size(10).text_color(t.dim).child("script")))
            .when(f.add > 0, (e) => e.child(div().text_size(11).text_color(t.ansi2).child(`+${f.add}`)))
            .when(f.del > 0, (e) => e.child(div().text_size(11).text_color(t.ansi1).child(`−${f.del}`)));
        })).when(!shown.length, (e) => e.child(div().p(8).text_color(t.dim).child(empty))))
        .child(Scrollbar.vertical("session-changes-list").absolute().inset_0())))
      .when(Boolean(file?.label), (e) => e.child(div().flex_none().h(26).px(12).text_size(11).text_color(t.muted).truncate().tooltip(file.label).child(file.label)))
      .child(div().flex_1().min_h(0).relative().font_family(settings().font_family ?? "JetBrains Mono").text_size(this.expanded ? 14 : 11)
        .child(div().id("session-changes-diff-x").size_full().overflow_x_scrollbar()
          .child(div().w_full().min_w(width).h_full()
            .child(v_flex().id("session-changes-diff").overflow_y_scroll().children(stream.map((item) => {
                const entry = item.file, row = item.row;
                if (row[0] === "F") return h_flex().h(this.rowHeight).flex_none().gap(8).px(8).bg(t.raised)
                  .child(div().flex_1().truncate().child(entry.rel))
                  .when(Boolean(entry.review), (e) => e.child(this.button(`undo-${entry.path}`, this.confirmUndo === `${entry.path}:file` ? "确认撤销文件" : "撤销文件", (_e, cx) => this.undo(entry, null, cx), t)))
                  .when(!entry.review, (e) => e.child(div().text_size(10).text_color(t.dim).tooltip("缺少可验证的文本基线，或属于结构化/二进制对比").child("撤销不可用")));
                if (row[0] === "@") return h_flex().h(this.rowHeight).flex_none()
                  .child(this.diffRow(row, t).flex_1().min_w(0))
                  .when(Boolean(entry.review && !this.context), (e) => e.child(this.button(`undo-block-${entry.path}-${item.block}`, this.confirmUndo === `${entry.path}:${item.block}` ? "确认撤销" : "撤销块", (_e, cx) => this.undo(entry, item.block, cx), t)));
                if (row[0] === "S") return h_flex().w_full().h(this.rowHeight)
                  .child(div().w(width / 2).flex_none().overflow_hidden().child(this.diffRow(item.left || ["", null, ""], t)))
                  .child(div().flex_1().min_w(0).overflow_hidden().child(this.diffRow(item.right || ["", null, ""], t)));
                return this.compareRow(row, t);
              })).track_scroll(this.scroll).size_full())))
        .child(Scrollbar.vertical("session-changes-diff").viewport_from_layout().absolute().inset_0())
        .when(!file && shown.length > 0, (e) => e.child(div().absolute().top(6).px(12).text_color(t.dim).child("Select a file to view its changes"))))
      .child(h_flex().flex_none().h(34).px(12).gap(8).items_center().border_t(1).border_color(t.border).text_size(11).text_color(t.dim)
        .child(div().flex_1().min_w(0).truncate().child(this.info ? `${this.info.agent} · ${this.info.name || this.info.title || this.info.session.slice(0, 8)}` : this.address ?? ""))
        .when(hidden > 0, (e) => e.child(div().id("toggle-scripts").px(8).h(22).flex().items_center().rounded(6).cursor_pointer()
          .bg(this.showScripts ? t.selected : t.raised).text_color(this.showScripts ? t.foreground : t.muted).hover((s) => s.bg(t.hover))
          .on_click((_e, cx) => {
            this.showScripts = !this.showScripts;
            const list = this.visible();
            if (!list.some((f) => f.path === this.open)) this.open = this.closed ? null : list[0]?.path ?? null;
            cx.notify();
          })
          .child(this.showScripts ? `Hide tool scripts (${hidden})` : `Show tool scripts (${hidden})`))));
  }
}
