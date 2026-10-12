import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import common
import db
import audit
import run
import runtime
import act
import checkpoint
import pathmap
import observe
import read
import state
import logcat
import cleanup
import time
from types import SimpleNamespace


class TrustedExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Path(self.temp.name)
        self.database = db.SessionDB(self.storage / "sessions.db")
        self.sid = self.database.start_session("test", "steps and expectations")
        runtime.execution("not_executed")

    def tearDown(self):
        conn = getattr(self.database._local, "conn", None)
        if conn is not None:
            conn.close()
        self.temp.cleanup()

    def event(self, sid=None):
        evidence = self.storage / "shot.png"
        evidence.write_bytes(b"evidence")
        return self.database.log_event(
            sid or self.sid, "observe", "observe", evidence=str(evidence))

    def test_pass_requires_findings(self):
        with self.assertRaises(ValueError):
            self.database.finish_session(self.sid, "PASS")
        self.assertEqual(self.database.get_session(self.sid)["status"], "running")

    def test_finding_requires_same_session_evidence(self):
        other = self.database.start_session("other", "other")
        with self.assertRaises(ValueError):
            self.database.add_finding(
                self.sid, "PASS", "visible", "visible", event_id=self.event(other))

    def test_verified_finding_requires_evidence(self):
        with self.assertRaises(ValueError):
            self.database.add_finding(self.sid, "PASS", "visible", "visible")

    def test_conflicting_finish_does_not_update_case(self):
        case_id = self.database.add_case("case", "input")
        sid = self.database.start_session("case", "input", case_id=case_id)
        self.database.add_finding(sid, "FAIL", "enabled", "disabled",
                                  event_id=self.event(sid))
        with self.assertRaises(ValueError):
            self.database.finish_session(sid, "PASS")
        self.assertEqual(self.database.get_case(case_id)["run_count"], 0)
        self.assertEqual(self.database.get_session(sid)["status"], "running")

    def test_finish_rolls_back_statistics_failure(self):
        case_id = self.database.add_case("case", "input")
        sid = self.database.start_session("case", "input", case_id=case_id)
        self.database.add_finding(sid, "PASS", "visible", "visible",
                                  event_id=self.event(sid))
        self.database._conn().execute(
            "CREATE TRIGGER reject_stats BEFORE UPDATE ON cases "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        self.database._conn().commit()
        with self.assertRaises(sqlite3.Error):
            self.database.finish_session(sid, "PASS")
        self.assertEqual(self.database.get_session(sid)["status"], "running")
        self.assertEqual(self.database.get_case(case_id)["run_count"], 0)

    def test_finish_is_not_counted_twice(self):
        case_id = self.database.add_case("case", "input")
        sid = self.database.start_session("case", "input", case_id=case_id)
        self.database.add_finding(sid, "PASS", "visible", "visible",
                                  event_id=self.event(sid))
        self.assertTrue(self.database.finish_session(sid, "PASS"))
        with self.assertRaises(ValueError):
            self.database.finish_session(sid, "PASS")
        self.assertEqual(self.database.get_case(case_id)["run_count"], 1)

    def test_event_write_failure_is_not_silent(self):
        self.database._conn().execute(
            "CREATE TRIGGER reject_event BEFORE INSERT ON events "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        self.database._conn().commit()
        with self.assertRaises(sqlite3.Error):
            self.event()

    def test_log_auto_none_result_exits_with_recording_error(self):
        with patch("db.current_session", return_value=self.sid), \
                patch("db.get_db") as get_database, \
                patch("common.STORAGE_DIR", self.storage), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            get_database.return_value.log_event.return_value = None
            with self.assertRaises(SystemExit) as error:
                common.log_auto("act", "act tap", "clicked")
        self.assertEqual(error.exception.code, 1)
        self.assertFalse(json.loads(output.getvalue())["ok"])
        self.assertTrue(json.loads(output.getvalue())["recording_failed"])

    def test_multiple_evidence_events(self):
        first, second = self.event(), self.event()
        fid = self.database.add_finding(
            self.sid, "PASS", "persisted", "persisted", event_ids=[first, second])
        finding = self.database.get_session(self.sid)["findings"][0]
        self.assertEqual(finding["id"], fid)
        self.assertEqual(finding["event_ids"], [first, second])

    def test_missing_evidence_rejects_finish(self):
        self.database.add_finding(
            self.sid, "PASS", "visible", "visible", event_id=self.event())
        (self.storage / "shot.png").unlink()
        with self.assertRaises(ValueError):
            self.database.finish_session(self.sid, "PASS")

    def test_cli_rejection_preserves_current_pointer(self):
        (self.storage / "current_session").write_text(str(self.sid), encoding="utf-8")
        env = {**os.environ, "ANDROID_TEST_STORAGE": str(self.storage)}
        result = subprocess.run(
            [sys.executable, str(TOOLS / "session.py"), "finish",
             "--status", "PASS", "--no-paths"],
            env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.storage / "current_session").read_text(), str(self.sid))
        self.assertEqual(self.database.get_session(self.sid)["status"], "running")

    def test_terminated_session_only_finishes_blocked(self):
        self.database.terminate_session(self.sid, "crash")
        with self.assertRaises(ValueError):
            self.database.finish_session(self.sid, "ERROR", "crash")
        self.assertTrue(self.database.finish_session(self.sid, "BLOCKED", "crash"))

    def test_incomplete_recording_prevents_pass_but_can_close_error(self):
        self.database.add_finding(self.sid, "PASS", "visible", "visible",
                                  event_id=self.event())
        errors = self.storage / "recording_errors" / str(self.sid)
        errors.mkdir(parents=True)
        (errors / "failed.json").write_text("{}")
        with self.assertRaises(ValueError):
            self.database.finish_session(self.sid, "PASS")
        self.assertTrue(self.database.finish_session(self.sid, "ERROR", "recording lost"))

    def test_started_ledger_prevents_pass(self):
        self.database.add_finding(self.sid, "PASS", "visible", "visible",
                                  event_id=self.event())
        run.write_record(self.storage / "calls" / "unfinished.json", {
            "call_id": "unfinished", "session_id": self.sid,
            "expects_event": True, "status": "started"})
        with self.assertRaises(ValueError):
            self.database.finish_session(self.sid, "PASS")

    def test_started_ledger_blocks_further_device_commands(self):
        run.write_record(self.storage / "calls" / "unfinished.json", {
            "call_id": "unfinished", "session_id": self.sid,
            "expects_event": True, "status": "started"})
        with patch("db.current_session", return_value=self.sid), \
                patch("common.STORAGE_DIR", self.storage), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as error:
                common.guard_terminated("act")
        self.assertEqual(error.exception.code, 1)
        self.assertIn("incomplete recording", json.loads(output.getvalue())["error"])

    def test_malformed_ledger_is_explicit_error(self):
        run.write_record(self.storage / "calls" / "invalid.json", {
            "call_id": "invalid", "session_id": self.sid,
            "expects_event": True, "status": "finished", "audit": None})
        with self.assertRaises(ValueError):
            runtime.recording_issues(self.storage, self.sid)

    def test_real_launcher_and_auditor_cli(self):
        eid = self.event()
        env = {**os.environ, "ANDROID_TEST_STORAGE": str(self.storage)}
        result = subprocess.run(
            [sys.executable, str(TOOLS / "run.py"), "--session-id", str(self.sid),
             "session", "finding", "--status", "PASS", "--event-id", str(eid),
             "--expect", "visible", "--actual", "visible"],
            env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(json.loads(result.stdout)["ok"])
        calls = list(runtime.invocation_paths(self.storage, self.sid))
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0].read_text())["status"], "ended")
        report = subprocess.run(
            [sys.executable, str(TOOLS / "audit.py"), "--session-id", str(self.sid)],
            env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(report.returncode, 0)
        self.assertIn(eid, json.loads(report.stdout)["untracked_event_ids"])

    def test_clear_explicit_session_preserves_another_current_session(self):
        pointer = self.storage / "current_session"
        pointer.write_text("999")
        with patch("db.CURRENT_FILE", pointer):
            db.clear_current(self.sid)
        self.assertEqual(pointer.read_text(), "999")

    def test_launcher_reconciles_independent_record(self):
        def child(command, **kwargs):
            call_id = kwargs["env"]["ANDROID_TEST_CALL_ID"]
            eid = self.event()
            self.database._conn().execute(
                "UPDATE events SET call_id=?, kind='act' WHERE id=?", (call_id, eid))
            self.database._conn().commit()
            return subprocess.CompletedProcess(command, 0, json.dumps({
                "ok": True, "call_id": call_id, "event_ids": [eid],
                "timing": {"stages_ms": {"action": 1.0}},
            }), "")

        with patch("run.subprocess.run", side_effect=child), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = run.launch("act", ["tap"], self.storage, self.sid)
        self.assertEqual(code, 0)
        with patch("audit.STORAGE_DIR", self.storage):
            result = audit.report(self.sid)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["calls"]), 1)
        self.assertGreaterEqual(result["calls"][0]["wall_ms"], 0)

    def test_launcher_detects_success_without_events(self):
        result = subprocess.CompletedProcess([], 0, '{"ok": true}', "")
        with patch("run.subprocess.run", return_value=result), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = run.launch("observe", [], self.storage, self.sid)
        self.assertEqual(code, 1)
        self.assertTrue(list((self.storage / "recording_errors" / str(self.sid)).glob("*.json")))

    def test_interrupted_launcher_leaves_start_record(self):
        with patch("run.subprocess.run", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run.launch("observe", [], self.storage, self.sid)
        with patch("audit.STORAGE_DIR", self.storage):
            result = audit.report(self.sid)
        self.assertFalse(result["ok"])
        self.assertEqual(result["calls"][0]["status"], "started")

    def test_audit_detects_untracked_events(self):
        eid = self.event()
        with patch("audit.STORAGE_DIR", self.storage):
            result = audit.report(self.sid)
        self.assertFalse(result["ok"])
        self.assertEqual(result["untracked_event_ids"], [eid])

    def test_filtered_audit_does_not_hide_event_assigned_to_other_session(self):
        eid = self.event()
        run.write_record(self.storage / "calls" / "wrong-session.json", {
            "call_id": runtime.CALL_ID, "session_id": self.sid + 1,
            "tool": "observe", "status": "ended", "returncode": 0})
        with patch("audit.STORAGE_DIR", self.storage):
            result = audit.report(self.sid)
        self.assertFalse(result["ok"])
        self.assertEqual(result["untracked_event_ids"], [eid])

    def test_malformed_current_pointer_does_not_disable_guard(self):
        pointer = self.storage / "current_session"
        pointer.write_text("not-an-id")
        with patch("db.CURRENT_FILE", pointer), \
                patch.dict(os.environ, {}, clear=True), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                common.guard_terminated("act tap")
        self.assertEqual(error.exception.code, 1)

    def test_timing_includes_failed_stage(self):
        with patch.dict(runtime.STAGES, {}, clear=True):
            with self.assertRaises(RuntimeError):
                with runtime.stage("test_failure"):
                    raise RuntimeError("injected")
            timing = runtime.timing_snapshot()
        self.assertIn("test_failure", timing["stages_ms"])
        self.assertGreaterEqual(timing["stages_ms"]["test_failure"], 0)

    def test_cli_success_commits_and_clears_matching_pointer(self):
        event_id = self.event()
        self.database.add_finding(self.sid, "PASS", "visible", "visible",
                                  event_id=event_id)
        pointer = self.storage / "current_session"
        pointer.write_text(str(self.sid))
        result = subprocess.run(
            [sys.executable, str(TOOLS / "session.py"), "finish",
             "--status", "PASS", "--no-paths"],
            env={**os.environ, "ANDROID_TEST_STORAGE": str(self.storage)},
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(pointer.exists())
        self.assertEqual(self.database.get_session(self.sid)["status"], "PASS")

    def test_act_and_observe_emit_evidence_ids_and_measured_stages(self):
        class Shot:
            size = (100, 200)

            def save(self, path):
                Path(path).write_bytes(b"fake screenshot")

        class Device:
            info = {"displayRotation": 0}

            def screenshot(self):
                return Shot()

            def dump_hierarchy(self):
                return ('<hierarchy><node package="com.app" text="Save" clickable="true" '
                        'resource-id="com.app:id/save" bounds="[0,0][100,100]"/></hierarchy>')

            def app_current(self):
                return {"package": "com.app", "activity": ".Main"}

            def click(self, x, y):
                pass

            def shell(self, command):
                return SimpleNamespace(output="")

        device = Device()
        for module, arguments, expected_kind in (
                (act, ["act.py", "tap", "--x", "20", "--y", "20", "--settle", "0"], "act"),
                (observe, ["observe.py"], "observe")):
            with self.subTest(tool=expected_kind), \
                    patch("db.current_session", return_value=self.sid), \
                    patch("db.get_db", return_value=self.database), \
                    patch("common.STORAGE_DIR", self.storage), \
                    patch("common.DEFAULT_EVIDENCE_DIR", self.storage / "evidence"), \
                    patch.object(module, "connect", return_value=device), \
                    patch("perm.handle", return_value={"detected": False}), \
                    patch("act.check_crash", side_effect=lambda *args: time.sleep(0.03)), \
                    patch.object(sys, "argv", arguments), \
                    patch.dict(runtime.STAGES, {}, clear=True), \
                    patch.object(runtime, "EVENT_IDS", []), \
                    patch.object(common, "EVENT_IDS", runtime.EVENT_IDS), \
                    patch.dict(os.environ, {"ANDROID_TEST_VERBOSE_TIMING": "1"}), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                module.main()
                result = json.loads(output.getvalue())
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["event_ids"])
            self.assertTrue(Path(result["evidence"]["png"]).exists())
            for name in ("screenshot", "png_save", "ui_dump", "permission", "node_summary"):
                self.assertIn(name, result["timing"]["stages_ms"])
            event = self.database.get_session(self.sid)["events"][-1]
            self.assertEqual(event["kind"], expected_kind)
            self.assertEqual(event["id"], result["event_ids"][-1])
            self.assertGreaterEqual(event["duration_ms"], 30)

    def test_read_times_initialization_separately(self):
        import types
        image = self.storage / "image.png"
        image.write_bytes(b"fake screenshot")
        fake_ocr = types.ModuleType("rapidocr_onnxruntime")
        fake_ocr.RapidOCR = lambda: lambda path: ([], None)
        with patch.dict(sys.modules, {"rapidocr_onnxruntime": fake_ocr}), \
                patch("PIL.Image.open", return_value=contextlib.nullcontext(SimpleNamespace(size=(100, 200)))), \
                patch("db.current_session", return_value=self.sid), \
                patch("db.get_db", return_value=self.database), \
                patch.object(sys, "argv", ["read.py", "--image", str(image)]), \
                patch.dict(runtime.STAGES, {}, clear=True), \
                patch.object(common, "EVENT_IDS", []), \
                patch.dict(os.environ, {"ANDROID_TEST_VERBOSE_TIMING": "1"}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            read.main()
        result = json.loads(output.getvalue())
        self.assertIn("ocr_initialize", result["timing"]["stages_ms"])
        self.assertIn("ocr_inference", result["timing"]["stages_ms"])

    def test_refusal_and_connection_failure_are_complete_attempts(self):
        for rejected in (True, False):
            call_id = "refused" if rejected else "connection"
            result = {"ok": False, "rejected": rejected,
                      "execution_state": "not_executed", "call_id": call_id, "event_ids": []}
            record = {"status": "ended", "call_id": call_id, "tool": "act",
                      "returncode": 3 if rejected else 1, "expects_event": True, "result": result}
            reconciled = run.reconcile(record, [])
            self.assertTrue(reconciled["complete"], reconciled)
            self.assertEqual(reconciled["outcome"], "refused" if rejected else "failed")

    def test_unknown_action_failure_remains_incomplete(self):
        record = {"status": "ended", "call_id": "unknown", "tool": "act", "returncode": 1,
                  "expects_event": True, "result": {
                      "ok": False, "execution_state": "may_have_executed",
                      "call_id": "unknown", "event_ids": []}}
        self.assertFalse(run.reconcile(record, [])["complete"])

    # --- 用法错误（参数不对、工具入口即拒绝）与"可能已执行"分级 ---
    # 真实事故：tap 传了不支持的 --text → argparse exit 2 → 无结构化输出
    # → 被判"记录不完整" → 整个会话锁定作废。但该调用确定没碰设备。

    def test_usage_error_is_not_a_recording_defect(self):
        record = {"status": "ended", "call_id": "usage", "tool": "act", "returncode": 2,
                  "expects_event": True, "result": None, "timing": None}
        reconciled = run.reconcile(record, [])
        self.assertTrue(reconciled["complete"], reconciled)
        self.assertFalse(reconciled["executed"])
        self.assertIn("usage error", " ".join(reconciled["issues"]))

    def test_usage_error_detector_requires_all_three_signals(self):
        base = {"status": "ended", "tool": "act", "returncode": 2,
                "result": None, "timing": None}
        self.assertTrue(run.is_usage_error(base))
        # 有结构化输出 → 工具跑到了 emit，不是用法错误
        self.assertFalse(run.is_usage_error({**base, "result": {"ok": False}}))
        # 有 timing 分段 → 执行路径已进入，不是用法错误
        self.assertFalse(run.is_usage_error({**base, "timing": {"stages_ms": {}}}))
        # 非 argparse 的返回码 → 不适用
        self.assertFalse(run.is_usage_error({**base, "returncode": 1}))

    def test_partial_execution_without_result_stays_incomplete(self):
        """没有结构化输出、但已采到 timing：无法判断是否已执行 → 保守判不完整。"""
        record = {"status": "ended", "call_id": "partial", "tool": "act", "returncode": 1,
                  "expects_event": True, "result": None,
                  "timing": {"stages_ms": {"action": 12.0}}}
        reconciled = run.reconcile(record, [])
        self.assertFalse(reconciled["complete"], reconciled)
        self.assertIn("missing structured tool result", reconciled["issues"])

    def test_usage_error_does_not_lock_session(self):
        """用法错误不产生 recording marker，也不会让 recording_issues 报锁。"""
        with patch.object(runtime, "STORAGE_DIR", self.storage):
            calls = self.storage / "calls" / str(self.sid)
            calls.mkdir(parents=True, exist_ok=True)
            (calls / "usage.json").write_text(json.dumps({
                "call_id": "usage", "session_id": self.sid, "tool": "act",
                "status": "ended", "returncode": 2, "expects_event": True,
                "result": None,
                "audit": run.reconcile(
                    {"status": "ended", "call_id": "usage", "tool": "act",
                     "returncode": 2, "expects_event": True, "result": None}, []),
            }), encoding="utf-8")
            self.assertEqual(runtime.recording_issues(self.storage, self.sid), [])

    def test_may_have_executed_still_locks_session(self):
        """反向保护：可能已执行的调用必须仍然锁会话（不能被上面放宽掉）。"""
        with patch.object(runtime, "STORAGE_DIR", self.storage):
            calls = self.storage / "calls" / str(self.sid)
            calls.mkdir(parents=True, exist_ok=True)
            (calls / "dangling.json").write_text(json.dumps({
                "call_id": "dangling", "session_id": self.sid, "tool": "act",
                "status": "ended", "returncode": 1, "expects_event": True,
                "result": {"ok": False, "execution_state": "may_have_executed",
                           "call_id": "dangling", "event_ids": []},
                "audit": run.reconcile(
                    {"status": "ended", "call_id": "dangling", "tool": "act",
                     "returncode": 1, "expects_event": True,
                     "result": {"ok": False, "execution_state": "may_have_executed",
                                "call_id": "dangling", "event_ids": []}}, []),
            }), encoding="utf-8")
            issues = runtime.recording_issues(self.storage, self.sid)
            self.assertTrue(issues, "可能已执行的悬空动作必须仍被锁定")

    def test_successful_call_writes_nothing_to_stderr(self):
        """成功的调用不应在 stderr 留任何输出（否则调用方会误判失败）。"""
        env = {**os.environ, "ANDROID_TEST_STORAGE": str(self.storage)}
        result = subprocess.run(
            [sys.executable, str(TOOLS / "run.py"), "--session-id", str(self.sid),
             "session", "current"], env=env,
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.stderr, "", result.stderr)

    def test_tap_rejects_text_selector_with_actionable_message(self):
        """tap 传选择器应给出可执行的下一步，而不是只说缺 --x/--y。"""
        env = {**os.environ, "ANDROID_TEST_STORAGE": str(self.storage)}
        result = subprocess.run(
            [sys.executable, str(TOOLS / "act.py"), "tap", "--text", "确定"],
            env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("observe", result.stderr)
        self.assertIn("--x", result.stderr)

    def test_real_guard_refusal_has_no_recording_marker(self):
        self.database.terminate_session(self.sid, "crash")
        env = {**os.environ, "ANDROID_TEST_STORAGE": str(self.storage)}
        result = subprocess.run(
            [sys.executable, str(TOOLS / "run.py"), "--session-id", str(self.sid),
             "act", "tap", "--x", "1", "--y", "1"], env=env,
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertFalse((self.storage / "recording_errors").exists())
        with patch("audit.STORAGE_DIR", self.storage):
            report = audit.report(self.sid)
        self.assertTrue(report["ok"], report)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["failed_attempts"][0]["outcome"], "refused")

    def test_rejected_finish_does_not_poison_later_finish(self):
        env = {**os.environ, "ANDROID_TEST_STORAGE": str(self.storage)}
        command = [sys.executable, str(TOOLS / "run.py"), "--session-id", str(self.sid),
                   "session", "finish", "--status", "PASS", "--no-paths"]
        rejected = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(rejected.returncode, 1)
        def child(command, **kwargs):
            call_id = kwargs["env"]["ANDROID_TEST_CALL_ID"]
            eid = self.event()
            self.database._conn().execute("UPDATE events SET call_id=? WHERE id=?", (call_id, eid))
            self.database._conn().commit()
            self.database.add_finding(self.sid, "PASS", "visible", "visible", event_id=eid)
            return subprocess.CompletedProcess(command, 0, json.dumps({
                "ok": True, "execution_state": "executed", "call_id": call_id, "event_ids": [eid]}), "")
        with patch("run.subprocess.run", side_effect=child), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run.launch("observe", [], self.storage, self.sid), 0)
        accepted = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        calls = [json.loads(path.read_text(encoding="utf-8"))
                 for path in runtime.invocation_paths(self.storage, self.sid)]
        self.assertTrue(all(call["audit"]["complete"] for call in calls))
        with patch("audit.STORAGE_DIR", self.storage):
            report = audit.report(self.sid)
        self.assertIsNotNone(report["session_wall_ms"])
        self.assertEqual(len(report["failed_attempts"]), 1)

    def test_final_ledger_failure_preserves_child_output(self):
        child = subprocess.CompletedProcess([], 0, '{"ok": true}', "")
        original = run.write_record
        def writer(path, record):
            if record["status"] == "ended":
                raise OSError("injected ledger write failure")
            original(path, record)
        with patch("run.subprocess.run", return_value=child), \
                patch("run.write_record", side_effect=writer), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            code = run.launch("session", ["finding"], self.storage, self.sid)
        self.assertEqual(code, 1)
        self.assertTrue(json.loads(output.getvalue())["ok"])
        self.assertIn("ledger_write", errors.getvalue())
        self.assertEqual(json.loads(next(runtime.invocation_paths(self.storage, self.sid)).read_text())["status"],
                         "started")

    def test_launch_failure_records_complete_unexecuted_attempt(self):
        with patch("run.subprocess.run", side_effect=OSError("cannot spawn")):
            with self.assertRaises(OSError):
                run.launch("act", ["tap"], self.storage, self.sid)
        self.assertEqual(runtime.recording_issues(self.storage, self.sid), [])
        with patch("audit.STORAGE_DIR", self.storage):
            report = audit.report(self.sid)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["failed_attempts"][0]["outcome"], "failed")

    def test_success_without_operation_event_is_not_complete(self):
        record = {"status": "ended", "call_id": "invalid", "tool": "observe", "returncode": 0,
                  "expects_event": True, "result": {
                      "ok": True, "execution_state": "not_executed",
                      "call_id": "invalid", "event_ids": []}}
        self.assertFalse(run.reconcile(record, [])["complete"])

    def test_timing_write_failure_preserves_result_and_marks_audit_incomplete(self):
        path = self.storage / "timing.json"
        def child(command, **kwargs):
            with patch.dict(os.environ, {"ANDROID_TEST_TIMING_FILE": str(path)}), \
                    patch.object(Path, "write_text", side_effect=OSError("timing denied")), \
                    contextlib.redirect_stdout(io.StringIO()) as output, \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                with self.assertRaises(SystemExit) as error:
                    common.emit(True, {"committed": True})
            return subprocess.CompletedProcess(command, error.exception.code,
                                               output.getvalue(), errors.getvalue())
        with patch("run.subprocess.run", side_effect=child), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run.launch("session", ["finish"], self.storage, self.sid), 1)
        self.assertTrue(json.loads(output.getvalue())["committed"])
        with patch("audit.STORAGE_DIR", self.storage):
            self.assertFalse(audit.report(self.sid)["ok"])

    def test_session_scan_ignores_other_session_files(self):
        run.write_record(runtime.call_path(self.storage, self.sid + 1, "bad"), {"audit": None})
        self.assertEqual(runtime.recording_issues(self.storage, self.sid), [])

    def test_nonserializable_event_uses_explicit_recording_error(self):
        with patch("db.current_session", return_value=self.sid), \
                patch("db.get_db", return_value=self.database), \
                patch("common.STORAGE_DIR", self.storage), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit):
                common.log_auto("act", "act", "test", {"bad": object()})
        self.assertTrue(json.loads(output.getvalue())["recording_failed"])

    def test_committed_finish_with_corrupt_pointer_returns_warning(self):
        self.database.add_finding(self.sid, "PASS", "visible", "visible", event_id=self.event())
        pointer = self.storage / "current_session"
        pointer.write_text("corrupt")
        result = subprocess.run(
            [sys.executable, str(TOOLS / "session.py"), "finish", "--id", str(self.sid),
             "--status", "PASS", "--no-paths"],
            env={**os.environ, "ANDROID_TEST_STORAGE": str(self.storage)}, capture_output=True,
            text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("committed", json.loads(result.stdout)["self_check"][0])
        self.assertEqual(pointer.read_text(), "corrupt")

    def test_emit_default_is_compact_and_timing_is_saved_out_of_band(self):
        path = self.storage / "timing" / "test.json"
        with patch.dict(os.environ, {"ANDROID_TEST_TIMING_FILE": str(path),
                                   "ANDROID_TEST_VERBOSE_TIMING": "0"}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            common.emit(True, {"value": 1})
        self.assertNotIn("timing", json.loads(output.getvalue()))
        timing = json.loads(path.read_text())
        self.assertIn("serialization", timing["stages_ms"])
        self.assertEqual(timing["response_bytes"], len(output.getvalue().encode("utf-8")))

    def test_state_and_logcat_events_are_required_by_subcommand(self):
        self.assertTrue(run.expects_event("state", ["stop", "--package", "com.app"]))
        self.assertTrue(run.expects_event("logcat", ["crash"]))
        self.assertTrue(run.expects_event("logcat", ["clear"]))
        self.assertFalse(run.expects_event("logcat", ["setup"]))

    def test_act_steps_dry_run_does_not_require_an_event(self):
        """⚠️ 回归：`act steps --dry-run` 只算路线、不碰设备也不落事件。

        若仍按"应落事件"记账，ledger 会留下 started 无事件 → 整个会话被锁死
        不可重放（真实事故：会话 326）。
        """
        self.assertFalse(run.expects_event(
            "act", ["steps", "--from", "日历首页", "--to", "菜单", "--dry-run"]))
        # 不带 --dry-run 的 steps 会真跑设备 → 仍必须落事件
        self.assertTrue(run.expects_event(
            "act", ["steps", "--from", "日历首页", "--to", "菜单"]))
        # 普通 act 不受影响
        self.assertTrue(run.expects_event("act", ["tap", "--x", "1", "--y", "2"]))
        self.assertFalse(run.expects_event("act", ["steps", "--help"]))

    def test_state_and_logcat_mutations_are_blocked_before_connection(self):
        self.database.terminate_session(self.sid, "crash")
        for module, args in ((state, ["state.py", "stop", "--package", "com.app"]),
                             (logcat, ["logcat.py", "clear"]),
                             (logcat, ["logcat.py", "setup"])):
            with self.subTest(args=args), \
                    patch("db.current_session", return_value=self.sid), \
                    patch("db.get_db", return_value=self.database), \
                    patch("common.STORAGE_DIR", self.storage), \
                    patch.object(module, "connect") as connection, \
                    patch.object(sys, "argv", args), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    module.main()
                self.assertEqual(error.exception.code, 3)
                connection.assert_not_called()

    def test_logcat_evidence_read_is_allowed_after_termination(self):
        self.database.terminate_session(self.sid, "crash")
        device = SimpleNamespace(shell=lambda command: SimpleNamespace(output=""))
        with patch("db.current_session", return_value=self.sid), \
                patch("db.get_db", return_value=self.database), \
                patch("logcat.connect", return_value=device), \
                patch.object(sys, "argv", ["logcat.py", "crash"]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            logcat.main()
        self.assertTrue(json.loads(output.getvalue())["ok"])

    def test_cleanup_uses_isolated_storage_and_refuses_reference_errors(self):
        target = self.storage / "evidence" / "fixture"
        target.mkdir(parents=True)
        (target / "shot.png").write_bytes(b"test")
        with patch("cleanup.STORAGE_DIR", self.storage), \
                patch("db.get_db", return_value=self.database), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cleanup.rm_dir("fixture"), 0)
        target.mkdir()
        with patch("cleanup.STORAGE_DIR", self.storage), \
                patch("cleanup._db_referenced", side_effect=sqlite3.OperationalError("unreadable")):
            with self.assertRaises(sqlite3.Error):
                cleanup.rm_dir("fixture")
        self.assertTrue(target.exists())

    def test_cleanup_preserves_absolute_isolated_evidence_references(self):
        target = self.storage / "evidence" / "fixture"
        target.mkdir(parents=True)
        shot = target / "shot.png"
        shot.write_bytes(b"test")
        self.database.log_event(self.sid, "observe", "observe", evidence=str(shot))
        with patch("cleanup.STORAGE_DIR", self.storage), \
                patch("db.get_db", return_value=self.database), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cleanup.rm_dir("fixture"), 2)
        self.assertTrue(shot.exists())

    def test_connection_failure_does_not_create_missing_evidence_marker(self):
        def child(command, **kwargs):
            with patch("act.connect", side_effect=OSError("not connected")), \
                    patch("db.current_session", return_value=self.sid), \
                    patch("db.get_db", return_value=self.database), \
                    patch("common.STORAGE_DIR", self.storage), \
                    patch.object(common, "CALL_ID", kwargs["env"]["ANDROID_TEST_CALL_ID"]), \
                    patch.object(common, "EVENT_IDS", []), \
                    patch.object(sys, "argv", ["act.py", "tap", "--x", "1", "--y", "1"]), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as error:
                    act.main()
            return subprocess.CompletedProcess(command, error.exception.code, output.getvalue(), "")
        with patch("run.subprocess.run", side_effect=child), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run.launch("act", ["tap"], self.storage, self.sid), 1)
        self.assertFalse((self.storage / "recording_errors").exists())
        self.assertEqual(runtime.recording_issues(self.storage, self.sid), [])

    def test_legacy_calls_are_partitioned_once(self):
        path = self.storage / "calls" / "old.json"
        run.write_record(path, {"call_id": "old", "session_id": self.sid,
                                "expects_event": True, "status": "started"})
        self.assertEqual(list(runtime.invocation_paths(self.storage, self.sid)),
                         [runtime.call_path(self.storage, self.sid, "old")])
        self.assertFalse(path.exists())
        self.assertTrue((self.storage / "calls" / ".partitioned").exists())

    def test_vision_uses_isolated_configuration_and_allows_evidence_review(self):
        import vision
        (self.storage / "vision.json").write_text(json.dumps({
            "base_url": "http://example.invalid", "model": "fake", "api_key": "fake"}))
        image = self.storage / "shot.png"
        image.write_bytes(b"fake image")
        response = io.StringIO(json.dumps({"choices": [{"message": {"content": "visible"}}]}))
        with patch("vision.STORAGE_DIR", self.storage), \
                patch("vision.urllib.request.urlopen", return_value=response), \
                patch.object(sys, "argv", ["vision.py", "--image", str(image), "--prompt", "check"]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            vision.main()
        self.assertEqual(json.loads(output.getvalue())["text"], "visible")

    def test_existing_schema_migrates_without_changing_records(self):
        path = self.storage / "legacy.db"
        with contextlib.closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, session_id INTEGER,"
                         "seq INTEGER, kind TEXT, tool TEXT, detail TEXT, data_json TEXT,"
                         "evidence TEXT, duration_ms INTEGER, started_at TEXT, created_at TEXT)")
            conn.execute("INSERT INTO events(id,session_id,kind) VALUES (1,99,'observe')")
            conn.commit()
        migrated = db.SessionDB(path)
        try:
            row = migrated._conn().execute("SELECT * FROM events WHERE id=1").fetchone()
            self.assertEqual(row["kind"], "observe")
            self.assertIsNone(row["call_id"])
        finally:
            migrated._conn().close()


