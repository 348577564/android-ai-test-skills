"""测试会话 SQLite 持久化（v2：围绕 AI 会话，不围绕脚本）。

三张表：
  sessions  一次测试会话（用户口述用例 + 结论）
  events    统一事件流：observe/act/read 每次工具调用自动落一条（append-only）
  findings  断言/结论（从事件流独立出来，方便聚合统计）

零依赖（标准库 sqlite3）。库位置：<skill根>/storage/sessions.db。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from runtime import CALL_ID, STORAGE_DIR, recording_issues

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = STORAGE_DIR / "sessions.db"
CURRENT_FILE = STORAGE_DIR / "current_session"

STATUSES = ("PASS", "FAIL", "WARN", "BLOCKED", "ERROR")
# 会话中间态：不是结论，不计入通过率统计；必须以 pause 保持或 finish 收尾
INTERIM = ("running", "paused", "waiting")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT,
  user_input TEXT,
  package TEXT,
  related TEXT,
  device TEXT,
  case_id INTEGER,
  status TEXT DEFAULT 'running',
  kind TEXT DEFAULT 'test',
  terminated INTEGER DEFAULT 0,
  terminate_reason TEXT,
  perm_intent_action TEXT,
  perm_intent_perm TEXT,
  perm_intent_at TEXT,
  knowledge_shown INTEGER DEFAULT 0,
  summary TEXT,
  report_path TEXT,
  started_at TEXT,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS cases(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  input TEXT NOT NULL,
  package TEXT,
  kind TEXT DEFAULT 'test',
  last_session_id INTEGER,
  last_run_at TEXT,
  last_status TEXT,
  run_count INTEGER DEFAULT 0,
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  seq INTEGER,
  kind TEXT,
  tool TEXT,
  detail TEXT,
  data_json TEXT,
  evidence TEXT,
  duration_ms INTEGER,
  started_at TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS findings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  event_id INTEGER,
  status TEXT,
  expect TEXT,
  actual TEXT,
  note TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS finding_events(
  finding_id INTEGER NOT NULL REFERENCES findings(id),
  event_id INTEGER NOT NULL REFERENCES events(id),
  PRIMARY KEY(finding_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_findings_session ON findings(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_kind ON sessions(kind);
"""

# 会话类型：
#   test = 正式测试（进「测试记录」，计入仪表盘成功率）
#   diag = 验证/调试（工具自检、探针；默认不显示，不计入）
#   mock = 占位/构造数据（冒烟样例、手工造的演示数据；**永不入库**）
SESSION_KINDS = ("test", "diag", "mock")
# 只有这些类型算正式记录（进列表与统计）
FORMAL_KINDS = ("test",)


