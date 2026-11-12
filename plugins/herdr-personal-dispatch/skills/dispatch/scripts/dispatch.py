#!/usr/bin/env python3
"""Personal Herdr dispatcher. Python 3.10+, standard library, local hc API."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid

SCRIPT = Path(__file__).resolve()
SKILL = SCRIPT.parent.parent
LOCKED = {"starting", "running", "review", "blocked", "needs_user", "paused"}
ID = re.compile(r"^[a-z][a-z0-9-]{0,23}$")


class DispatchError(RuntimeError):
    pass


def now():
    return time.time()


def home():
    return Path(os.environ.get("PERSONAL_DISPATCH_HOME", str(Path.home() / ".local/share/herdr-personal-dispatch"))).resolve()


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def contained(path, root):
    return path == root or root in path.parents


def resolve_scope(root, value):
    if not isinstance(value, str) or not value or any(c in value for c in "*?[]"):
        raise DispatchError("scope 必须是精确相对文件或目录，不能用 glob")
    p = Path(value)
    if p.is_absolute():
        raise DispatchError("plan scope 必须相对工作区")
    resolved = (root / p).resolve()
    if not contained(resolved, root):
        raise DispatchError("scope 超出工作区: " + value)
    return os.path.normcase(str(resolved))


def overlaps(a, b):
    a, b = Path(a), Path(b)
    return contained(a, b) or contained(b, a)


def conflict(a, b):
    if set(a["resources"]) & set(b["resources"]):
        return True
    return any(overlaps(x, y) for x in a["write_paths"] for y in b["read_paths"] + b["write_paths"]) or any(
        overlaps(x, y) for x in b["write_paths"] for y in a["read_paths"])


def validate_plan(plan, root, default_agent):
    if not isinstance(plan, dict) or not isinstance(plan.get("objective"), str) or not plan["objective"].strip():
        raise DispatchError("plan 需要非空 objective")
    jobs = plan.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise DispatchError("plan 需要 jobs")
    ids = []
    normalized = []
    for j in jobs:
        if not isinstance(j, dict) or not ID.fullmatch(str(j.get("id", ""))):
            raise DispatchError("job id 必须是字母开头的 1-24 位小写字母、数字或连字符")
        j = dict(j)
        ids.append(j["id"])
        if not isinstance(j.get("task"), str) or not j["task"].strip():
            raise DispatchError("job task 不能为空")
        j.setdefault("agent", default_agent)
        if j["agent"] not in {"codex", "claude", "deepseek"}:
            raise DispatchError("agent 只能是 codex、claude 或 deepseek")
        for key in ["depends_on", "reads", "writes", "resources", "acceptance"]:
            j.setdefault(key, [])
            if not isinstance(j[key], list) or not all(isinstance(v, str) and v for v in j[key]):
                raise DispatchError(key + " 必须是非空字符串列表（列表本身可为空）")
        if not j["acceptance"]:
            raise DispatchError("每个任务必须有 acceptance")
        j["read_paths"] = [resolve_scope(root, v) for v in j["reads"]]
        j["write_paths"] = [resolve_scope(root, v) for v in j["writes"]]
        j.setdefault("checks", [])
        if not isinstance(j["checks"], list):
            raise DispatchError("checks 必须是列表")
        for c in j["checks"]:
            if not isinstance(c, dict) or not isinstance(c.get("argv"), list) or not c["argv"] or not all(isinstance(v, str) and v for v in c["argv"]):
                raise DispatchError("check.argv 必须是非空 argv 列表")
            c["cwd_path"] = resolve_scope(root, c.get("cwd", "."))
            if not isinstance(c.get("timeout", 300), (int, float)) or not 0 < c.get("timeout", 300) <= 7200:
                raise DispatchError("check timeout 必须在 0 到 7200 秒间")
        normalized.append(j)
    if len(ids) != len(set(ids)):
        raise DispatchError("job id 重复")
    graph = {j["id"]: j["depends_on"] for j in normalized}
    visiting, visited = set(), set()
    def visit(key):
        if key in visiting:
            raise DispatchError("任务依赖有环")
        if key in visited:
            return
        visiting.add(key)
        for dep in graph[key]:
            if dep not in graph:
                raise DispatchError("依赖不存在: " + dep)
            visit(dep)
        visiting.remove(key)
        visited.add(key)
    for key in graph:
        visit(key)
    return {"objective": plan["objective"], "jobs": normalized}


class Store:
    def __init__(self, readonly=False):
        if not readonly:
            home().mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect((home() / "dispatch.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=30) if readonly else sqlite3.connect(home() / "dispatch.sqlite3", timeout=30)
        self.db.row_factory = sqlite3.Row
        if readonly:
            return
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS runs(
          id TEXT PRIMARY KEY, root TEXT, objective TEXT, agent TEXT, lanes INTEGER,
          controller TEXT, status TEXT, restarts INTEGER DEFAULT 0,
          watcher TEXT, watcher_token TEXT, note TEXT DEFAULT '', created REAL);
        CREATE TABLE IF NOT EXISTS jobs(
          run_id TEXT REFERENCES runs(id), id TEXT, spec TEXT, status TEXT DEFAULT 'pending',
          attempt INTEGER DEFAULT 1, nudges INTEGER DEFAULT 0, address TEXT, token TEXT,
          sent REAL DEFAULT 0, baseline_turn INTEGER DEFAULT 0, note TEXT DEFAULT '',
          PRIMARY KEY(run_id,id));
        """)

    def run(self, ref):
        key = Path(ref).name if ("/" in ref or "\\" in ref) else ref
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (key,)).fetchone()
        if not row:
            raise DispatchError("未找到 run: " + ref)
        return dict(row)

    def jobs(self, run_id):
        rows = self.db.execute("SELECT * FROM jobs WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
        return [dict(row) for row in rows]

    def job(self, run_id, job_id):
        row = self.db.execute("SELECT * FROM jobs WHERE run_id=? AND id=?", (run_id, job_id)).fetchone()
        if not row:
            raise DispatchError("未找到 job: " + job_id)
        return dict(row)

    def update(self, table, keys, **fields):
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(" + table + ")")}
        if not set(fields) <= columns or not set(keys) <= columns:
            raise DispatchError("非法状态字段")
        self.db.execute("UPDATE " + table + " SET " + ",".join(k + "=?" for k in fields) + " WHERE " + " AND ".join(k + "=?" for k in keys), tuple(fields.values()) + tuple(keys.values()))
        self.db.commit()

    def snapshot(self, run_id):
        r = self.run(run_id)
        r["jobs"] = [{**j, "spec": json.loads(j["spec"])} for j in self.jobs(run_id)]
        atomic(run_dir(run_id) / "state.json", r)
        return r


def run_dir(run_id):
    return home() / "runs" / run_id


def job_dir(run_id, job_id):
    return run_dir(run_id) / "tasks" / job_id


def executable(kind):
    # Resolve native binaries, never execute npm PowerShell shims or shell strings.
    override = os.environ.get("PERSONAL_DISPATCH_" + kind.upper())
    if override:
        p = Path(override).resolve()
        if not p.is_file() or p.suffix.lower() in {".cmd", ".bat", ".ps1"}:
            raise DispatchError("可执行路径必须是原生程序: " + kind)
        return str(p)
    candidates = []
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        roaming = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))
        if kind == "hc":
            candidates = [local / "Programs/herdr-core/hc.exe"]
        elif kind == "claude":
            candidates = [roaming / "npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe", Path.home() / ".local/bin/claude.exe"]
        elif kind == "codex":
            candidates = sorted((local / "OpenAI/Codex/bin").glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
            candidates += list((roaming / "npm/node_modules/@openai").glob("codex*/vendor/*/codex/codex.exe"))
    found = shutil.which(kind + (".exe" if os.name == "nt" else ""))
    if found:
        candidates.insert(0, Path(found))
    for p in candidates:
        if p.is_file():
            return str(p.resolve())
    raise DispatchError("未找到 " + kind + " 原生程序；用 PERSONAL_DISPATCH_" + kind.upper() + " 指定。旧 Herdr TUI 不提供 hc")


def program_argv(agent):
    app_home = Path(os.environ.get("HERDR_APP_HOME", str(Path.home() / ".config/herdr-app")))
    config = app_home / "programs.json"
    if config.is_file():
        argv = load(config).get("programs", {}).get(agent, {}).get("run")
        if isinstance(argv, list) and argv and all(isinstance(v, str) for v in argv):
            if Path(argv[0]).is_file() and Path(argv[0]).suffix.lower() not in {".cmd", ".bat", ".ps1"}:
                return argv[:]
    return [executable(agent)]


def deepseek_tui_argv():
    """Resolve the installed native dsh-tui entry point without a PowerShell shim."""
    appdata = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))
    npm = appdata / "npm/node_modules/@deepseek-harness-tui/dsh-tui/bin/dsh-tui.js"
    script = Path(os.environ.get("PERSONAL_DISPATCH_DSH_TUI", str(npm))).resolve()
    if not script.is_file():
        raise DispatchError("未找到 dsh-tui；用 PERSONAL_DISPATCH_DSH_TUI 指定 dsh-tui.js")
    return [executable("node"), str(script)]