class CheckpointTest(unittest.TestCase):
    """检查点校验：让工具判"这步落在预期位置没有"，省掉 AI 读 dump 的判断时间。"""

    def test_parses_activity_rid_text(self):
        self.assertEqual(checkpoint.parse_expect("activity=EditTimetableActivity"),
                         {"activity": ["EditTimetableActivity"]})
        self.assertEqual(checkpoint.parse_expect("rid=et_schedule_name"),
                         {"rid": ["et_schedule_name"]})
        self.assertEqual(checkpoint.parse_expect("text=完成"), {"text": ["完成"]})

    def test_comma_is_and_pipe_is_or(self):
        got = checkpoint.parse_expect("activity=A,rid=b")
        self.assertEqual(got, {"activity": ["A"], "rid": ["b"]})
        self.assertEqual(checkpoint.parse_expect("activity=A|B"), {"activity": ["A", "B"]})

    def test_unknown_keys_and_empty_are_ignored(self):
        self.assertEqual(checkpoint.parse_expect("bogus=1"), {})
        self.assertEqual(checkpoint.parse_expect(""), {})
        self.assertEqual(checkpoint.parse_expect(None), {})

    def test_no_expectation_is_not_checked(self):
        r = checkpoint.evaluate({}, activity="X", sig=[])
        self.assertFalse(r["checked"])
        self.assertTrue(r["ok"])

    def test_and_semantics_require_all_conditions(self):
        sig = ["rid=et_schedule_name", "text=完成"]
        act = "com.zui.calendar.timetable.management.EditTimetableActivity"
        self.assertTrue(checkpoint.evaluate(
            checkpoint.parse_expect("activity=EditTimetableActivity,rid=et_schedule_name"),
            activity=act, sig=sig)["ok"])
        self.assertFalse(checkpoint.evaluate(
            checkpoint.parse_expect("activity=EditTimetableActivity,rid=nope"),
            activity=act, sig=sig)["ok"])

    def test_activity_match_respects_class_boundary(self):
        """⚠️ 回归：EditTimetableActivity **不能**被当成命中了 TimetableActivity。

        裸用 `endswith` 会让检查点在**错误的页面**上静默通过 —— 那比不校验更危险。
        """
        r = checkpoint.evaluate(
            checkpoint.parse_expect("activity=TimetableActivity"),
            activity="com.zui.calendar.timetable.management.EditTimetableActivity", sig=[])
        self.assertFalse(r["ok"], "EditTimetableActivity 误判命中 TimetableActivity")
        # 真正的那个 TimetableActivity 仍应命中
        r2 = checkpoint.evaluate(
            checkpoint.parse_expect("activity=TimetableActivity"),
            activity="com.zui.calendar.timetable.display.TimetableActivity", sig=[])
        self.assertTrue(r2["ok"])
        # 同后缀但不同类（SettingsActivity vs GeneralSettingsActivity）也不能误命中
        r3 = checkpoint.evaluate(
            checkpoint.parse_expect("activity=SettingsActivity"),
            activity="com.zui.calendar.GeneralSettingsActivity", sig=[])
        self.assertFalse(r3["ok"])

    def test_full_activity_name_also_matches(self):
        r = checkpoint.evaluate(
            checkpoint.parse_expect("activity=.timetable.management.EditTimetableActivity"),
            activity="com.zui.calendar.timetable.management.EditTimetableActivity", sig=[])
        self.assertTrue(r["ok"])

    def test_missed_reports_actual_activity(self):
        r = checkpoint.evaluate(
            checkpoint.parse_expect("activity=TimetableActivity"),
            activity="com.zui.calendar.timetable.management.EditTimetableActivity", sig=[])
        self.assertFalse(r["ok"])
        self.assertTrue(any("EditTimetableActivity" in m for m in r["missed"]))

    def test_transient_popup_only_matches_known_specs(self):
        self.assertIsNotNone(checkpoint.detect_transient(
            ["rid=timetable_guide_button", "text=我知道了"]))
        # 未知画面**不猜** —— 交回给 AI 判断
        self.assertIsNone(checkpoint.detect_transient(["rid=et_schedule_name"]))
        self.assertIsNone(checkpoint.detect_transient([]))


