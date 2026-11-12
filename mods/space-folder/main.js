// Project folder of a space (like a Codex project): new tabs and agents started
// under the space begin there. Opened as a dialog with props { space }.
// Stored in shared state "space.project": { [spaceId]: folder }.

import { View, div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input } from "gpui-base";
import { exec, spaces, panes, props, closeOverlay, shared, setShared, pickFolder, theme, window, notify } from "herdr";

const KEY = "space.project";

// Where a space is now: its first terminal's live folder, else where it was made.
function place(s) {
  const tabs = new Set((s.tabs ?? []).map((c) => c.id));
  return panes().find((p) => tabs.has(p.tab) && p.cwd)?.cwd || s.folder || "";
}

// Pasted paths often come quoted ("C:\work\game"); strip quotes and trailing separators.
export function clean(text) {
  let v = String(text ?? "").trim().replace(/^(["'])(.*)\1$/, "$2").trim();
  if (v.length > 1 && !/^[A-Za-z]:[\\/]$/.test(v)) v = v.replace(/[\\/]+$/, "");
  return v;
}

export default class SpaceFolder extends View {
  init(_props, cx) {
    const space = spaces().find((s) => s.id === props()?.space);
    this.space = space ?? null;
    this.current = space ? (shared(KEY) ?? {})[space.id] ?? "" : "";
    this.text = this.current || (space ? place(space) : "");
    this.error = "";
    this.busy = false;
    this.input = InputState.new({ value: this.text, placeholder: "Folder where new tabs and agents start" });
    if (this.text) this.input.set_selected_range({ start: 0, end: this.text.length });
    this.input.on("change", (_e, cx) => { this.text = this.input.value(); this.error = ""; cx.notify(); });
    this.input.on("submit", (_e, cx) => this.save(cx));
    this.inputFocused = true;
    this.input.on("focus", (_e, cx) => { this.inputFocused = true; cx.notify(); });
    this.input.on("blur", (_e, cx) => { this.inputFocused = false; cx.notify(); });
    this.input.focus();
    cx.timer.after(600, () => this.input.focus());
    this.handle = cx.focus_handle();
  }

  store(folder) {
    const all = { ...(shared(KEY) ?? {}) };
    if (folder) all[this.space.id] = folder;
    else delete all[this.space.id];
    setShared(KEY, all);
  }

  // The folder must exist: a bad one would make every new tab fail later.
  // Any trivial program fails to start in a folder that isn't there.
  async exists(folder) {
    const local = !this.space.machine;
    if (local && window()?.platform !== "windows") return exec("true", [], { cwd: folder }).then(() => true, () => false);
    if (local) return exec("cmd", ["/c", "exit", "0"], { cwd: folder }).then(() => true, () => false);
    // Another machine may be anything: an unreachable machine is the only error we can tell.
    return exec("git", ["--version"], { cwd: folder, on: this.space.machine }).then(() => true, () => false);
  }

  save(cx) {
    if (!this.space || this.busy) return;
    const folder = clean(this.text);
    if (!folder) return this.clear();
    this.busy = true;
    this.error = "";
    cx.notify();
    cx.spawn(async (cx) => {
      const ok = await this.exists(folder);
      this.busy = false;
      if (!ok) {
        this.error = this.space.machine ? "That folder isn't reachable on " + this.space.machine + "." : "That folder doesn't exist.";
        cx.notify();
        return;
      }
      this.store(folder);
      closeOverlay(props().overlay);
      notify({ title: "Project folder set", body: folder, level: "info", timeout: 2500 });
    });
  }

  clear() {
    if (!this.space) return;
    this.store("");
    closeOverlay(props().overlay);
  }

  browse(cx) {
    cx.spawn(async (cx) => {
      const picked = await pickFolder({ prompt: "Project folder" }).catch(() => null);
      if (picked) {
        this.text = picked;
        this.error = "";
        this.input.set_value(picked);
      }
      this.input.focus();
      cx.notify();
    });
  }

  key(e, cx) {
    if (e.key === "escape") { closeOverlay(props().overlay); cx.stop_propagation(); }
  }

  render() {
    const t = theme();
    const p = props() ?? {};
    const button = (id, text, kind, action) => h_flex().id(id).h(32).px(14).items_center().rounded(8)
      .border_1().border_color(kind === "primary" ? t.solid : t.input).bg(kind === "primary" ? t.solid : t.raised)
      .text_color(kind === "primary" ? t.solid_foreground : kind === "danger" ? t.blocked : t.foreground)
      .text_size(12.5).font_medium().cursor_pointer().hover((s) => s.opacity(0.88)).on_click(action).child(text);
    const local = this.space && !this.space.machine;
    return v_flex().id("space-folder").track_focus(this.handle).on_key_down((e, cx) => this.key(e, cx))
      .w_full().min_h(0).p(18).bg(t.popover).text_color(t.foreground)
      .child(h_flex().items_center().child(div().flex_1().text_size(15).font_semibold().child("Project folder"))
        .child(div().text_size(11).text_color(t.dim).child("Esc")))
      .child(div().pt(6).text_size(11.5).text_color(t.dim)
        .child(this.space ? `New tabs and agents in “${this.space.name}” start here.` : "This space is gone."))
      .child(div().pt(14).pb(7).text_size(11.5).text_color(t.dim).child("Folder"))
      .child(h_flex().gap(8).items_center()
        .child(div().flex_1().min_w(0).child(Input.new(this.input).h(36).px(10).rounded(9).border_1()
          .border_color(this.error ? t.blocked : this.inputFocused ? t.accent : t.input).bg(t.background).text_size(12.8).text_color(t.foreground)))
        .when(local, (el) => el.child(button("sf-browse", "Browse…", "plain", (_e, cx) => this.browse(cx)))))
      .child(div().pt(6).text_size(11.5).text_color(this.error ? t.blocked : t.dim)
        .child(this.error || (this.space?.machine ? `On ${this.space.machine}.` : "Leave empty to follow the focused terminal again.")))
      .child(h_flex().pt(20).gap(8).justify_end()
        .when(Boolean(this.current), (el) => el.child(div().flex_1().child(h_flex().child(button("sf-clear", "Clear", "danger", () => this.clear())))))
        .child(button("sf-cancel", "Cancel", "plain", () => closeOverlay(p.overlay)))
        .child(button("sf-save", this.busy ? "Checking…" : "Save", "primary", (_e, cx) => this.save(cx))));
  }
}