def name_app_tab(hc, r, j):
    t = hc.owned(j["address"], r["id"], j["token"])
    tab = t.get("labels", {}).get("herdr.tab")
    if not tab:
        return
    app = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "Programs/herdr-app/herdr-app.exe"
    exe = str(app) if app.is_file() else shutil.which("herdr-app")
    if exe:
        subprocess.run([exe, "do", "tab.rename", "--id", tab, "--name", json.loads(j["spec"])["agent"].title() + " · " + j["id"]], capture_output=True, timeout=10)


class Herdr:
    def __init__(self):
        self.exe = executable("hc")

    def call(self, *args, timeout=30):
        p = subprocess.run([self.exe, *map(str, args)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        if p.returncode:
            raise DispatchError((p.stderr or p.stdout).strip()[:1500])
        try:
            return json.loads(p.stdout)
        except json.JSONDecodeError:
            raise DispatchError("hc 未返回 JSON: " + p.stdout[:300])

    def get(self, address):
        return self.call("get", address, "--json")

    def owned(self, address, run_id, token):
        t = self.get(address)
        labels = t.get("labels", {})
        if labels.get("dispatch.run") != run_id or labels.get("dispatch.token") != token:
            raise DispatchError("Herdr 终端身份不匹配，保留现场: " + address)
        return t

    def app_space(self, address):
        visited = set()
        while address and address not in visited:
            visited.add(address)
            try:
                terminal = self.get(address)
            except DispatchError:
                if len(visited) == 1:
                    raise
                return None
            space = terminal.get("labels", {}).get("herdr.space")
            if space:
                return space
            address = terminal.get("parent")
        return None

    def start(self, r, name, token, argv, role):
        labels = ["dispatch.run=" + r["id"], "dispatch.token=" + token, "dispatch.role=" + role]
        if role in {"worker", "controller"}:
            # The app adopts labeled terminals even if the controller has no visible ancestor.
            space = self.app_space(r["controller"]) or "pdspace-" + r["id"]
            labels += ["herdr.space=" + space, "herdr.tab=pdtab-" + token[:16]]
        args = ["run", "--bg", "--json", "--name", name, "--cwd", r["root"], "--outlive-parent", "--no-notify"]
        args += ["--parent", r["controller"]] if role in {"worker", "controller"} else ["--no-parent"]
        for label in labels:
            args += ["--label", label]
        result = self.call(*args, *argv)
        if not result.get("address"):
            raise DispatchError("hc launch 缺少 address")
        return result

    def send(self, address, text, *, agent=None, labels=None):
        if not text.strip():
            raise DispatchError("消息为空")
        target = self.get(address)
        address = target["address"]
        lock_id = hashlib.sha256(f"{Path.home()}:{address}:{target.get('created_ms')}".encode()).hexdigest()
        path = Path(tempfile.gettempdir()) / ("herdr-dispatch-" + lock_id + ".lock")
        with file_lock(path, "该终端正在接收另一条调度消息", wait=75):
            current = self.get(address)
            require_message_target(current, target, labels)
            agent = terminal_agent(current) or agent
            if not agent:
                raise DispatchError("目标终端没有报告 Codex、Claude 或 DeepSeek 状态")
            message = f"[dispatch:{uuid.uuid4().hex[:8]}] {text}"
            receipt = self.call("send", address, message, "--json", timeout=75)

            # An already working agent can return started=true with the text still in its composer.
            time.sleep(0.3)
            screen = self.call("read", address, "--lines", "0", "--json")["text"]
            if message_in_composer(screen, agent, message):
                current = self.get(address)
                require_message_target(current, target, labels)
                self.call("keys", address, current.get("submit") or "enter", "--json")
                time.sleep(0.3)
                screen = self.call("read", address, "--lines", "0", "--json")["text"]
                if message_in_composer(screen, agent, message):
                    raise DispatchError("消息仍在输入框，请检查目标终端")
                return {**receipt, "resubmitted": True}
            if receipt.get("started") is False:
                raise DispatchError("Herdr 未观察到消息开始处理，请检查目标终端")
            return {**receipt, "resubmitted": False}

    def find_owned(self, run_id, token):
        matches = []
        for item in self.call("ls", "--all", "--json").get("terminals", []):
            t = self.get(item["address"])
            labels = t.get("labels", {})
            if labels.get("dispatch.run") == run_id and labels.get("dispatch.token") == token:
                matches.append(t)
        if len(matches) > 1:
            raise DispatchError("同一 token 有多个终端，保留现场，不自动重发")
        return matches[0] if matches else None


def status_word(t):
    value = t.get("status")
    return value.get("state") if isinstance(value, dict) else value


def terminal_agent(t):
    status = t.get("status")
    app = status.get("app") if isinstance(status, dict) else None
    return {"codex": "codex", "claude": "claude", "claude-code": "claude", "dsh-tui": "deepseek"}.get(app)


def require_message_target(current, target, labels):
    actual = current.get("labels", {})
    if (current["state"] != "live" or current.get("created_ms") != target.get("created_ms")
            or current.get("pid") != target.get("pid")
            or any(actual.get(key) != value for key, value in (labels or {}).items())):
        raise DispatchError("目标 Herdr 终端已变更")
    if status_word(current) == "blocked":
        raise DispatchError("目标代理需要处理当前提示，消息暂未提交")
    if status_word(current) not in {"idle", "done", "working", "error"}:
        raise DispatchError("目标代理尚未报告可接收消息的状态")


def message_in_composer(screen, agent, message):
    if agent == "codex":
        prompts = list(re.finditer(r"(?m)^[ \t]*›[ \t]+", screen))
        if not prompts:
            return False
        sections = re.split(r"\n(?:[ \t]*\n)+", screen[prompts[-1].end():].rstrip())
        if len(sections) != 2 or not sections[1].startswith("  "):
            return False
        draft = sections[0]
    else:
        # Claude uses horizontal rules. DeepSeek frames its composer and adds an expand glyph.
        if agent == "claude":
            pattern = r"^[ \t]*─{3,}[ \t]*\n[ \t]*❯[^\S\r\n]+(.*?)\n[ \t]*─{3,}[ \t]*$"
        else:
            pattern = r"^[ \t]*╭─{3,}╮[ \t]*\n[ \t]*⌸[ \t]+❯[ \t]+(.*?)\n[ \t]*╰─{3,}╯[ \t]*$"
        frames = list(re.finditer(pattern, screen, re.MULTILINE | re.DOTALL))
        if not frames:
            return False
        draft = frames[-1].group(1)
        if agent == "deepseek":
            draft = re.sub(r"[ \t]+⛶(?=[ \t]*(?:\n|$))", "", draft, count=1)
    draft = re.sub(r"\s+", "", draft)
    # hc may wrap messages sent from another agent in a sender tag.
    tagged = re.fullmatch(r'<hcfrom="[^"]+">(.*)</hc>', draft)
    return (tagged.group(1) if tagged else draft) == re.sub(r"\s+", "", message)


def require_controller(hc, r=None):
    current = os.environ.get("HC_TERMINAL_ID")
    if not current:
        raise DispatchError("请在 Herdr App/core 的 Codex、Claude 或 DeepSeek 终端内运行")
    t = hc.get(current)
    if t.get("state") != "live":
        raise DispatchError("主控 Herdr 终端不在线")
    if r is not None and (current != r["controller"] or t.get("labels", {}).get("dispatch.controller") != r["id"]):
        raise DispatchError("只有登记的主控可以派发/验收；新的主控先 resume")
    return current


def controller_label(hc, controller, run_id):
    hc.call("label", controller, "dispatch.controller=" + run_id, "--json")


def controller_available(store, current, except_run=None):
    row = store.db.execute("SELECT id FROM runs WHERE controller=? AND status IN ('active','paused') AND id != ?", (current, except_run or "")).fetchone()
    if row:
        raise DispatchError("当前主控已有任务 " + row[0] + "；在另一个 Herdr 主控处理新 run")


def run_lock(run_id):
    return file_lock(run_dir(run_id) / "sweep.lock", "该 run 正在由另一个监督操作处理，稍后重试")


@contextlib.contextmanager
def file_lock(path, contention, *, wait=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as f:
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        deadline = time.monotonic() + wait
        while True:
            f.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise DispatchError(contention)
                time.sleep(0.1)
        try:
            yield
        finally:
            f.seek(0)
            if os.name == "nt":
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def init_run(store, hc, root, plan, agent, lanes):
    root = Path(root).resolve()
    if not root.is_dir():
        raise DispatchError("工作区不存在")
    plan = validate_plan(plan, root, agent)
    if not 1 <= lanes <= 8:
        raise DispatchError("并发范围 1-8，默认 2")
    controller = require_controller(hc)
    controller_available(store, controller)
    current = hc.get(controller).get("labels", {}).get("dispatch.controller")
    if current:
        existing = store.db.execute("SELECT status FROM runs WHERE id=?", (current,)).fetchone()
        if existing and existing[0] in {"active", "paused"}:
            raise DispatchError("当前主控已有任务 " + current + "；先继续它，或在另一 Herdr 终端新建")
    for kind in {j["agent"] for j in plan["jobs"]}:
        deepseek_tui_argv() if kind == "deepseek" else executable(kind)
    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    run_dir(run_id).mkdir(parents=True)
    atomic(run_dir(run_id) / "plan.json", plan)
    store.db.execute("INSERT INTO runs(id,root,objective,agent,lanes,controller,status,created) VALUES(?,?,?,?,?,?,?,?)", (run_id, str(root), plan["objective"], agent, lanes, controller, "active", now()))
    for j in plan["jobs"]:
        store.db.execute("INSERT INTO jobs(run_id,id,spec) VALUES(?,?,?)", (run_id, j["id"], json.dumps(j, ensure_ascii=False)))
        job_dir(run_id, j["id"]).mkdir(parents=True)
    store.db.commit()
    controller_label(hc, controller, run_id)
    return store.snapshot(run_id)


def reserve(store, r, job):
    store.db.execute("BEGIN IMMEDIATE")
    try:
        latest = store.job(r["id"], job["id"])
        if latest["status"] != "pending" or store.run(r["id"])["status"] != "active":
            store.db.rollback()
            return False
        jobs = store.jobs(r["id"])
        if sum(j["status"] in {"starting", "running"} for j in jobs) >= r["lanes"]:
            store.db.rollback()
            return False
        spec = json.loads(job["spec"])
        phases = {j["id"]: j["status"] for j in jobs}
        if any(phases.get(d) != "complete" for d in spec["depends_on"]):
            store.db.rollback()
            return False
        for other in store.db.execute("SELECT spec,status FROM jobs"):
            if other["status"] in LOCKED and conflict(spec, json.loads(other["spec"])):
                store.db.rollback()
                return False
        store.db.execute("UPDATE jobs SET status='starting',token=?,sent=? WHERE run_id=? AND id=?", (uuid.uuid4().hex, now(), r["id"], job["id"]))
        store.db.commit()
        return True
    except Exception:
        store.db.rollback()
        raise


def brief(r, j):
    task = job_dir(r["id"], j["id"])
    spec = json.loads(j["spec"])
    atomic(task / "manifest.json", {"run": {"id": r["id"], "root": r["root"]}, "job_id": j["id"], "spec": spec})
    content = "# Herdr 任务\n\n" + json.dumps({"objective": r["objective"], **spec}, ensure_ascii=False, indent=2)
    content += f"\n\n工作目录必须是 {r['root']}。任务数据目录 {task}。先读取 {SKILL / 'references/personal.md'}，以及目标目录适用的 AGENTS.md/CLAUDE.md。主控为 {r['controller']}。\n"
    content += "只修改 writes 范围内的文件；reads 只读。保留用户已有改动。先逐个确定文件并用下面的 backup 保存原字节，再修改；新增文件也登记。专业技能的原生验证条件继续适用。不会进行 Git/版本控制提交、push、提审、部署、发送消息或再派代理。主控负责这些动作。\n"
    backup_argv = [sys.executable, str(SCRIPT), 'backup', '--data-home', str(home()), '--run', r['id'], '--job', j['id'], '--file', '要修改的相对文件']
    command = "& " + " ".join("'" + a.replace("'", "''") + "'" for a in backup_argv) if os.name == "nt" else shlex.join(backup_argv)
    content += "备份命令（替换最后的相对文件，路径保持原样）:\n```" + ("powershell" if os.name == "nt" else "sh") + "\n" + command + "\n```\n"
    content += f"每一步更新 {task / 'progress.md'}（已做、剩余、实际文件、检查、问题）。有疑问先做独立项，再向主控报告。停下之前原子写 result.json（临时文件再替换），格式：\n"
    content += json.dumps({"attempt": j["attempt"], "status": "done | blocked | needs_host", "summary": "实际结果或准确阻碍", "changed_files": [], "evidence": ["真实文件/检查证据"], "question": "没有问题时为空字符串"}, ensure_ascii=False, indent=2)
    content += "\nchanged_files 使用工作区相对文件路径。只读任务写出的报告在任务数据目录，不作为工作区改动。done 仅是待主控验收声明。遇到没有的工具/连接用 needs_host，不能宣称原生验证已通过。\n"
    content += "写好 result.json 后结束当前轮。调度守望会把报告通知主控。主控可发来反馈让你继续。\n"
    deps = spec["depends_on"]
    if deps:
        content += "\n依赖任务产物:\n" + "\n".join(str(job_dir(r["id"], d)) for d in deps) + "\n"
    (task / "TASK.md").write_text(content, encoding="utf-8")
    return f"读取 {task / 'TASK.md'}，按任务范围自主执行到完成或明确阻碍。直接开始，不请求常规计划确认。"


def agent_argv(agent, task, prompt, root=None):
    if agent == "codex":
        base = program_argv(agent)
        is_bridge = any("codex-bridge" in item and item.endswith("launch.py") for item in base)
        args = base + ([] if is_bridge or "--no-daemon" in base else ["--no-daemon"]) + ["--no-alt-screen", "--sandbox", "workspace-write", "-c", 'approval_policy="never"', "--add-dir", str(task)]
        if root:
            args += ["-c", "projects." + json.dumps(str(root)) + '.trust_level="trusted"']
        return args + [prompt]
    if agent == "deepseek":
        args = deepseek_tui_argv() + ["--backend", "dsh"]
        if root:
            args.append(str(root))
        return args + ["--", prompt]
    return program_argv(agent) + ["--permission-mode", "auto", "--add-dir", str(task), "--", prompt]


def settle_trust(hc, r, j, terminal):
    """Acknowledge only Claude's first trust prompt for the authorized exact cwd."""
    if json.loads(j["spec"])["agent"] != "claude" or now() - j["sent"] > 180 or terminal["state"] != "live":
        return False
    if status_word(terminal) == "working":
        return False
    view = subprocess.run([hc.exe, "read", j["address"], "--lines", "0"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15).stdout
    if ("Quick safety check: Is this a project you created or one you trust?" in view
        and r["root"] in view and "Yes, I trust this folder" in view and "> No, exit" in view):
        hc.call("keys", j["address"], "down", "enter", "--json")
        return True
    return False


def launch_job(store, hc, r, j):
    prompt = brief(r, j)
    try:
        name = "pd-" + r["id"][-6:] + "-" + j["id"][:15] + "-" + j["token"][:4]
        t = hc.start(r, name, j["token"], agent_argv(json.loads(j["spec"])["agent"], job_dir(r["id"], j["id"]), prompt, r["root"]), "worker")
        store.update("jobs", {"run_id": r["id"], "id": j["id"]}, address=t["address"], status="running", baseline_turn=t.get("turn", 0), sent=now(), note="")
        if hasattr(hc, "exe"):
            name_app_tab(hc, r, store.job(r["id"], j["id"]))
    except Exception as e:
        # A launch may have succeeded before a timeout. Keep the reservation and reconcile labels.
        store.update("jobs", {"run_id": r["id"], "id": j["id"]}, status="starting", note=str(e)[:1500])


def report_for(r, j):
    path = job_dir(r["id"], j["id"]) / "result.json"
    if not path.exists():
        return None
    try:
        result = load(path)
    except (ValueError, OSError):
        return None
    if not isinstance(result, dict) or result.get("attempt") != j["attempt"]:
        return None
    if result.get("status") not in {"done", "blocked", "needs_host"} or not isinstance(result.get("summary"), str) or not result["summary"].strip():
        raise DispatchError("result 字段不完整")
    files = result.get("changed_files", [])
    if not isinstance(files, list):
        raise DispatchError("changed_files 必须为列表")
    spec = json.loads(j["spec"])
    for f in files:
        p = resolve_scope(Path(r["root"]), f)
        if not any(contained(Path(p), Path(scope)) for scope in spec["write_paths"]):
            raise DispatchError("报告修改超出 writes: " + str(f))
    return result


def send_to_worker(hc, r, j, text):
    return hc.send(j["address"], text, agent=json.loads(j["spec"])["agent"],
                   labels={"dispatch.run": r["id"], "dispatch.token": j["token"]})


def reconcile_job(store, hc, r, j):
    if j["status"] not in {"starting", "running", "blocked"}:
        return
    if not j["address"]:
        listed = hc.call("ls", "--all", "--json").get("terminals", [])
        matches = []
        for t in listed:
            # ls does not include labels on all versions; confirm with get.
            t = hc.get(t["address"])
            labels = t.get("labels", {})
            if labels.get("dispatch.run") == r["id"] and labels.get("dispatch.token") == j["token"]:
                matches.append(t)
        if len(matches) == 1:
            store.update("jobs", {"run_id": r["id"], "id": j["id"]}, address=matches[0]["address"], status="running")
            j = store.job(r["id"], j["id"])
        elif now() - j["sent"] > 120:
            store.update("jobs", {"run_id": r["id"], "id": j["id"]}, status="needs_user", note="启动未能确认；保留锁，主控检查现场后 feedback 重试")
            return
        else:
            return
    try:
        t = hc.owned(j["address"], r["id"], j["token"])
        if hasattr(hc, "exe"):
            name_app_tab(hc, r, j)
        if hasattr(hc, "exe") and settle_trust(hc, r, j, t):
            return
        word = status_word(t)
        result = report_for(r, j)
        if result and word != "working":
            phase = "review" if result["status"] == "done" else "blocked"
            store.update("jobs", {"run_id": r["id"], "id": j["id"]}, status=phase, note=result["summary"])
        elif t["state"] != "live":
            store.update("jobs", {"run_id": r["id"], "id": j["id"]}, status="blocked", note="执行代理已退出；读进度后 feedback 可恢复")
        elif word in {"blocked", "error"}:
            store.update("jobs", {"run_id": r["id"], "id": j["id"]}, status="blocked", note="代理状态 " + word + "；主控读取终端与进度解决")
        elif word in {"done", "idle"} and now() - j["sent"] > 90 and j["status"] == "running":
            if j["nudges"] >= 3:
                store.update("jobs", {"run_id": r["id"], "id": j["id"]}, status="blocked", note="三次续跑后仍无有效报告；主控诊断")
            else:
                send_to_worker(hc, r, j, "继续读取 TASK.md 和 progress.md 完成剩余项；完成或受阻时写匹配当前 attempt 的 result.json。")
                store.update("jobs", {"run_id": r["id"], "id": j["id"]}, nudges=j["nudges"] + 1, sent=now(), baseline_turn=t.get("turn", 0))
    except (DispatchError, subprocess.TimeoutExpired) as e:
        store.update("jobs", {"run_id": r["id"], "id": j["id"]}, status="blocked", note=str(e)[:1500])


def ensure_watcher(store, hc, r):
    if r["watcher"]:
        try:
            if hc.owned(r["watcher"], r["id"], r["watcher_token"])["state"] == "live":
                return
        except DispatchError:
            pass
    token = uuid.uuid4().hex
    t = hc.start(r, "pd-" + r["id"][-6:] + "-watch-" + token[:4], token, [sys.executable, str(SCRIPT), "watch", "--data-home", str(home()), "--run", r["id"]], "watcher")
    store.update("runs", {"id": r["id"]}, watcher=t["address"], watcher_token=token)


def sweep(store, hc, ref):
    r = store.run(ref)
    require_controller(hc, r)
    with run_lock(r["id"]):
        r = store.run(r["id"])
        require_controller(hc, r)
        if r["status"] != "active":
            return store.snapshot(r["id"])
        for j in store.jobs(r["id"]):
            reconcile_job(store, hc, r, j)
        for j in store.jobs(r["id"]):
            if j["status"] == "pending" and reserve(store, r, j):
                launch_job(store, hc, r, store.job(r["id"], j["id"]))
        if all(j["status"] == "complete" for j in store.jobs(r["id"])):
            store.update("runs", {"id": r["id"]}, status="complete", note="全部任务已由主控验收")
        else:
            ensure_watcher(store, hc, store.run(r["id"]))
        return store.snapshot(r["id"])


def backup(store, r, job_id, files, spec_override=None):
    spec = spec_override or json.loads(store.job(r["id"], job_id)["spec"])
    dest = job_dir(r["id"], job_id) / "backups"
    for f in files:
        p = (Path(r["root"]) / f).resolve()
        if not contained(p, Path(r["root"])) or not any(contained(p, Path(s)) for s in spec["write_paths"]):
            raise DispatchError("备份文件不在本任务 writes 中")
        key = hashlib.sha256(os.path.normcase(str(p)).encode()).hexdigest()
        target = dest / key
        target.mkdir(parents=True, exist_ok=True)
        if (target / "meta.json").exists():
            continue
        if p.exists() and not p.is_file():
            raise DispatchError("请逐个备份文件，不备份目录")
        meta = {"path": str(p), "existed": p.exists(), "sha256": digest(p) if p.exists() else None}
        if p.exists():
            shutil.copy2(p, target / "original")
        atomic(target / "meta.json", meta)
    return {"backup_dir": str(dest)}


def start_checks(store, hc, r, job_id):
    with run_lock(r["id"]):
        return _start_checks_locked(store, hc, store.run(r["id"]), job_id)


def _start_checks_locked(store, hc, r, job_id):
    require_controller(hc, r)
    j = store.job(r["id"], job_id)
    if j["status"] != "review" or r["status"] != "active":
        raise DispatchError("只对 active run 中待验收的任务运行检查")
    task = job_dir(r["id"], job_id)
    running = task / "check-terminal.json"
    if running.exists():
        prev = load(running)
        if prev.get("attempt") == j["attempt"]:
            t = hc.owned(prev["address"], r["id"], prev["token"]) if prev.get("address") else hc.find_owned(r["id"], prev["token"])
            if t:
                prev["address"] = t["address"]
                atomic(running, prev)
                if t["state"] == "live":
                    return prev
            elif prev.get("status") == "starting":
                raise DispatchError("检查启动尚未确认，不重复创建；请检查 token=" + prev["token"])
    token = uuid.uuid4().hex
    info = {"address": None, "token": token, "attempt": j["attempt"], "status": "starting", "started": now()}
    atomic(running, info)
    name = "pd-" + r["id"][-6:] + "-c-" + job_id[:10] + "-" + token[:4]
    t = hc.start(r, name, token, [sys.executable, str(SCRIPT), "run-checks", "--data-home", str(home()), "--run", r["id"], "--job", job_id, "--attempt", str(j["attempt"])], "check")
    info.update(address=t["address"], status="running")
    atomic(running, info)
    return info


def run_checks(store, r, job_id, attempt):
    j = store.job(r["id"], job_id)
    if j["attempt"] != attempt or j["status"] != "review" or r["status"] != "active":
        raise DispatchError("检查任务状态已改变")
    out = []
    claim = report_for(r, j)
    if not claim or claim["status"] != "done":
        raise DispatchError("检查前需要当前 done 报告")
    def file_hashes():
        return {f: digest(Path(r["root"]) / f) if (Path(r["root"]) / f).is_file() else None for f in claim.get("changed_files", [])}
    before = file_hashes()
    task = job_dir(r["id"], job_id)
    for n, c in enumerate(json.loads(j["spec"])["checks"]):
        argv = c["argv"][:]
        if argv[0] in {"python", "python3"}:
            argv[0] = sys.executable
        resolved_exe = shutil.which(argv[0]) or argv[0]
        if Path(resolved_exe).suffix.lower() in {".cmd", ".bat", ".ps1"}:
            raise DispatchError("check 请用原生 exe 或解释器 argv，避免 shell shim")
        log = task / f"check-{attempt}-{n}.log"
        code, error = None, ""
        with log.open("w", encoding="utf-8") as stream:
            try:
                p = subprocess.run(argv, cwd=c["cwd_path"], stdout=stream, stderr=subprocess.STDOUT, timeout=c.get("timeout", 300), shell=False)
                code = p.returncode
            except (OSError, subprocess.TimeoutExpired) as e:
                error = str(e)
                stream.write("\n" + error)
        out.append({"argv": c["argv"], "returncode": code, "error": error, "log": str(log)})
    after = file_hashes()
    result = {"attempt": attempt, "passed": all(c["returncode"] == 0 for c in out) and before == after, "checks": out, "hashes": after, "source_changed_during_check": before != after, "finished": now()}
    atomic(task / "checks.json", result)
    return result


def accept(store, hc, r, job_id, evidence):
    require_controller(hc, r)
    with run_lock(r["id"]):
        r = store.run(r["id"])
        require_controller(hc, r)
        j = store.job(r["id"], job_id)
        if r["status"] != "active" or j["status"] != "review":
            raise DispatchError("只能验收 active run 的 review 任务")
        t = hc.owned(j["address"], r["id"], j["token"])
        if status_word(t) == "working":
            raise DispatchError("执行代理仍在工作，先等待其停止")
        result = report_for(r, j)
        if not result or result["status"] != "done":
            raise DispatchError("缺少当前 attempt 的 done 报告")
        evidence = Path(evidence).resolve()
        if not evidence.is_file() or not evidence.read_text(encoding="utf-8-sig").strip():
            raise DispatchError("需要主控实际验收证据文件")
        task = job_dir(r["id"], job_id)
        spec = json.loads(j["spec"])
        if spec["checks"]:
            check = load(task / "checks.json") if (task / "checks.json").exists() else {}
            if check.get("attempt") != j["attempt"] or not check.get("passed"):
                raise DispatchError("登记检查尚未实际通过")
        records = {}
        for f in result.get("changed_files", []):
            p = (Path(r["root"]) / f).resolve()
            key = hashlib.sha256(os.path.normcase(str(p)).encode()).hexdigest()
            if not (task / "backups" / key / "meta.json").exists():
                raise DispatchError("报告中的文件缺少修改前备份登记: " + f)
            records[f] = digest(p) if p.is_file() else None
        if spec["checks"] and check.get("hashes") != records:
            raise DispatchError("源文件在检查后已改变，请重新检查和验收")
        shutil.copyfile(evidence, task / "accepted-evidence.md") if evidence != task / "accepted-evidence.md" else None
        atomic(task / "accepted.json", {"attempt": j["attempt"], "controller": r["controller"], "accepted": now(), "hashes": records, "evidence_sha256": digest(evidence)})
        store.update("jobs", {"run_id": r["id"], "id": job_id}, status="complete", note=result["summary"])
        return store.snapshot(r["id"])


def feedback(store, hc, r, job_id, text_file):
    require_controller(hc, r)
    if r["status"] != "active":
        raise DispatchError("run 未 active，先 resume")
    with run_lock(r["id"]):
        r = store.run(r["id"])
        require_controller(hc, r)
        if r["status"] != "active":
            raise DispatchError("run 未 active，先 resume")
        j = store.job(r["id"], job_id)
        if j["status"] not in {"review", "blocked", "needs_user", "running"}:
            raise DispatchError("该任务状态不能反馈")
        text = Path(text_file).read_text(encoding="utf-8-sig").strip()
        if not text:
            raise DispatchError("反馈为空")
        t = hc.owned(j["address"], r["id"], j["token"]) if j["address"] else None
        if t and status_word(t) == "working":
            raise DispatchError("代理仍在工作，先等待或明确中断")
        attempt = j["attempt"] + 1
        (job_dir(r["id"], job_id) / f"feedback-{attempt}.md").write_text(text, encoding="utf-8")
        store.update("jobs", {"run_id": r["id"], "id": job_id}, attempt=attempt, nudges=0, status="running", sent=now(), note="")
        j = store.job(r["id"], job_id)
        prompt = brief(r, j) + f" 先读取 feedback-{attempt}.md，按主控反馈继续。"
        if t and t["state"] == "live":
            try:
                send_to_worker(hc, r, j, prompt)
            except Exception as e:
                store.update("jobs", {"run_id": r["id"], "id": job_id}, status="blocked", note=str(e)[:1500])
                raise
        else:
            store.update("jobs", {"run_id": r["id"], "id": job_id}, token=uuid.uuid4().hex, address=None, status="starting")
            launch_job(store, hc, r, store.job(r["id"], job_id))
        return store.snapshot(r["id"])


def pause(store, hc, r):
    require_controller(hc, r)
    with run_lock(r["id"]):
        r = store.run(r["id"])
        require_controller(hc, r)
        store.update("runs", {"id": r["id"]}, status="paused", note="用户暂停，保留任务和锁")
        for j in store.jobs(r["id"]):
            if j["address"] and j["status"] in LOCKED:
                t = hc.owned(j["address"], r["id"], j["token"])
                if t["state"] == "live" and status_word(t) == "working":
                    hc.call("interrupt", j["address"], "--json")
                if j["status"] in {"starting", "running"}:
                    store.update("jobs", {"run_id": r["id"], "id": j["id"]}, status="paused")
            check_file = job_dir(r["id"], j["id"]) / "check-terminal.json"
            if check_file.exists():
                info = load(check_file)
                t = hc.owned(info["address"], r["id"], info["token"]) if info.get("address") else hc.find_owned(r["id"], info["token"])
                if t and t["state"] == "live":
                    hc.call("interrupt", info["address"], "--json")
        return store.snapshot(r["id"])


def resume(store, hc, r):
    current = require_controller(hc)
    with run_lock(r["id"]):
        r = store.run(r["id"])
        controller_available(store, current, r["id"])
        if r["status"] == "complete":
            return store.snapshot(r["id"])
        if current != r["controller"]:
            try:
                old = hc.get(r["controller"])
                if old["state"] == "live" and old.get("labels", {}).get("dispatch.controller") == r["id"]:
                    raise DispatchError("旧主控仍在线；请在原主控 resume，或明确结束旧主控后接管")
            except DispatchError as e:
                if "旧主控仍在线" in str(e):
                    raise
        controller_label(hc, current, r["id"])
        store.update("runs", {"id": r["id"]}, controller=current, status="active", note="")
        for j in store.jobs(r["id"]):
            if j["status"] == "paused":
                store.update("jobs", {"run_id": r["id"], "id": j["id"]}, status="running", sent=0)
        return store.snapshot(r["id"])


def watch(ref, interval=120):
    store, hc = Store(), Herdr()
    r = store.run(ref)
    events = subprocess.Popen([hc.exe, "events", "--json", "--label", "dispatch.run=" + r["id"]], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
    messages = queue.Queue()
    def read_events():
        for line in events.stdout:
            messages.put(line)
    threading.Thread(target=read_events, daemon=True).start()
    last = now()
    announced = set()
    print("\033]7501;state=working:app=dispatch\033\\", end="", flush=True)
    try:
        while True:
            changed = False
            try:
                messages.get(timeout=min(interval, 15))
                changed = True
                while not messages.empty():
                    messages.get_nowait()
            except queue.Empty:
                pass
            r = store.run(ref)
            if r["status"] in {"complete", "paused"}:
                return
            if r["watcher"] and r["watcher"] != os.environ.get("HC_TERMINAL_ID"):
                return
            if events.poll() is not None:
                store.update("runs", {"id": r["id"]}, note="Herdr 事件流断开；主控 sweep 会重新启动守望")
                return
            jobs = store.jobs(r["id"])
            reports = set()
            for j in jobs:
                key = (j["id"], j["attempt"])
                if key in announced or not j["address"] or j["status"] not in {"running", "blocked"}:
                    continue
                try:
                    report = report_for(r, j)
                    if report and (j["status"] == "running" or j["note"] != report["summary"]):
                        worker = hc.owned(j["address"], r["id"], j["token"])
                        if status_word(worker) != "working":
                            reports.add(key)
                except (DispatchError, subprocess.TimeoutExpired):
                    # sweep checks invalid reports and terminal identity.
                    continue
            if not reports and now() - last < interval:
                continue
            try:
                t = hc.get(r["controller"])
                if t["state"] == "live" and t.get("labels", {}).get("dispatch.controller") == r["id"]:
                    if t.get("labels", {}).get("dispatch.role") == "controller" and r["agent"] == "claude":
                        pseudo = {"spec": json.dumps({"agent": "claude"}), "address": r["controller"], "sent": t.get("created_ms", 0) / 1000}
                        if settle_trust(hc, r, pseudo, t):
                            last = now()
                            continue
                    # Human decisions remain parked; unrelated jobs may continue.
                    phases = {j["id"]: j["status"] for j in jobs}
                    actionable = any(j["status"] in {"starting", "running", "review"} or (j["status"] == "pending" and all(phases[d] == "complete" for d in json.loads(j["spec"])["depends_on"])) for j in jobs)
                    if status_word(t) in {"blocked", "error"}:
                        continue
                    if reports or actionable or changed:
                        # A timed-out send may have typed the message. Leave retries to the periodic reminder.
                        announced.update(reports)
                        report_note = "已收到任务报告：" + "、".join(job for job, _ in sorted(reports)) + "。" if reports else ""
                        notice = f"[个人调度监督] run={r['id']}。{report_note}读取 $dispatch / /herdr-personal-dispatch:dispatch，执行 sweep --data-home {home()} --run {r['id']}，核对实际结果并自主继续。"
                        hc.send(r["controller"], notice, agent=r["agent"], labels={"dispatch.controller": r["id"]})
                    last = now()
                elif t["state"] == "finished":
                    if any(j["status"] in {"needs_user", "blocked"} for j in jobs) or r["restarts"] >= 3:
                        store.update("runs", {"id": r["id"]}, note="主控已退出且需要用户/已达恢复上限；在 Herdr 新主控 resume")
                        return
                    token = uuid.uuid4().hex
                    prompt = f"恢复个人 Herdr 调度 run={r['id']}。读取 {SKILL / 'SKILL.md'}。原主控已退出，调用脚本 resume 和 sweep 时带 --data-home {home()}。读取磁盘记录，自主完成原目标；已有产物先验收，不重复修改。"
                    new = hc.start(r, "pd-" + r["id"][-6:] + "-lead-" + str(r["restarts"] + 1), token, agent_argv(r["agent"], home(), prompt, r["root"]), "controller")
                    controller_label(hc, new["address"], r["id"])
                    store.update("runs", {"id": r["id"]}, controller=new["address"], restarts=r["restarts"] + 1, note="自动恢复主控")
                    last = now()
                else:
                    store.update("runs", {"id": r["id"]}, note="主控身份变更，停止自动输入；请人工 resume")
                    return
            except (DispatchError, subprocess.TimeoutExpired) as e:
                # Permission dialogs reject send; do not force keys or spin.
                print("监督通知暂未送达: " + str(e)[:200], flush=True)
                last = now()
    finally:
        events.terminate()
        try:
            events.wait(timeout=5)
        except subprocess.TimeoutExpired:
            events.kill()
        print("\033]7501;state=done:app=dispatch\033\\", end="", flush=True)


def doctor():
    result = {"backend": "herdr-core", "terminal": os.environ.get("HC_TERMINAL_ID"), "data_dir": str(home()), "tools": {}}
    for kind in ["hc", "codex", "claude"]:
        try:
            exe = executable(kind)
            p = subprocess.run([exe, "--version"], capture_output=True, text=True, encoding="utf-8", timeout=10)
            result["tools"][kind] = {"path": exe, "version": p.stdout.strip()}
            if kind in {"codex", "claude"}:
                result["tools"][kind]["launcher"] = program_argv(kind)
        except (DispatchError, subprocess.TimeoutExpired) as e:
            result["tools"][kind] = {"error": str(e)}
    try:
        argv = deepseek_tui_argv()
        p = subprocess.run(argv + ["version"], capture_output=True, text=True, encoding="utf-8", timeout=10)
        result["tools"]["deepseek"] = {"path": argv[-1], "launcher": argv, "version": p.stdout.strip(), "backend": "dsh"}
    except (DispatchError, subprocess.TimeoutExpired) as e:
        result["tools"]["deepseek"] = {"error": str(e)}
    try:
        result["inside_herdr"] = bool(require_controller(Herdr()))
    except DispatchError as e:
        result["inside_herdr"] = False
        result["reason"] = str(e)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    p = sub.add_parser("send", help="向 Herdr 中的 Codex、Claude 或 DeepSeek 发送消息")
    p.add_argument("--to", required=True, metavar="TERMINAL")
    body = p.add_mutually_exclusive_group(required=True)
    body.add_argument("--text")
    body.add_argument("--text-file", help="让同一台机器上的代理读取消息文件")
    p = sub.add_parser("init")
    p.add_argument("--data-home")
    p.add_argument("--root", required=True)
    p.add_argument("--plan-file", required=True)
    p.add_argument("--agent", choices=["codex", "claude", "deepseek"], default="codex")
    p.add_argument("--lanes", type=int, default=2)
    for cmd in ["status", "sweep", "watch", "pause", "resume", "backup", "check", "run-checks", "accept", "feedback"]:
        p = sub.add_parser(cmd)
        p.add_argument("--data-home")
        p.add_argument("--run", required=True)
        if cmd in {"backup", "check", "run-checks", "accept", "feedback"}:
            p.add_argument("--job", required=True)
        if cmd == "backup":
            p.add_argument("--file", action="append", required=True)
        if cmd == "accept":
            p.add_argument("--evidence-file", required=True)
        if cmd == "feedback":
            p.add_argument("--text-file", required=True)
        if cmd == "run-checks":
            p.add_argument("--attempt", required=True, type=int)
    args = parser.parse_args()
    if getattr(args, "data_home", None):
        os.environ["PERSONAL_DISPATCH_HOME"] = str(Path(args.data_home).resolve())
    if args.command == "doctor":
        output = doctor()
    elif args.command == "send":
        message = args.text
        if args.text_file:
            path = Path(args.text_file).resolve(strict=True)
            if not path.is_file() or "/" in args.to or "\\" in args.to:
                raise DispatchError("消息文件必须是目标代理所在本机的文件")
            message = f"请读取消息文件 {json.dumps(str(path), ensure_ascii=False)} 并处理其中的消息。"
        output = Herdr().send(args.to, message)
    elif args.command == "backup":
        key = Path(args.run).name
        manifest = load(job_dir(key, args.job) / "manifest.json")
        if manifest.get("job_id") != args.job or manifest.get("run", {}).get("id") != key:
            raise DispatchError("未找到备份任务")
        output = backup(None, manifest["run"], args.job, args.file, manifest["spec"])
    else:
        store = Store()
        if args.command == "init":
            output = init_run(store, Herdr(), args.root, load(args.plan_file), args.agent, args.lanes)
        else:
            r = store.run(args.run)
            if args.command == "status":
                output = store.snapshot(r["id"])
            elif args.command == "watch":
                watch(r["id"])
                return
            elif args.command == "backup":
                output = backup(store, r, args.job, args.file)
            elif args.command == "run-checks":
                output = run_checks(store, r, args.job, args.attempt)
            elif args.command == "sweep":
                output = sweep(store, Herdr(), r["id"])
            elif args.command == "check":
                output = start_checks(store, Herdr(), r, args.job)
            elif args.command == "accept":
                output = accept(store, Herdr(), r, args.job, args.evidence_file)
            elif args.command == "feedback":
                output = feedback(store, Herdr(), r, args.job, args.text_file)
            elif args.command == "pause":
                output = pause(store, Herdr(), r)
            elif args.command == "resume":
                output = resume(store, Herdr(), r)
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (DispatchError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