class PathMapTest(unittest.TestCase):
    """路径图执行器：把已有的边变成可批量走的计划（跨设备用锚点，不写死坐标）。"""

    SAMPLE = """
## 日历首页

- **activity**: `.AllInOneActivity`
- **识别键**: `rid=iv_more` · `text=今天`

**出边**：

| 锚点（认得出就行） | rid | desc | text | → 去向 |
|---|---|---|---|---|
| 右上角「更多」 | `iv_more` | — | — | → 菜单 |

---

## 菜单（弹层）

- **识别键**: `text=课程表`

**出边**：

| 锚点 | rid | desc | text | → 去向 |
|---|---|---|---|---|
| 菜单项「课程表」 | `item_title` ⚠️多态 | — | 课程表 | → 课程表页-空态 |

---

## 课程表页-空态

- **activity**: `.timetable.display.TimetableActivity`
- **识别键**: `text=还未添加课程表` · `rid=btnCreateManually`

**出边**：

| 锚点 | rid | desc | text | → 去向 |
|---|---|---|---|---|
| 「手动创建课程表」 | `btnCreateManually` | — | 手动创建课程表 | → 课程表基本信息编辑页 |

---

## 课程表基本信息编辑页

- **activity**: `.timetable.management.EditTimetableActivity`
- **识别键**: `text=课程表名称（必填）`
"""

    def test_parses_nodes_and_edges(self):
        nodes = pathmap.parse_nodes(self.SAMPLE)
        names = [n["name"] for n in nodes]
        self.assertIn("日历首页", names)
        self.assertIn("课程表页-空态", names)
        home = pathmap.find_node(nodes, "日历首页")
        self.assertEqual(home["activity"], ".AllInOneActivity")
        self.assertEqual(len(home["edges"]), 1)

    def test_header_row_is_not_an_edge(self):
        """⚠️ 回归：`| 锚点… | rid | desc | text | → 去向 |` 表头不能被当成一条边。"""
        nodes = pathmap.parse_nodes(self.SAMPLE)
        home = pathmap.find_node(nodes, "日历首页")
        self.assertEqual(len(home["edges"]), 1, "表头行被误当成边")
        self.assertNotEqual(home["edges"][0]["rid"], "rid")

    def test_dash_means_empty_not_a_value(self):
        nodes = pathmap.parse_nodes(self.SAMPLE)
        home = pathmap.find_node(nodes, "日历首页")
        e = home["edges"][0]
        self.assertEqual(e["desc"], "")
        self.assertEqual(e["text"], "")

    def test_multi_state_rid_annotation_is_stripped(self):
        nodes = pathmap.parse_nodes(self.SAMPLE)
        menu = pathmap.find_node(nodes, "菜单（弹层）")
        self.assertEqual(menu["edges"][0]["rid"], "item_title")

    def test_selector_prefers_rid_then_desc_then_text(self):
        self.assertEqual(pathmap.edge_selector({"rid": "iv_more"}), "rid=iv_more")
        self.assertEqual(pathmap.edge_selector({"desc": "导入课程表"}), "desc=导入课程表")
        self.assertEqual(pathmap.edge_selector({"text": "完成"}), "text=完成")

    def test_same_rid_multi_state_carries_text_to_disambiguate(self):
        """同 rid 多态（item_title）必须带 text，否则会点到别的菜单项。"""
        sel = pathmap.edge_selector({"rid": "item_title", "text": "课程表"})
        self.assertEqual(sel, "rid=item_title,text=课程表")

    def test_path_between_finds_shortest_route(self):
        nodes = pathmap.parse_nodes(self.SAMPLE)
        r = pathmap.path_between(nodes, "日历首页", "课程表基本信息编辑页")
        self.assertTrue(r["found"])
        self.assertEqual(r["nodes"], ["日历首页", "菜单（弹层）", "课程表页-空态",
                                      "课程表基本信息编辑页"])
        self.assertEqual(len(r["steps"]), 3)

    def test_path_between_reports_missing_nodes(self):
        nodes = pathmap.parse_nodes(self.SAMPLE)
        r = pathmap.path_between(nodes, "日历首页", "不存在的页")
        self.assertFalse(r["found"])
        self.assertIn("找不到节点", r["reason"])

    def test_path_between_reports_no_route(self):
        nodes = pathmap.parse_nodes(self.SAMPLE)
        r = pathmap.path_between(nodes, "课程表基本信息编辑页", "日历首页")
        self.assertFalse(r["found"])          # 没有反向边

    def test_edges_carry_from_and_to_for_resume(self):
        nodes = pathmap.parse_nodes(self.SAMPLE)
        r = pathmap.path_between(nodes, "日历首页", "课程表基本信息编辑页")
        self.assertEqual(r["steps"][0]["from"], "日历首页")
        self.assertEqual(r["steps"][0]["to"], "菜单（弹层）")

    def test_finish_expect_prefers_activity(self):
        nodes = pathmap.parse_nodes(self.SAMPLE)
        exp = pathmap.finish_expect(pathmap.find_node(nodes, "课程表基本信息编辑页"))
        self.assertIn("activity=timetable.management.EditTimetableActivity", exp)

    def test_finish_expect_prefers_rid_over_text(self):
        """⚠️ 回归：识别键要挑**稳定**的 —— rid 优先于 text。

        「课程表基本信息编辑页」的 text 键是占位符「课程表名称（必填）」，
        一旦填了名字就消失；用它做检查点会在编辑态误报未命中（真实事故：会话 334）。
        """
        node = {"activity": ".a.B", "keys": [("text", "课程表名称（必填）"),
                                             ("rid", "et_schedule_name")]}
        exp = pathmap.finish_expect(node)
        self.assertIn("rid=et_schedule_name", exp)
        self.assertNotIn("text=", exp)

    def test_finish_expect_falls_back_to_text_when_no_rid(self):
        node = {"activity": "", "keys": [("text", "还未添加课程表")]}
        self.assertEqual(pathmap.finish_expect(node), "text=还未添加课程表")


