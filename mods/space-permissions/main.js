// Agent permissions of a space (like a Codex permission profile): the profile
// new agent sessions in the space start with, and whether it is applied at all.
// Opened as a dialog from the space menu with props { space }.
// Stored in shared state "space.permissions": { [spaceId]: { profile, override } },
// and mirrored to data/agent-permissions.json, which the launch bridge reads.

import { View, div, svg } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { spaces, props, closeOverlay, shared, setShared, appHome, readTextFile, writeTextFile, theme, notify } from "herdr";

const KEY = "space.permissions";
const PROFILES = [
  ["default", "Default", "Follow the agent's own configuration"],
  ["plan", "Plan", "Read-only: look and read, never write"],
  ["auto", "Auto", "Writable, no approval prompts"],
  ["yolo", "Yolo", "No sandbox, no approval prompts"],
];

const policyPath = () => `${appHome().replace(/[\\/]+$/, "")}/data/agent-permissions.json`;

function policyOf(text) {
  try {
    const doc = JSON.parse(text ?? "");
    if (doc && typeof doc === "object" && doc.spaces && typeof doc.spaces === "object") return doc;
  } catch {}
  return { version: 1, spaces: {} };
}

// The bridge reads this file, so write it before claiming the change.
// writeTextFile refuses a stale snapshot: read, retry once.
async function store(spaceId, value) {
  const path = policyPath();
  for (let attempt = 0; ; attempt++) {
    const text = await readTextFile(path);
    const doc = policyOf(text);
    if (value) doc.spaces[spaceId] = value;
    else delete doc.spaces[spaceId];
    try {
      await writeTextFile(path, JSON.stringify(doc, null, 2) + "\n", text ?? null);
      return;
    } catch (error) {
      if (attempt === 1) throw error;
    }
  }
}

export default class SpacePermissions extends View {
  init(_props, cx) {
    const space = spaces().find((s) => s.id === props()?.space);
    this.space = space ?? null;
    const current = space ? (shared(KEY) ?? {})[space.id] : null;
    this.profile = current?.profile ?? "default";
    this.override = current?.override ?? true;
    this.busy = false;
    this.confirming = false;
    this.error = "";
    this.handle = cx.focus_handle();
    this.handle.focus();
  }

  choose(profile, cx) {
    if (this.profile === profile) return;
    this.profile = profile;
    this.confirming = false;
    this.error = "";
    cx.notify();
  }

  toggle(cx) {
    this.override = !this.override;
    this.confirming = false;
    this.error = "";
    cx.notify();
  }

