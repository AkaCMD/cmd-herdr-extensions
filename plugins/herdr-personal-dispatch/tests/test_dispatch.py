import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "skills/dispatch/scripts/dispatch.py"
spec = importlib.util.spec_from_file_location("dispatch", SCRIPT)
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


class FakeHerdr:
    def __init__(self):
        self.terminals = {"lead": {"address": "lead", "state": "live", "status": {"state": "working"}, "labels": {}, "turn": 0}}
        self.sent = []

    def get(self, address):
        if address not in self.terminals:
            raise d.DispatchError("not_found")
        return self.terminals[address]

    def call(self, *args, **kwargs):
        if args[0] == "label":
            for item in args[2:]:
                if "=" in item:
                    k, v = item.split("=", 1)
                    self.get(args[1])["labels"][k] = v
        elif args[0] == "interrupt":
            self.get(args[1])["status"] = {"state": "idle"}
            return {"outcome": "stopped"}
        elif args[0] == "kill":
            self.get(args[1])["state"] = "finished"
        elif args[0] == "keys":
            self.get(args[1])["last_keys"] = [a for a in args[2:] if a != "--json"]
        elif args[0] == "ls":
            return {"terminals": list(self.terminals.values())}
        return {}

    def owned(self, address, run_id, token):
        t = self.get(address)
        if t["labels"].get("dispatch.run") != run_id or t["labels"].get("dispatch.token") != token:
            raise d.DispatchError("identity mismatch")
        return t

    def start(self, r, name, token, argv, role):
        address = "t" + str(len(self.terminals))
        self.terminals[address] = {"address": address, "state": "live", "status": {"state": "working"}, "labels": {"dispatch.run": r["id"], "dispatch.token": token}, "turn": 0, "argv": argv}
        assert len(name) <= 32
        return self.terminals[address]

    def find_owned(self, run_id, token):
        return next((t for t in self.terminals.values() if t["labels"].get("dispatch.run") == run_id and t["labels"].get("dispatch.token") == token), None)

    def send(self, address, text, **kwargs):
        if d.status_word(self.get(address)) == "blocked":
            raise d.DispatchError("blocked")
        self.sent.append((address, text))
        self.get(address)["status"] = {"state": "working"}
        return {}


def job(key, writes=None, reads=None, deps=None, resources=None, checks=None):
    return {"id": key, "task": key, "agent": "codex", "writes": writes or [], "reads": reads or [], "depends_on": deps or [], "resources": resources or [], "checks": checks or [], "acceptance": ["真实结果正确"]}


