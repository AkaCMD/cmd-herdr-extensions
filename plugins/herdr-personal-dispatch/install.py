#!/usr/bin/env python3
"""Register this local plugin in Codex, Claude Code, or both."""
import argparse
import importlib.util
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("dispatch_runtime", ROOT / "skills/dispatch/scripts/dispatch.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", choices=["both", "codex", "claude"], default="both")
    args = p.parse_args()
    hosts = ["codex", "claude"] if args.host == "both" else [args.host]
    failed = []
    for host in hosts:
        try:
            exe = runtime.executable(host)
            commands = [[exe, "plugin", "marketplace", "add", str(ROOT), "--json"]]
            commands.append([exe, "plugin", "add" if host == "codex" else "install", "herdr-personal-dispatch@AkaCMD-local", "--json"])
            for command in commands:
                result = subprocess.run(command, cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=120)
                print(host + ": " + result.stdout.strip())
                if result.returncode:
                    raise RuntimeError(result.stderr.strip() or "plugin command failed")
                if host == "claude" and command[2] == "install":
                    import json
                    record = json.loads(result.stdout)
                    if record.get("installedVersion") and record.get("availableVersion") != record.get("installedVersion"):
                        update = subprocess.run([exe, "plugin", "update", "herdr-personal-dispatch@AkaCMD-local", "--json"], cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=120)
                        print(host + ": " + update.stdout.strip())
                        if update.returncode:
                            raise RuntimeError(update.stderr.strip())
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            print(host + ": " + str(e), file=sys.stderr)
            failed.append(host)
    print("请在 Herdr 中新开或重新加载对应客户端会话以发现技能。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