  // Yolo is the one setting that can let an agent do anything: ask once.
  save(cx) {
    if (!this.space || this.busy) return;
    if (this.profile === "yolo" && this.override && !this.confirming) {
      this.confirming = true;
      cx.notify();
      return;
    }
    const value = this.profile === "default" ? null : { profile: this.profile, override: this.override };
    this.busy = true;
    this.error = "";
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        await store(this.space.id, value);
      } catch (error) {
        this.busy = false;
        this.error = "Could not save: " + (error?.message ?? error);
        cx.notify();
        return;
      }
      const all = { ...(shared(KEY) ?? {}) };
      if (value) all[this.space.id] = value;
      else delete all[this.space.id];
      setShared(KEY, all);
      closeOverlay(props().overlay);
      notify({
        title: value ? "Agent permissions saved" : "Agent permissions cleared",
        body: value ? `${this.space.name}: ${value.profile}${value.override ? "" : " (not applied)"}` : this.space.name,
        level: "info",
        timeout: 2500,
      });
    });
  }

  key(e, cx) {
    const digit = /^[1-4]$/.test(e.key) ? Number(e.key) - 1 : -1;
    if (e.key === "escape") closeOverlay(props().overlay);
    else if (digit >= 0) this.choose(PROFILES[digit][0], cx);
    else if (e.key === "enter") this.save(cx);
    else return;
    cx.stop_propagation();
  }

  render() {
    const t = theme();
    const p = props() ?? {};
    const button = (id, text, kind, action) => h_flex().id(id).h(32).px(14).items_center().rounded(8)
      .border_1().border_color(kind === "primary" ? t.solid : t.input).bg(kind === "primary" ? t.solid : t.raised)
      .text_color(kind === "primary" ? t.solid_foreground : kind === "danger" ? t.blocked : t.foreground)
      .text_size(12.5).font_medium().cursor_pointer().hover((s) => s.opacity(0.88)).on_click(action).child(text);
    const row = (profile, name, hint) => {
      const selected = this.profile === profile;
      const alarm = profile === "yolo" && selected;
      return h_flex().id(`perm-${profile}`).h(42).px(10).gap(10).rounded(9).items_center()
        .when(selected, (s) => s.bg(t.selected))
        .hover((s) => s.bg(t.hover))
        .on_click((_e, cx) => this.choose(profile, cx))
        .child(h_flex().w(15).h(15).flex_none().rounded(8).justify_center().items_center().border_1()
          .border_color(selected ? (alarm ? t.blocked : t.accent) : t.input)
          .when(selected, (s) => s.child(div().w(7).h(7).rounded(4).bg(alarm ? t.blocked : t.accent))))
        .child(v_flex().flex_1().min_w(0).gap(1)
          .child(div().text_size(13).text_color(alarm ? t.blocked : t.foreground).child(name))
          .child(div().text_size(11).text_color(t.dim).truncate().child(hint)));
    };
    const saveLabel = this.busy ? "Saving…" : this.confirming ? "Save · allow it" : "Save";
    return v_flex().id("space-permissions").track_focus(this.handle).on_key_down((e, cx) => this.key(e, cx))
      .w_full().min_h(0).p(18).bg(t.popover).text_color(t.foreground)
      .child(h_flex().items_center()
        .child(div().flex_1().text_size(15).font_semibold().child("Agent permissions"))
        .child(div().text_size(11).text_color(t.dim).child("Esc")))
      .child(div().pt(6).text_size(11.5).text_color(t.dim)
        .child(this.space ? `New agent sessions in “${this.space.name}” start with this.` : "This space is gone."))
      .child(v_flex().pt(12).gap(2).children(PROFILES.map(([profile, name, hint]) => row(profile, name, hint))))
      .child(h_flex().id("perm-override").mt(10).h(52).px(10).gap(10).rounded(9).items_center()
        .border_1().border_color(this.override ? t.input : t.border)
        .hover((s) => s.bg(t.hover))
        .on_click((_e, cx) => this.toggle(cx))
        .child(v_flex().flex_1().min_w(0).gap(1)
          .child(div().text_size(13).child("Apply to new sessions"))
          .child(div().text_size(11).text_color(t.dim).truncate()
            .child(this.override ? "The space decides; the agent's own defaults are overridden" : "Kept as a note only; agents start with their own defaults")))
        .child(h_flex().w(36).h(20).flex_none().rounded(10).p(2).items_center()
          .when(this.override, (s) => s.justify_end())
          .bg(this.override ? t.accent : t.input)
          .child(div().w(16).h(16).rounded(8).bg(t.solid_foreground))))
      .child(div().pt(8).text_size(11).text_color(this.error ? t.blocked : t.dim)
        .child(this.error || "Running sessions keep theirs until restarted."))
      .when(!this.error, (el) => el.child(div().pt(2).text_size(11).text_color(t.dim)
        .child("Claude Code, Codex and DeepSeek Harness follow a space; Pi and OpenCode are not adapted.")))
      .child(h_flex().pt(16).gap(8).justify_end()
        .child(button("perm-cancel", "Cancel", "plain", () => closeOverlay(p.overlay)))
        .child(button("perm-save", saveLabel, this.confirming ? "danger" : "primary", (_e, cx) => this.save(cx))));
  }
}