class DeliveryTests(unittest.TestCase):
    def deliver(self, app, render, *, started=True, final_status="working", final_run="run", submit=None):
        hc = d.Herdr.__new__(d.Herdr)
        calls = []
        message = ""

        def call(command, address, *args, **kwargs):
            nonlocal message
            self.assertEqual(address, "t1")
            calls.append(command)
            if command == "get":
                return {
                    "address": "t1", "state": "live", "submit": submit,
                    "labels": {"dispatch.controller": final_run if message else "run"},
                    "status": {"app": app, "state": final_status if message else "idle"},
                    "turn": int(bool(message)),
                }
            if command == "send":
                message = args[0]
                return {"status": "idle", "started": started, "turn": 0}
            if command == "read":
                return {"text": "" if "keys" in calls else render(message)}
            if command == "keys":
                self.assertEqual(args, (submit or "enter", "--json"))
                return {"status": "working", "turn": 1}
            self.fail(command)

        hc.call = call
        with patch.object(d.time, "sleep"), patch.object(d, "file_lock", return_value=d.contextlib.nullcontext()):
            try:
                receipt = hc.send("t1", "DeepSeek 任务已报告，请读取结果。", labels={"dispatch.controller": "run"})
                return calls, receipt
            except d.DispatchError as exc:
                return calls, str(exc)

    def test_pause_during_native_send_never_resubmits_enter(self):
        hc = d.Herdr.__new__(d.Herdr)
        paused = False
        calls = []
        terminal = {"address": "t1", "state": "live", "status": {"state": "idle", "app": "codex"}, "labels": {}}
        def guard():
            if paused:
                raise d.DispatchPaused("paused")
        def call(command, *args, **kwargs):
            nonlocal paused
            calls.append(command)
            if command == "get":
                return terminal
            if command == "send":
                paused = True
                return {"started": False}
            self.fail("pause must prevent read/keys: " + command)
        hc.call = call
        with patch.object(d, "file_lock", return_value=d.contextlib.nullcontext()):
            with self.assertRaises(d.DispatchPaused):
                hc.send("t1", "continue", guard=guard)
        self.assertNotIn("keys", calls)

    def test_dropped_enter_is_recovered_for_each_agent(self):
        def codex(message):
            wrapped = message.replace("DeepSeek ", "DeepSeek\n  ")
            return f'历史消息\n\n› <hc from="t2 pd-watcher">{wrapped}</hc>\n\n\n  gpt-5.4'

        def claude(message):
            return f"──────────\n❯\u00a0{message}\n──────────\n  ← for agents"

        def deepseek(message):
            return f"  ╭────────╮\n  ⌸ ❯ {message}    ⛶\n  ╰────────╯\n   deepseek-flash · max"

        for app, render in [("codex", codex), ("claude-code", claude), ("dsh-tui", deepseek)]:
            with self.subTest(app=app):
                calls, receipt = self.deliver(app, render, started=app == "codex")
                self.assertEqual(calls.count("send"), 1)
                self.assertEqual(calls.count("keys"), 1)
                self.assertTrue(receipt["resubmitted"])

    def test_submitted_or_edited_draft_does_not_get_enter(self):
        for render in (
            lambda message: f"› {message}\n\nWorking\n\n› Next task\n\n  gpt-5.4",
            lambda message: f"› user text {message}\n\n  gpt-5.4",
            lambda message: f"› {message}\n\n  user text\n\n  gpt-5.4",
        ):
            calls, receipt = self.deliver("codex", render)
            self.assertNotIn("keys", calls)
            self.assertFalse(receipt["resubmitted"])

    def test_blocked_or_reassigned_target_does_not_get_enter(self):
        def render(message):
            return f"› {message}\n\n  gpt-5.4"
        for changes in ({"final_status": "blocked"}, {"final_run": "another-run"}):
            calls, error = self.deliver("codex", render, **changes)
            self.assertNotIn("keys", calls)
            self.assertIsInstance(error, str)

    def test_agent_at_its_composer_after_a_turn_error_can_receive_feedback(self):
        calls, receipt = self.deliver("dsh-tui", lambda message: f"╭────╮\n⌸ ❯ {message}    ⛶\n╰────╯",
                                      final_status="error")
        self.assertEqual(calls.count("keys"), 1)
        self.assertTrue(receipt["resubmitted"])

    def test_stalled_uncertain_send_is_reported_without_retry(self):
        calls, error = self.deliver("dsh-tui", lambda _: "Unknown screen", started=False)
        self.assertEqual(calls.count("send"), 1)
        self.assertNotIn("keys", calls)
        self.assertIn("未观察到", error)

    def test_worker_gets_its_own_tab_in_the_visible_ancestors_space(self):
        hc = d.Herdr.__new__(d.Herdr)
        hc.get = {"t1": {"parent": "t0"}, "t0": {"labels": {"herdr.space": "space", "herdr.tab": "lead-tab"}}}.__getitem__
        with patch.object(hc, "call", return_value={"address": "t2"}) as call:
            hc.start({"id": "run", "root": ".", "controller": "t1"}, "worker", "unique-token", ["codex"], "worker")
        args = call.call_args.args
        self.assertIn("herdr.space=space", args)
        self.assertIn("herdr.tab=pdtab-unique-token", args)
        self.assertIn("t1", args)

    def test_unattached_run_workers_get_a_space_without_adopting_the_watcher(self):
        hc = d.Herdr.__new__(d.Herdr)
        r = {"id": "run", "root": ".", "controller": "t1"}
        with patch.object(hc, "get", return_value={}), patch.object(hc, "call", return_value={"address": "t2"}) as call:
            hc.start(r, "worker", "worker-token", ["codex"], "worker")
            self.assertIn("herdr.space=pdspace-run", call.call_args.args)
            self.assertIn("herdr.tab=pdtab-worker-token", call.call_args.args)
            hc.start(r, "watcher", "watcher-token", ["python"], "watcher")
            self.assertFalse(any(arg.startswith("herdr.") for arg in call.call_args.args))


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.copy()
        os.environ["PERSONAL_DISPATCH_HOME"] = str(Path(self.tmp.name) / "data")
        os.environ["HC_TERMINAL_ID"] = "lead"
        self.root = Path(self.tmp.name) / "无git 普通目录"
        self.root.mkdir()
        self.hc, self.store = FakeHerdr(), d.Store()
        self.original_executable = d.executable
        d.executable = lambda kind: "fake-" + kind

    def tearDown(self):
        d.executable = self.original_executable
        self.store.db.close()
        os.environ.clear()
        os.environ.update(self.old)
        self.tmp.cleanup()

    def init(self, jobs, lanes=2):
        return d.init_run(self.store, self.hc, self.root, {"objective": "test", "jobs": jobs}, "codex", lanes)

    def finish_claim(self, r, key, files=None):
        j = self.store.job(r["id"], key)
        self.hc.get(j["address"])["status"] = {"state": "done"}
        self.hc.get(j["address"])["turn"] += 1
        d.atomic(d.job_dir(r["id"], key) / "result.json", {"attempt": j["attempt"], "status": "done", "summary": "真实完成", "changed_files": files or [], "evidence": ["file"]})
        d.sweep(self.store, self.hc, r["id"])

    def accept_job(self, r, key):
        evidence = Path(self.tmp.name) / "evidence.md"
        evidence.write_text("主控读取实际内容并验证", encoding="utf-8")
        return d.accept(self.store, self.hc, self.store.run(r["id"]), key, evidence)

    def test_non_git_dependency_and_idempotence(self):
        r = self.init([job("first"), job("second", deps=["first"])])
        d.sweep(self.store, self.hc, r["id"])
        n = len(self.hc.terminals)
        d.sweep(self.store, self.hc, r["id"])
        self.assertEqual(len(self.hc.terminals), n)
        self.assertEqual(self.store.job(r["id"], "second")["status"], "pending")
        self.finish_claim(r, "first")
        self.assertEqual(self.store.job(r["id"], "first")["status"], "review")
        self.assertEqual(self.store.job(r["id"], "second")["status"], "pending")
        self.accept_job(r, "first")
        d.sweep(self.store, self.hc, r["id"])
        self.assertEqual(self.store.job(r["id"], "second")["status"], "running")

    def test_parent_directory_reader_writer_conflict(self):
        r = self.init([job("reader", reads=["Resource"]), job("writer", writes=["Resource/a.xlsx"]), job("other", writes=["Source/b.py"])])
        d.sweep(self.store, self.hc, r["id"])
        self.assertEqual([j["status"] for j in self.store.jobs(r["id"])], ["running", "pending", "running"])

    def test_blocked_retains_lock_but_does_not_consume_slot(self):
        r = self.init([job("blocked", writes=["same.txt"]), job("conflict", writes=["same.txt"]), job("other", writes=["other.txt"])], lanes=1)
        d.sweep(self.store, self.hc, r["id"])
        j = self.store.job(r["id"], "blocked")
        self.hc.get(j["address"])["status"] = {"state": "blocked"}
        d.sweep(self.store, self.hc, r["id"])
        self.assertEqual([j["status"] for j in self.store.jobs(r["id"])], ["blocked", "pending", "running"])

    def test_cross_run_locks(self):
        r1 = self.init([job("one", writes=["file.txt"])])
        d.sweep(self.store, self.hc, r1["id"])
        self.hc.terminals["lead2"] = {"address": "lead2", "state": "live", "status": {"state": "working"}, "labels": {}}
        os.environ["HC_TERMINAL_ID"] = "lead2"
        r2 = self.init([job("two", reads=["file.txt"]), job("three", writes=["other.txt"])])
        d.sweep(self.store, self.hc, r2["id"])
        self.assertEqual([j["status"] for j in self.store.jobs(r2["id"])], ["pending", "running"])

    def test_pause_resume_retains_identity_and_restart_count(self):
        r = self.init([job("one", writes=["one.txt"])])
        d.sweep(self.store, self.hc, r["id"])
        address = self.store.job(r["id"], "one")["address"]
        d.pause(self.store, self.hc, self.store.run(r["id"]))
        self.assertEqual(self.store.job(r["id"], "one")["status"], "paused")
        self.store.update("runs", {"id": r["id"]}, restarts=3)
        d.resume(self.store, self.hc, self.store.run(r["id"]))
        self.assertEqual(self.store.run(r["id"])["restarts"], 3)
        d.sweep(self.store, self.hc, r["id"])
        self.assertEqual(self.store.job(r["id"], "one")["address"], address)
        self.assertEqual(len(self.hc.sent), 1)

    def test_ui_stop_pauses_workers_and_does_not_need_controller_environment(self):
        r = self.init([job("one")])
        d.sweep(self.store, self.hc, r["id"])
        del os.environ["HC_TERMINAL_ID"]
        result = d.control(self.store, self.hc, "lead", "stop", "esc")
        self.assertEqual(result["status"], "paused")
        self.assertEqual(d.status_word(self.hc.get("lead")), "idle")
        self.assertEqual(self.store.job(r["id"], "one")["status"], "paused")
        before = len(self.hc.sent)
        d.supervise(self.store, self.hc, r["id"])
        self.assertEqual(len(self.hc.sent), before)
        self.assertEqual(self.store.run(r["id"])["status"], "paused")

    def test_closed_controller_pauses_instead_of_restarting(self):
        r = self.init([job("one")])
        d.sweep(self.store, self.hc, r["id"])
        self.hc.get("lead")["state"] = "finished"
        before = len(self.hc.terminals)
        d.supervise(self.store, self.hc, r["id"])
        self.assertEqual(self.store.run(r["id"])["status"], "paused")
        self.assertEqual(len(self.hc.terminals), before)

    def test_stop_failure_remains_pausing_and_attempts_other_workers(self):
        r = self.init([job("one"), job("two")])
        d.sweep(self.store, self.hc, r["id"])
        original = self.hc.call
        first = self.store.job(r["id"], "one")["address"]
        def call(*args, **kwargs):
            if args[:2] == ("interrupt", first):
                raise d.DispatchError("offline")
            return original(*args, **kwargs)
        with patch.object(self.hc, "call", side_effect=call):
            result = d.pause(self.store, self.hc, r)
        self.assertEqual(result["status"], "pausing")
        self.assertIn("offline", result["note"])
        self.assertEqual(self.store.job(r["id"], "two")["status"], "paused")
        self.assertEqual(d.supervise(self.store, self.hc, r["id"])["status"], "paused")

    def test_pause_epoch_invalidates_delivery_even_after_resume(self):
        r = self.init([job("one")])
        d.pause(self.store, self.hc, r)
        d.resume(self.store, self.hc, self.store.run(r["id"]))
        with self.assertRaises(d.DispatchPaused):
            d.require_active(self.store, r)

    def test_unmanaged_terminal_keeps_stop_key(self):
        result = d.control(self.store, self.hc, "lead", "stop", "ctrl+c")
        self.assertFalse(result["managed"])
        self.assertEqual(self.hc.get("lead").get("last_keys"), ["ctrl+c"])

    def test_idle_controller_without_stop_is_still_supervised(self):
        r = self.init([job("one")])
        d.sweep(self.store, self.hc, r["id"])
        self.hc.get("lead")["status"] = {"state": "idle"}
        d.supervise(self.store, self.hc, r["id"])
        self.assertEqual(len(self.hc.sent), 1)

    def test_resume_restarts_watcher_and_reuses_existing_workers(self):
        r = self.init([job("one")])
        d.sweep(self.store, self.hc, r["id"])
        before = self.store.run(r["id"])
        worker = self.store.job(r["id"], "one")["address"]
        d.control(self.store, self.hc, "lead", "stop")
        result = d.control(self.store, self.hc, "lead", "resume")
        self.assertEqual(result["status"], "active")
        self.assertNotEqual(result["watcher"], before["watcher"])
        self.assertEqual(self.hc.get(before["watcher"])["state"], "finished")
        self.assertEqual(self.store.job(r["id"], "one")["address"], worker)
        self.assertTrue(any(address == "lead" for address, _ in self.hc.sent))

    def test_stop_during_resume_cannot_be_overwritten(self):
        r = self.init([job("one")])
        d.pause(self.store, self.hc, r)
        original = d.controller_label
        def label(*args):
            original(*args)
            d.request_pause(self.store, self.store.run(r["id"]), "new stop")
        with patch.object(d, "controller_label", side_effect=label):
            with self.assertRaises(d.DispatchPaused):
                d.resume(self.store, self.hc, self.store.run(r["id"]))
        self.assertEqual(self.store.run(r["id"])["status"], "pausing")

    def test_accept_requires_real_report_backup_and_checks(self):
        r = self.init([job("one", writes=["one.txt"], checks=[{"argv": ["python", "-c", "print('ok')"]}])])
        d.sweep(self.store, self.hc, r["id"])
        p = self.root / "one.txt"
        p.write_bytes(b"old")
        d.backup(self.store, r, "one", ["one.txt"])
        p.write_bytes(b"new")
        self.finish_claim(r, "one", ["one.txt"])
        with self.assertRaises(d.DispatchError):
            self.accept_job(r, "one")
        d.run_checks(self.store, self.store.run(r["id"]), "one", 1)
        p.write_bytes(b"changed after check")
        with self.assertRaises(d.DispatchError):
            self.accept_job(r, "one")
        d.run_checks(self.store, self.store.run(r["id"]), "one", 1)
        self.accept_job(r, "one")
        self.assertEqual(self.store.job(r["id"], "one")["status"], "complete")

    def test_bad_paths_cycles_and_identity(self):
        for jobs in [[job("escape", writes=["../escape"])], [job("glob", writes=["*.xlsx"])], [job("self", deps=["self"])]]:
            with self.assertRaises(d.DispatchError):
                d.validate_plan({"objective": "test", "jobs": jobs}, self.root, "codex")
        r = self.init([job("one")])
        d.sweep(self.store, self.hc, r["id"])
        j = self.store.job(r["id"], "one")
        self.hc.get(j["address"])["labels"]["dispatch.token"] = "someone-else"
        d.sweep(self.store, self.hc, r["id"])
        self.assertEqual(self.store.job(r["id"], "one")["status"], "blocked")
        self.assertFalse(self.hc.sent)

    def test_stale_snapshot_cannot_accept_paused_run(self):
        r = self.init([job("one")])
        d.sweep(self.store, self.hc, r["id"])
        self.finish_claim(r, "one")
        stale = self.store.run(r["id"])
        evidence = Path(self.tmp.name) / "ev.md"
        evidence.write_text("verified", encoding="utf-8")
        d.pause(self.store, self.hc, stale)
        with self.assertRaises(d.DispatchError):
            d.accept(self.store, self.hc, stale, "one", evidence)

    def test_resume_does_not_steal_controller_from_other_run(self):
        first = self.init([job("first")])
        self.hc.terminals["lead2"] = {"address": "lead2", "state": "live", "status": {"state": "working"}, "labels": {}}
        os.environ["HC_TERMINAL_ID"] = "lead2"
        second = self.init([job("second")])
        self.hc.get("lead")["state"] = "finished"
        with self.assertRaises(d.DispatchError):
            d.resume(self.store, self.hc, first)
        self.assertEqual(self.hc.get("lead2")["labels"]["dispatch.controller"], second["id"])

    def test_check_launch_timeout_does_not_duplicate_terminal(self):
        r = self.init([job("one")])
        d.sweep(self.store, self.hc, r["id"])
        self.finish_claim(r, "one")
        old_start = self.hc.start
        def timeout_after_spawn(*args):
            old_start(*args)
            raise d.subprocess.TimeoutExpired("hc", 30)
        self.hc.start = timeout_after_spawn
        with self.assertRaises(d.subprocess.TimeoutExpired):
            d.start_checks(self.store, self.hc, self.store.run(r["id"]), "one")
        n = len(self.hc.terminals)
        self.hc.start = old_start
        d.start_checks(self.store, self.hc, self.store.run(r["id"]), "one")
        self.assertEqual(n, len(self.hc.terminals))
        d.pause(self.store, self.hc, self.store.run(r["id"]))
        info = d.load(d.job_dir(r["id"], "one") / "check-terminal.json")
        self.assertEqual(d.status_word(self.hc.get(info["address"])), "idle")

    def test_backup_cli_only_needs_task_manifest(self):
        r = self.init([job("one", writes=["one.txt"])])
        d.sweep(self.store, self.hc, r["id"])
        # A worker may read/write only its own task directory, not run state or SQLite.
        state = d.run_dir(r["id"]) / "state.json"
        state.rename(state.with_suffix(".hidden"))
        p = d.subprocess.run([d.sys.executable, str(SCRIPT), "backup", "--data-home", str(d.home()), "--run", r["id"], "--job", "one", "--file", "one.txt"], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("backup_dir", json.loads(p.stdout))


if __name__ == "__main__":
    unittest.main()