class SessionDB:
    """Thread-local connections; trust-critical writes raise on failure."""

    def __init__(self, path: Path = DB_PATH):
        self.path = str(path)
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        if getattr(self._local, "conn", None) is None:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            # 幂等迁移：早期建的表补列
            for stmt in ("ALTER TABLE events ADD COLUMN duration_ms INTEGER",
                         "ALTER TABLE events ADD COLUMN started_at TEXT",
                         "ALTER TABLE sessions ADD COLUMN related TEXT",
                         "ALTER TABLE sessions ADD COLUMN case_id INTEGER",
                         "ALTER TABLE sessions ADD COLUMN kind TEXT DEFAULT 'test'",
                         "ALTER TABLE cases ADD COLUMN kind TEXT DEFAULT 'test'",
                         "ALTER TABLE sessions ADD COLUMN terminated INTEGER DEFAULT 0",
                         "ALTER TABLE sessions ADD COLUMN terminate_reason TEXT",
                         "ALTER TABLE sessions ADD COLUMN perm_intent_action TEXT",
                         "ALTER TABLE sessions ADD COLUMN perm_intent_perm TEXT",
                         "ALTER TABLE sessions ADD COLUMN perm_intent_at TEXT",
                         "ALTER TABLE sessions ADD COLUMN knowledge_shown INTEGER DEFAULT 0",
                         "ALTER TABLE events ADD COLUMN call_id TEXT"):
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass
            conn.commit()
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_call ON events(call_id)")
            conn.commit()
            self._local.conn = conn
        return self._local.conn

    @staticmethod
    def _now() -> str:
        # 毫秒精度：相邻事件差 = AI 决策耗时（时间轴展示依赖它）
        return datetime.now().isoformat(timespec="milliseconds")

    # ── 写 ──────────────────────────────────────────────────────
    def start_session(self, title: str, user_input: str, device: str | None = None,
                      case_id: int | None = None, kind: str = "test") -> int:
        if kind not in SESSION_KINDS:
            kind = "test"
        cur = self._conn().execute(
            "INSERT INTO sessions (title, user_input, device, case_id, kind, started_at)"
            " VALUES (?,?,?,?,?,?)",
            (title, user_input, device, case_id, kind, self._now()))
        self._conn().commit()
        return cur.lastrowid

    def log_event(self, session_id: int, kind: str, tool: str, detail: str = "",
                  data: dict | None = None, evidence: str | None = None,
                  duration_ms: int | None = None,
                  started_at: str | None = None) -> int | None:
        conn = self._conn()
        with conn:
            if not conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                raise ValueError(f"Session {session_id} does not exist")
            seq = self._conn().execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE session_id=?",
                (session_id,)).fetchone()[0]
            cur = self._conn().execute(
                "INSERT INTO events (session_id, seq, kind, tool, detail, data_json,"
                " evidence, duration_ms, started_at, created_at, call_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, seq, kind, tool, detail,
                 json.dumps(data, ensure_ascii=False) if data else None,
                 evidence, duration_ms, started_at, self._now(), CALL_ID))
            return cur.lastrowid

    def add_finding(self, session_id: int, status: str, expect: str = "",
                    actual: str = "", note: str = "", event_id: int | None = None,
                    event_ids: list[int] | None = None) -> int:
        if status not in STATUSES:
            raise ValueError(f"Invalid finding status: {status}")
        ids = list(dict.fromkeys(([event_id] if event_id is not None else []) + (event_ids or [])))
        if not actual.strip():
            raise ValueError("Finding actual observation/reason is required")
        if status in ("PASS", "FAIL", "WARN") and (not ids or not expect.strip()):
            raise ValueError("Verified findings require an expectation and explicit evidence events")
        conn = self._conn()
        with conn:
            session = conn.execute("SELECT status FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not session or session["status"] not in INTERIM:
                raise ValueError("Findings require an existing unfinished session")
            for eid in ids:
                self._validate_evidence(conn, session_id, eid)
            cur = self._conn().execute(
                "INSERT INTO findings (session_id, event_id, status, expect, actual,"
                " note, created_at) VALUES (?,?,?,?,?,?,?)",
                (session_id, ids[0] if ids else None, status, expect, actual, note, self._now()))
            fid = cur.lastrowid
            conn.executemany("INSERT INTO finding_events VALUES (?,?)", [(fid, eid) for eid in ids])
            return fid

    @staticmethod
    def _validate_evidence(conn: sqlite3.Connection, session_id: int, event_id: int) -> None:
        event = conn.execute(
            "SELECT session_id, evidence FROM events WHERE id=?", (event_id,)).fetchone()
        if not event or event["session_id"] != session_id or not event["evidence"]:
            raise ValueError(f"Event {event_id} must contain evidence from session {session_id}")
        path = Path(event["evidence"])
        if not path.is_absolute():
            path = ROOT / path
        if not path.is_file():
            raise ValueError(f"Event {event_id} evidence is missing: {path}")

    def finish_session(self, session_id: int, status: str, summary: str = "",
                       package: str | None = None) -> bool:
        if status not in STATUSES:
            raise ValueError(f"Invalid session status: {status}")
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not row or row["status"] not in INTERIM:
                raise ValueError("Finish requires an existing unfinished session")
            findings = conn.execute("SELECT * FROM findings WHERE session_id=?", (session_id,)).fetchall()
            statuses = {finding["status"] for finding in findings}
            if row["terminated"] and status != "BLOCKED":
                raise ValueError("Terminated sessions must finish BLOCKED")
            if status in ("PASS", "WARN"):
                allowed = {"PASS"} if status == "PASS" else {"PASS", "WARN"}
                if not findings or not statuses <= allowed:
                    raise ValueError("PASS/WARN requires verified, non-blocked findings with no failures")
            if status == "FAIL" and "FAIL" not in statuses:
                raise ValueError("FAIL requires a FAIL finding")
            if status in ("BLOCKED", "ERROR") and not summary.strip():
                raise ValueError("BLOCKED/ERROR requires a reason in summary")
            if status not in ("ERROR", "BLOCKED") and recording_issues(Path(self.path).parent, session_id):
                raise ValueError("Recording/ledger is incomplete; finish ERROR/BLOCKED with a reason")
            for finding in findings if status in ("PASS", "FAIL", "WARN") else []:
                if finding["status"] not in ("PASS", "FAIL", "WARN"):
                    continue
                ids = [r[0] for r in conn.execute(
                    "SELECT event_id FROM finding_events WHERE finding_id=?", (finding["id"],))]
                if finding["event_id"] is not None and finding["event_id"] not in ids:
                    ids.append(finding["event_id"])
                if not ids or not finding["expect"].strip() or not finding["actual"].strip():
                    raise ValueError(f"Finding {finding['id']} is missing evidence or concrete observations")
                for eid in ids:
                    self._validate_evidence(conn, session_id, eid)
            conn.execute(
                "UPDATE sessions SET status=?, summary=?, finished_at=?,"
                " package=COALESCE(?, package) WHERE id=?",
                (status, summary, self._now(), package, session_id))
            if row["case_id"]:
                updated = conn.execute(
                    "UPDATE cases SET package=COALESCE(package, ?), last_session_id=?,"
                    " last_run_at=?, last_status=?, run_count=COALESCE(run_count,0)+1 WHERE id=?",
                    (package or row["package"], session_id, self._now(), status, row["case_id"]))
                if updated.rowcount != 1:
                    raise ValueError("Associated case is missing")
            conn.commit()
            return True
        except (sqlite3.Error, ValueError, OSError):
            conn.rollback()
            raise

    def set_package(self, session_id: int, package: str) -> None:
        """首个 observe 回填被测包名（已有值不覆盖）。"""
        try:
            self._conn().execute(
                "UPDATE sessions SET package=? WHERE id=? AND (package IS NULL OR package='')",
                (package, session_id))
            self._conn().commit()
        except sqlite3.Error:
            pass

    def get_session_package(self, session_id: int) -> str | None:
        """会话的被测包名（崩溃分级的 target 依据）。

        用会话声明而非"当前前台包"：被测 App 崩溃后前台回落到桌面，
        拿前台包分类会把被测包的崩溃误判成 other 而放过。
        """
        try:
            row = self._conn().execute(
                "SELECT package FROM sessions WHERE id=?", (session_id,)).fetchone()
            return (row["package"] or None) if row else None
        except (sqlite3.Error, TypeError):
            return None

    def terminate_session(self, session_id: int, reason: str) -> None:
        """标记会话为**终态**：被测/关联包崩溃后，后续动作一律拒绝执行。

        这是工具层硬约束（不只靠 AI 自觉读 `terminate` 信号）：
        崩溃一旦发生，继续观测/点击只会产生不可信的数据和多余动作。
        """
        try:
            self._conn().execute(
                "UPDATE sessions SET terminated=1, terminate_reason=? WHERE id=?",
                (reason, session_id))
            self._conn().commit()
        except sqlite3.Error:
            pass

    def get_termination(self, session_id: int) -> str | None:
        """会话是否已终止；返回终止原因（None=未终止，可继续执行）。"""
        row = self._conn().execute(
            "SELECT terminated, terminate_reason FROM sessions WHERE id=?",
            (session_id,)).fetchone()
        if row is None:
            raise ValueError(f"Session {session_id} does not exist")
        if row["terminated"]:
            return row["terminate_reason"] or "会话已终止"
        return None

    def clear_termination(self, session_id: int) -> bool:
        """清除终态（resume 时用：人工确认后允许继续）。"""
        try:
            self._conn().execute(
                "UPDATE sessions SET terminated=0, terminate_reason=NULL WHERE id=?",
                (session_id,))
            self._conn().commit()
            return True
        except sqlite3.Error:
            return False

    # ── 权限测试意图（声明式，跨命令持久）──────────────────────────
    # 设计参考 AiAgentTest 的 set_permission_intent：AI 在分支开头声明
    # "接下来遇到权限弹窗该点同意还是拒绝"，之后每次 act/observe 自动按此响应。
    # 关键：决策**提前做完**，动作时只执行——弹窗约 6s 自动消失，来不及现场想。
    PERM_INTENT_TTL_S = 600.0   # 10 分钟过期（与 AiAgentTest 一致）

    def set_perm_intent(self, session_id: int, action: str,
                        permission: str = "") -> bool:
        """action ∈ {"grant","deny"}；permission 仅作标记（如 media_images）。"""
        if action not in ("grant", "deny"):
            return False
        try:
            self._conn().execute(
                "UPDATE sessions SET perm_intent_action=?, perm_intent_perm=?,"
                " perm_intent_at=? WHERE id=?",
                (action, (permission or "").strip(), self._now(), session_id))
            self._conn().commit()
            return True
        except sqlite3.Error:
            return False

    def clear_perm_intent(self, session_id: int) -> bool:
        try:
            self._conn().execute(
                "UPDATE sessions SET perm_intent_action=NULL, perm_intent_perm=NULL,"
                " perm_intent_at=NULL WHERE id=?", (session_id,))
            self._conn().commit()
            return True
        except sqlite3.Error:
            return False

    def get_perm_intent(self, session_id: int) -> dict | None:
        """返回 {"action","permission"}；无意图或已过期返回 None。"""
        try:
            row = self._conn().execute(
                "SELECT perm_intent_action, perm_intent_perm, perm_intent_at"
                " FROM sessions WHERE id=?", (session_id,)).fetchone()
        except sqlite3.Error:
            return None
        if not row or not row["perm_intent_action"]:
            return None
        at = row["perm_intent_at"] or ""
        try:
            set_at = datetime.fromisoformat(at)
            if (datetime.now() - set_at).total_seconds() > self.PERM_INTENT_TTL_S:
                return None      # 过期即视为未声明
        except (ValueError, TypeError):
            return None
        return {"action": row["perm_intent_action"],
                "permission": row["perm_intent_perm"] or ""}

    def mark_knowledge_shown(self, session_id: int) -> None:
        """记录"知识卡提示已给过"——避免每步重复带索引浪费 token。"""
        try:
            self._conn().execute(
                "UPDATE sessions SET knowledge_shown=1 WHERE id=?", (session_id,))
            self._conn().commit()
        except sqlite3.Error:
            pass

    def knowledge_shown(self, session_id: int) -> bool:
        try:
            row = self._conn().execute(
                "SELECT knowledge_shown FROM sessions WHERE id=?",
                (session_id,)).fetchone()
            return bool(row and row["knowledge_shown"])
        except (sqlite3.Error, TypeError, IndexError):
            return False

    def set_related(self, session_id: int, related: list[str]) -> None:
        try:
            self._conn().execute("UPDATE sessions SET related=? WHERE id=?",
                                 (",".join(related), session_id))
            self._conn().commit()
        except sqlite3.Error:
            pass

    def get_related(self, session_id: int) -> list[str]:
        """关联包列表（崩溃分级的"关联"判定域）。"""
        try:
            row = self._conn().execute(
                "SELECT related FROM sessions WHERE id=?", (session_id,)).fetchone()
            return [x.strip() for x in (row["related"] or "").split(",") if x.strip()]
        except (sqlite3.Error, TypeError):
            return []

    def set_session_status(self, session_id: int, status: str) -> bool:
        """中间态切换（paused/waiting/running）。结论态必须走 finish_session。"""
        if status not in INTERIM:
            return False
        try:
            self._conn().execute("UPDATE sessions SET status=? WHERE id=?",
                                 (status, session_id))
            self._conn().commit()
            return True
        except sqlite3.Error:
            return False

    def finding_stats(self, session_id: int) -> dict:
        """各结论的断言计数（finish 自检用：有 FAIL 时结论不得 PASS）。"""
        try:
            rows = self._conn().execute(
                "SELECT status, COUNT(*) FROM findings WHERE session_id=?"
                " GROUP BY status", (session_id,)).fetchall()
            return {r[0]: r[1] for r in rows}
        except sqlite3.Error:
            return {}

    def count_crash_events(self, session_id: int) -> int:
        try:
            return self._conn().execute(
                "SELECT COUNT(*) FROM events WHERE session_id=?"
                " AND data_json LIKE '%crash_detected%'",
                (session_id,)).fetchone()[0]
        except sqlite3.Error:
            return 0

    # ── 用例库（正式用例资产：口述文本的沉淀与复用）─────────────────
    def add_case(self, title: str, user_input: str, package: str | None = None,
                 kind: str = "test") -> int:
        if kind not in SESSION_KINDS:
            kind = "test"
        now = self._now()
        cur = self._conn().execute(
            "INSERT INTO cases (title, input, package, kind, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)", (title, user_input, package, kind, now, now))
        self._conn().commit()
        return cur.lastrowid

    def set_case_kind(self, case_id: int, kind: str) -> bool:
        """切换用例类型（test 正式 / mock 占位，不入库；diag 预留）。"""
        if kind not in SESSION_KINDS:
            return False
        try:
            self._conn().execute(
                "UPDATE cases SET kind=?, updated_at=? WHERE id=?",
                (kind, self._now(), case_id))
            self._conn().commit()
            return True
        except sqlite3.Error:
            return False

    def update_case(self, case_id: int, title: str | None = None,
                    user_input: str | None = None, package: str | None = None) -> bool:
        try:
            self._conn().execute(
                "UPDATE cases SET"
                " title=COALESCE(?, title), input=COALESCE(?, input),"
                " package=COALESCE(?, package), updated_at=? WHERE id=?",
                (title, user_input, package, self._now(), case_id))
            self._conn().commit()
            return True
        except sqlite3.Error:
            return False

    def get_case(self, case_id: int) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        return dict(row) if row else None

    def list_cases(self, limit: int = 100, include_mock: bool = False) -> list[dict]:
        """用例列表。默认排除 mock（占位/构造数据，不该出现在用例库）。"""
        sql = ("SELECT c.*,"
               " (SELECT COUNT(*) FROM sessions s WHERE s.case_id=c.id) AS session_count"
               " FROM cases c")
        if not include_mock:
            sql += " WHERE COALESCE(c.kind,'test') != 'mock'"
        sql += " ORDER BY c.id DESC LIMIT ?"
        rows = self._conn().execute(sql, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def module_stats(self) -> list[dict]:
        """按模块（包名）聚合用例数与结论分布（仪表盘/用例库分组）。

        package 为空时**不再一律归成「未识别」**：优先从该用例最近一次
        关联会话的 package 兜底，仍为空才记「未识别」。否则历史用例
        （建卡时没写包名）会全挤进一个桶，模块分类失去意义（真实 bug）。
        """
        rows = self._conn().execute(
            "SELECT COALESCE(NULLIF(c.package,''),"
            "                NULLIF((SELECT s.package FROM sessions s"
            "                        WHERE s.id=c.last_session_id),''),"
            "                '未识别') AS package,"
            " COUNT(*) AS total,"
            " SUM(CASE WHEN c.last_status='PASS' THEN 1 ELSE 0 END) AS pass,"
            " SUM(CASE WHEN c.last_status='FAIL' THEN 1 ELSE 0 END) AS fail,"
            " SUM(CASE WHEN c.last_status='BLOCKED' THEN 1 ELSE 0 END) AS blocked,"
            " SUM(CASE WHEN c.last_status='WARN' THEN 1 ELSE 0 END) AS warn,"
            " SUM(CASE WHEN c.last_status='ERROR' THEN 1 ELSE 0 END) AS error,"
            " SUM(CASE WHEN c.last_status IS NULL THEN 1 ELSE 0 END) AS never_run"
            " FROM cases c"
            " WHERE COALESCE(c.kind,'test') != 'mock'"
            " GROUP BY 1 ORDER BY total DESC").fetchall()
        return [dict(r) for r in rows]

    def delete_case(self, case_id: int) -> bool:
        """删除用例（不牵连历史会话：sessions.case_id 置空而非级联删）。"""
        try:
            cur = self._conn().execute("DELETE FROM cases WHERE id=?", (case_id,))
            self._conn().execute(
                "UPDATE sessions SET case_id=NULL WHERE case_id=?", (case_id,))
            self._conn().commit()
            return cur.rowcount > 0
        except sqlite3.Error:
            return False

    def delete_session(self, session_id: int, remove_evidence: bool = True) -> dict:
        """删除一次测试记录：数据库行 + **连带磁盘证据**（截图/日志）。

        返回 {"ok","files","dirs"}。证据定位走三条线索（缺一会漏删）：
          ① events.evidence 字段记的相对路径；
          ② storage/{evidence,logs}/<会话id>/ 自动命名目录；
          ③ ①中每个文件**所在的步骤目录**（shot.png 与 dump.xml/meta.json 同目录，
             只删 events 里那一个文件会把 dump/meta 留成孤儿）。
        """
        import shutil

        files, dirs = 0, 0
        try:
            rows = self._conn().execute(
                "SELECT evidence FROM events WHERE session_id=?", (session_id,)).fetchall()
            ev_paths = [r["evidence"] for r in rows if r["evidence"]]

            if remove_evidence:
                storage_root = Path(self.path).parent.resolve()
                step_dirs: set[Path] = set()
                for rel_p in ev_paths:
                    try:
                        fp = (ROOT / rel_p).resolve()
                        # 只删 storage 内的证据，防路径穿越
                        if not fp.is_relative_to(storage_root):
                            continue
                        if fp.is_file():
                            fp.unlink()
                            files += 1
                        # 该文件的父目录 = 一个步骤证据目录，整目录收掉
                        if fp.parent != storage_root and fp.parent.is_dir():
                            step_dirs.add(fp.parent)
                    except OSError:
                        pass
                # 步骤目录整棵删（含同目录的 dump.xml / meta.json / crash.log）
                for d in step_dirs:
                    n = sum(1 for _ in d.rglob("*") if _.is_file())
                    if shutil.rmtree(d, ignore_errors=True) or not d.exists():
                        files += n
                        dirs += 1
                # 自动命名目录 <会话id>/ 整棵删
                for base in ("evidence", "logs"):
                    d = Path(self.path).parent / base / str(session_id)
                    if d.is_dir():
                        n = sum(1 for _ in d.rglob("*") if _.is_file())
                        if shutil.rmtree(d, ignore_errors=True) or not d.exists():
                            files += n
                            dirs += 1
                # 清掉遗留的空目录（只清空的，不动有内容的）
                ev_root = Path(self.path).parent / "evidence"
                if ev_root.is_dir():
                    for sub in sorted(ev_root.rglob("*"), reverse=True):
                        if sub.is_dir() and not any(sub.iterdir()):
                            try:
                                sub.rmdir()
                                dirs += 1
                            except OSError:
                                pass

            cur = self._conn().execute("DELETE FROM sessions WHERE id=?", (session_id,))
            self._conn().execute("DELETE FROM events WHERE session_id=?", (session_id,))
            self._conn().execute(
                "DELETE FROM finding_events WHERE finding_id IN "
                "(SELECT id FROM findings WHERE session_id=?)", (session_id,))
            self._conn().execute("DELETE FROM findings WHERE session_id=?", (session_id,))
            self._conn().commit()
            return {"ok": cur.rowcount > 0, "files": files, "dirs": dirs}
        except sqlite3.Error:
            return {"ok": False, "files": files, "dirs": dirs}

    def set_session_kind(self, session_id: int, kind: str) -> bool:
        """切换会话类型（test=进测试记录 / diag=验证调试，默认不展示）。"""
        if kind not in SESSION_KINDS:
            return False
        try:
            self._conn().execute("UPDATE sessions SET kind=? WHERE id=?",
                                 (kind, session_id))
            self._conn().commit()
            return True
        except sqlite3.Error:
            return False

    def run_stats(self, days: int = 3) -> list[dict]:
        """最近 N 天每天的执行情况（仪表盘柱状图：成功率/失败率）。

        只统计正式测试（kind='test'），验证/调试会话不计入，
        否则自测噪音会污染成功率曲线。
        """
        try:
            rows = self._conn().execute(
                "SELECT substr(started_at,1,10) AS day, COUNT(*) AS total,"
                " SUM(CASE WHEN status='PASS' THEN 1 ELSE 0 END) AS pass,"
                " SUM(CASE WHEN status='FAIL' THEN 1 ELSE 0 END) AS fail,"
                " SUM(CASE WHEN status='BLOCKED' THEN 1 ELSE 0 END) AS blocked,"
                " SUM(CASE WHEN status NOT IN ('PASS','FAIL','BLOCKED')"
                "     THEN 1 ELSE 0 END) AS other"
                " FROM sessions WHERE kind='test'"
                # date('now') 是 UTC，而 started_at 是本地时间——
                # 用 'localtime' 对齐，否则跨天边界（本地 00:00~08:00）会算错一天。
                "   AND substr(started_at,1,10) >= date('now','localtime', ?)"
                " GROUP BY day ORDER BY day DESC", (f"-{max(0, days - 1)} day",)).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error:
            return []

    def case_overview(self) -> dict:
        """仪表盘汇总：用例总数 + 最近结论分布。"""
        try:
            row = self._conn().execute(
                "SELECT COUNT(*) AS total,"
                " SUM(CASE WHEN last_status='PASS' THEN 1 ELSE 0 END) AS pass,"
                " SUM(CASE WHEN last_status='FAIL' THEN 1 ELSE 0 END) AS fail,"
                " SUM(CASE WHEN last_status='BLOCKED' THEN 1 ELSE 0 END) AS blocked,"
                " SUM(CASE WHEN last_status='WARN' THEN 1 ELSE 0 END) AS warn,"
                " SUM(CASE WHEN last_status='ERROR' THEN 1 ELSE 0 END) AS error,"
                " SUM(CASE WHEN last_status IS NULL THEN 1 ELSE 0 END) AS never_run"
                " FROM cases WHERE COALESCE(kind,'test') != 'mock'").fetchone()
            return dict(row) if row else {}
        except sqlite3.Error:
            return {}

    def touch_case(self, case_id: int, session_id: int, status: str) -> None:
        """会话 finish 时回写用例的运行统计。"""
        try:
            self._conn().execute(
                "UPDATE cases SET last_session_id=?, last_run_at=?, last_status=?,"
                " run_count=COALESCE(run_count,0)+1 WHERE id=?",
                (session_id, self._now(), status, case_id))
            self._conn().commit()
        except sqlite3.Error:
            pass

    # ── 读（Web UI 用）───────────────────────────────────────────
    def list_sessions(self, limit: int = 100, kind: str | None = "test",
                      include: tuple[str, ...] | None = None) -> list[dict]:
        """会话列表。

        kind='test'（默认）= 只看正式测试记录；
        include=None 且 kind=None → 全部（含 diag/mock，仅调试用）；
        include=("test","diag") → 正式 + 验证调试（mock 仍排除）。
        mock（占位/构造数据）**永不进正式列表**。
        """
        # ⚠️ 时间基准坑（踩过两次，务必看清）：
        #   · julianday('now') 是 **UTC**，而 started_at/finished_at 由
        #     datetime.now() 写成**本地时间**——直接相减差一个时区（UTC+8 得 -28800s）。
        #   · 但 'localtime' 修饰符只能加在 **'now'** 上，**不能加在 finished_at 上**：
        #     julianday(finished_at,'localtime') 的语义是"把 finished_at 当 UTC 再换算"，
        #     而它本来就是本地串 → 对已收尾会话二次偏移（150s 变成 28950s）。
        #   正解：对 'now' 就地取本地时间；finished_at 已是本地串，原样使用。
        sql = ("SELECT s.*, MAX(0, ROUND((julianday(COALESCE(s.finished_at,"
               " datetime('now','localtime'))) - julianday(s.started_at)) * 86400, 1))"
               " AS duration_seconds,"
               " (SELECT COUNT(*) FROM events e WHERE e.session_id=s.id) AS event_count,"
               " (SELECT COUNT(*) FROM findings f WHERE f.session_id=s.id"
               "   AND f.status='FAIL') AS fail_count,"
               " (SELECT COUNT(*) FROM findings f WHERE f.session_id=s.id) AS finding_count"
               " FROM sessions s")
        params: tuple = ()
        if include:
            ph = ",".join("?" for _ in include)
            sql += f" WHERE COALESCE(s.kind,'test') IN ({ph})"
            params = tuple(include)
        elif kind:
            sql += " WHERE COALESCE(s.kind,'test')=?"
            params = (kind,)
        sql += " ORDER BY s.id DESC LIMIT ?"
        rows = self._conn().execute(sql, params + (limit,)).fetchall()
        out = [dict(r) for r in rows]
        # 「回放」入口是否可用：该会话至少有一帧的截图还在磁盘上。
        # ⚠️ 证据可能被 cleanup 轮转或误删 —— 那时按钮要置灰，而不是点进去才发现播不了。
        # 放在这里统一算（列表页要按行显示），避免前端为每条记录各发一次请求。
        for r in out:
            evs = self._conn().execute(
                "SELECT evidence FROM events WHERE session_id=? AND evidence IS NOT NULL"
                " AND evidence != '' LIMIT 50", (r["id"],)).fetchall()
            r["has_evidence"] = any((ROOT / e["evidence"]).is_file() for e in evs)
        return out

    def session_kind_counts(self) -> dict:
        """各类型会话数（测试记录页「显示验证记录」开关用）。

        mock 不计入（它对用户不可见，不该出现在任何计数里）。
        """
        try:
            rows = self._conn().execute(
                "SELECT COALESCE(kind,'test') AS k, COUNT(*) AS n"
                " FROM sessions WHERE COALESCE(kind,'test') != 'mock'"
                " GROUP BY k").fetchall()
            return {r["k"]: r["n"] for r in rows}
        except sqlite3.Error:
            return {}

    def get_session(self, session_id: int) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            return None
        s = dict(row)
        s["events"] = [dict(r) for r in self._conn().execute(
            "SELECT * FROM events WHERE session_id=? ORDER BY seq", (session_id,))]
        s["findings"] = [dict(r) for r in self._conn().execute(
            "SELECT * FROM findings WHERE session_id=? ORDER BY id", (session_id,))]
        for finding in s["findings"]:
            finding["event_ids"] = [r[0] for r in self._conn().execute(
                "SELECT event_id FROM finding_events WHERE finding_id=? ORDER BY event_id",
                (finding["id"],))]
            if finding["event_id"] is not None and finding["event_id"] not in finding["event_ids"]:
                finding["event_ids"].insert(0, finding["event_id"])
        return s

    def session_stats(self) -> dict:
        """正式测试记录的结论统计（kind='test'，验证/调试会话不计入）。"""
        row = self._conn().execute(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN status='PASS' THEN 1 ELSE 0 END) AS pass_n,"
            " SUM(CASE WHEN status='FAIL' THEN 1 ELSE 0 END) AS fail_n,"
            " SUM(CASE WHEN status='BLOCKED' THEN 1 ELSE 0 END) AS blocked_n,"
            " SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running_n,"
            " SUM(CASE WHEN status IN ('paused','waiting') THEN 1 ELSE 0 END) AS paused_n"
            " FROM sessions WHERE COALESCE(kind,'test')='test'").fetchone()
        return dict(row) if row else {}


_DB: SessionDB | None = None


def get_db() -> SessionDB:
    global _DB
    if _DB is None:
        _DB = SessionDB()
    return _DB


# ── 当前会话（工具层自动挂接）────────────────────────────────────
def set_current(session_id: int) -> None:
    CURRENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CURRENT_FILE.write_text(str(session_id), encoding="utf-8")


def clear_current(expected_id: int | None = None) -> None:
    if expected_id is not None:
        try:
            if int(CURRENT_FILE.read_text(encoding="utf-8").strip()) != expected_id:
                return
        except FileNotFoundError:
            return
    CURRENT_FILE.unlink(missing_ok=True)


def current_session() -> int | None:
    explicit = os.environ.get("ANDROID_TEST_SESSION_ID")
    if explicit is not None:
        return int(explicit)
    try:
        return int(CURRENT_FILE.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return None
