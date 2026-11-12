import { View, div } from "gpui-kit";
import { v_flex, h_flex, Scrollbar, v_virtual_list } from "gpui-base";
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
    this.panelWidth = 360;
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
    this.open = this.open === path ? null : path;
    this.closed = !this.open;
    cx.notify();
  }

  diffRow([kind, number, text], t) {
    if (kind === "@") return div().w_full().px(12).h(20).flex_none().truncate().bg(t.raised).text_color(t.dim).child(text);
    return h_flex().w_full().h(this.expanded ? 28 : 20).flex_none().gap(10).when(kind === "+", (e) => e.bg(t.ansi2 + "18")).when(kind === "-", (e) => e.bg(t.ansi1 + "18"))
      .child(div().w(34).flex_none().text_right().text_color(t.dim).child(number == null ? "" : String(number)))
      .child(div().w(10).flex_none().text_color(kind === "+" ? t.ansi2 : kind === "-" ? t.ansi1 : t.dim).child(kind === " " ? "" : kind))
      .child(div().flex_1().min_w(0).truncate().text_color(kind === "" ? t.dim : t.foreground).child(text || " "));
  }

  compareRow(row, t) {
    if (row[0] !== "C") return this.diffRow(row, t);
    return h_flex().h(this.expanded ? 30 : 24).flex_none().w_full().text_size(this.expanded ? 14 : 11)
      .child(div().w(this.locationWidth).flex_none().px(8).truncate().text_color(t.muted).tooltip(row[1]).child(row[1]))
      .child(div().w(this.valueWidth).flex_none().px(8).h_full().bg(t.ansi1 + "12").text_color(row[2] === "∅" ? t.dim : t.ansi1).truncate().tooltip(row[2]).child(row[2]))
      .child(div().flex_1().min_w(this.valueWidth).px(8).h_full().bg(t.ansi2 + "12").text_color(row[3] === "∅" ? t.dim : t.ansi2).truncate().tooltip(row[3]).child(row[3]));
  }

  render(cx) {
    if (this.followedAgent()?.address !== this.address && !this.loading) this.refresh(cx);
    const t = theme(), shown = this.expanded ? this.files.filter((f) => f.path === this.open) : this.visible(), hidden = this.expanded ? 0 : this.files.length - this.files.filter((f) => !f.script).length;
    const add = shown.reduce((n, f) => n + f.add, 0), del = shown.reduce((n, f) => n + f.del, 0);
    const file = this.files.find((f) => f.path === this.open);
    const rows = file ? (file.cut ? [...file.rows, ["", null, `… showing the first ${file.rows.length} lines`]] : file.rows) : [];
    const structured = rows.some((r) => r[0] === "C");
    const width = structured ? Math.max(330, this.panelWidth - 2) : Math.max(300, ...rows.slice(0, 400).map((r) => (r[2]?.length ?? 0) * 7 + 70));
    this.locationWidth = this.expanded ? Math.min(360, Math.round(width * 0.32)) : Math.round(width * 0.42);
    this.valueWidth = Math.floor((width - this.locationWidth) / 2);
    const empty = this.error || (!this.address ? "No agent in this tab" : this.loading && !this.info ? "Reading conversation…" : this.info ? (hidden ? "Only tool scripts changed in this conversation" : "No files changed in this conversation yet") : "Reading conversation…");

    return v_flex().id("session-changes").size_full().min_h(0).rounded(10).overflow_hidden().bg(t.background).border_1().border_color(t.border).text_color(t.foreground).text_size(13).track_focus(this.handle)
      .on_size_change((size, cx) => { if (Math.abs(size.width - this.panelWidth) > 1) { this.panelWidth = size.width; cx.notify(); } })
      .child(h_flex().h(42).flex_none().px(14).gap(8).items_center().border_b(1).border_color(t.border)
        .child(div().font_semibold().child(this.expanded ? "文件对比" : "Session"))
        .when(!this.expanded, (e) => e.child(div().px(5).py(1).rounded(4).border_1().border_color(t.border).text_size(10).text_color(t.muted).child(`${shown.length} files`)))
        .when(this.expanded, (e) => e.child(div().flex_1().min_w(0).truncate().tooltip(file?.source_path || file?.path || "").child(file?.rel || "Reading…")))
        .child(div().flex_1()).child(div().text_size(11).text_color(t.ansi2).child(`+${add}`)).child(div().text_size(11).text_color(t.ansi1).child(`−${del}`)))
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
      .child(v_flex().flex_1().min_h(0).relative().font_family(settings().font_family ?? "JetBrains Mono").text_size(this.expanded ? 14 : 11)
        .child(div().id("session-changes-diff-x").size_full().overflow_x_scrollbar()
          .child(v_flex().w_full().min_w(width).h_full()
            .when(structured, (e) => e.child(h_flex().h(26).flex_none().bg(t.raised).text_color(t.muted)
              .child(div().w(this.locationWidth).flex_none().px(8)
                .child(file?.kind === "JSONL" ? "Line / field" : ["JSON", "TOML"].includes(file?.kind) ? "Field path" : "Sheet / cell"))
              .child(div().w(this.valueWidth).flex_none().px(8).child("Before"))
              .child(div().flex_1().px(8).child("After"))))
            .child(v_virtual_list("session-changes-diff", rows.length, this.expanded ? (structured ? 30 : 28) : (structured ? 24 : 20), (i) => `${this.open}:${i}`,
              (range) => rows.slice(range.start, range.end).map((row) => this.compareRow(row, t))).w_full().flex_1().min_h(0))))
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