class AnchorMatchTest(unittest.TestCase):
    """锚点定位：坐标必须现场派生，且**不能猜**。"""

    NODES = [
        {"center": [2853, 391], "rid": "com.zui.calendar:id/tv_schedule_info", "text": "设置"},
        {"center": [1520, 1694], "rid": "com.zui.calendar:id/layout_time_settings"},
    ]

    def test_rid_match_is_used_first(self):
        hit = act._find_anchor(self.NODES, [], {"rid": ["tv_schedule_info"]})
        self.assertIsNotNone(hit)
        self.assertEqual(hit["x"], 2853)
        self.assertEqual(hit["how"], "rid=tv_schedule_info")

    def test_same_rid_multi_state_requires_text_to_agree(self):
        """同 rid 多态：带了 text 就必须文字对上，否则不能点（否则点错项）。"""
        nodes = [
            {"center": [100, 200], "rid": "item_title", "text": "纵览"},
            {"center": [300, 400], "rid": "item_title", "text": "课程表"},
        ]
        hit = act._find_anchor(nodes, [], {"rid": ["item_title"], "text": ["课程表"]})
        self.assertIsNotNone(hit)
        self.assertEqual(hit["x"], 300)

    def test_text_fallback_is_exact_not_substring(self):
        """⚠️ 回归：text 兜底必须**整串相等**，不能用 `in`。

        真实事故（会话 334）：找 `text=设置` 时子串匹配命中了另一个含"设置"的
        容器 `layout_time_settings`，点到了完全不同的行，落点跑到
        TimeSlotSettingsActivity —— 而检查点本应期待编辑页。
        宁可报 anchor_not_found，也不要猜一个"差不多"的。
        """
        nodes = [{"center": [1520, 1694], "rid": "layout_time_settings",
                  "text": "课程时间设置"}]
        self.assertIsNone(act._find_anchor(nodes, [], {"text": ["设置"]}),
                          "子串不应命中")
        # 整串相等才命中
        self.assertIsNotNone(act._find_anchor(nodes, [], {"text": ["课程时间设置"]}))

    def test_missing_anchor_returns_none(self):
        self.assertIsNone(act._find_anchor(self.NODES, [], {"rid": ["does_not_exist"]}))


if __name__ == "__main__":
    unittest.main()
