import { View, div } from "gpui-kit";
import { focused, terminals, props, appHome, exec, hc, notify, nextCommand } from "herdr";

// Capture focus before async work so a tab switch cannot redirect Escape.
export function target() {
  const f = focused();
  const address = f?.terminal ?? f?.agent;
  return terminals().find(t => t.address === address) ?? null;
}

export async function stop(t) {
  if (!t) return;
  if (!t.labels?.["dispatch.controller"]) {
    const result = await hc(["keys", t.address, "esc", "--json"]);
    if (result.code !== 0) throw new Error(result.stderr || "Esc 未送达");
    return;
  }
  if (t.address.includes("/")) throw new Error("请在管理者所在机器安装调度 Esc 扩展");
  const config = props() ?? {};
  const script = config.script ?? `${appHome()}/plugins/herdr-personal-dispatch/skills/dispatch/scripts/dispatch.py`;
  const args = [script, "control", "--terminal", t.address, "--action", "stop", "--key", "esc"];
  if (t.labels["dispatch.home"]) args.push("--data-home", t.labels["dispatch.home"]);
  const result = await exec(config.python ?? "python", args, { timeout: 180000 });
  if (result.code !== 0) throw new Error(result.stderr || result.stdout || "暂停未完成");
  const state = JSON.parse(result.stdout);
  if (state.status === "pausing") notify({ title: "调度正在暂停", body: state.note ?? "", level: "info", timeout: 5000 });
}

export default class DispatchEscape extends View {
  init(_props, cx) {
    cx.spawn(async cx => {
      for (;;) {
        if (await nextCommand() !== "stop-escape") continue;
        const selected = target();
        cx.spawn(async () => {
          try { await stop(selected); }
          catch (error) { notify({ title: "暂停未完成", body: String(error?.message ?? error), level: "error", timeout: 6000 }); }
        });
      }
    });
  }

  render() { return div(); }
}
