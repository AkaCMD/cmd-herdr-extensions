"""Opt-in live smoke in a separate HC_SESSION and disposable non-Git directory."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

SCRIPT = Path(__file__).resolve().parents[1] / "skills/dispatch/scripts/dispatch.py"
spec = importlib.util.spec_from_file_location("dispatch", SCRIPT)
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


def controller(scratch):
    root = scratch / "普通目录 测试"
    root.mkdir(exist_ok=True)
    os.environ["PERSONAL_DISPATCH_HOME"] = str(scratch / "dispatch-data")
    hc, store = d.Herdr(), d.Store()
    print("\033]7501;state=working:app=dispatch-test\033\\", end="", flush=True)
    jobs = []
    for agent in ["codex", "claude"]:
        name = agent + "-ok.txt"
        jobs.append({
            "id": agent, "agent": agent,
            "task": "这是隔离测试。在当前普通目录创建 " + name + "，内容精确为 " + agent + "-ok 加一个换行。先按简报登记新增文件备份。无需分析其他项目、调用外部系统或派代理。完成后按简报写 result.json。",
            "writes": [name], "reads": [], "resources": [], "depends_on": [],
            "acceptance": ["文件 UTF-8 内容精确正确", "结构化报告完成"],
            "checks": [{"argv": [sys.executable, "-c", "from pathlib import Path; assert Path(" + repr(name) + ").read_bytes()==" + repr((agent + "-ok\n").encode())], "timeout": 10}]
        })
    r = d.init_run(store, hc, root, {"objective": "隔离测试两个真实 Herdr 交互代理，不涉及用户业务文件", "jobs": jobs}, "codex", 2)
    d.atomic(scratch / "run.json", {"run": r["id"], "controller": os.environ["HC_TERMINAL_ID"]})
    deadline = time.time() + 240
    try:
        while time.time() < deadline:
            r = d.sweep(store, hc, r["id"])
            for j in r["jobs"]:
                if j["status"] == "review":
                    # Independent mechanical fixture checks known test acceptance.
                    spec = j["spec"]
                    file = root / (spec["agent"] + "-ok.txt")
                    assert file.read_bytes() == (spec["agent"] + "-ok\n").encode()
                    d.start_checks(store, hc, store.run(r["id"]), j["id"])
                    info = d.load(d.job_dir(r["id"], j["id"]) / "check-terminal.json")
                    hc.call("wait", info["address"], "--timeout", "20", "--json")
                    evidence = scratch / (j["id"] + "-evidence.md")
                    evidence.write_text("读取实际文件并核对精确字节；独立检查进程退出码为 0。", encoding="utf-8")
                    d.accept(store, hc, store.run(r["id"]), j["id"], evidence)
            r = d.sweep(store, hc, r["id"])
            d.atomic(scratch / "live-status.json", r)
            if r["status"] == "complete":
                d.atomic(scratch / "outcome.json", {"passed": True, "run": r["id"], "root": str(root), "jobs": [{"id": j["id"], "status": j["status"], "terminal": j["address"]} for j in r["jobs"]]})
                return
            if all(j["status"] in {"blocked", "needs_user", "complete"} for j in r["jobs"]):
                break
            hc.call("wait", "--children", "any", "--timeout", "15", "--json")
        d.atomic(scratch / "outcome.json", {"passed": False, "run": r["id"], "jobs": [{"id": j["id"], "status": j["status"], "note": j["note"]} for j in store.jobs(r["id"])]})
    except Exception as e:
        d.atomic(scratch / "outcome.json", {"passed": False, "error": str(e), "run": r["id"]})
    finally:
        # Only interrupt/kill terminals in this isolated test's recorded run.
        if store.run(r["id"])["status"] != "complete":
            d.pause(store, hc, store.run(r["id"]))
        for j in store.jobs(r["id"]):
            if j["address"]:
                t = hc.owned(j["address"], r["id"], j["token"])
                if t["state"] == "live":
                    hc.call("kill", j["address"], "--json")
        current = store.run(r["id"])
        if current["watcher"]:
            t = hc.owned(current["watcher"], r["id"], current["watcher_token"])
            if t["state"] == "live":
                hc.call("kill", current["watcher"], "--json")
        print("\033]7501;state=done:app=dispatch-test\033\\", end="", flush=True)


def launcher(scratch):
    env = os.environ.copy()
    for k in ["HC_TERMINAL_ID", "HC_TERMINAL_SOCKET", "HC_SCOPE"]:
        env.pop(k, None)
    env.update(d.load(scratch / "env.json"))
    env["PERSONAL_DISPATCH_HOME"] = str(scratch / "dispatch-data")
    hc = d.executable("hc")
    subprocess.run([hc, "server", "start", "--json"], env=env, check=True)
    p = subprocess.run([hc, "run", "--bg", "--json", "--no-parent", "--name", "dispatch-test-controller", "--cwd", str(scratch), sys.executable, str(Path(__file__).resolve()), "--controller", str(scratch)], env=env, capture_output=True, text=True, encoding="utf-8", check=True)
    print(p.stdout)
    (scratch / "controller-terminal.json").write_text(p.stdout, encoding="utf-8")


if __name__ == "__main__":
    scratch = Path(sys.argv[-1]).resolve()
    if sys.argv[1] == "--controller":
        controller(scratch)
    elif sys.argv[1] == "--launch":
        launcher(scratch)
    else:
        raise SystemExit("Use --launch SCRATCH; scratch/env.json must specify an isolated HC_SESSION.")
