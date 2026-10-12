# -*- coding: utf-8 -*-
"""回归（第二轮 Review）：用带真实 logcat 前缀的样本测崩溃取证全链路。"""
import sys
from pathlib import Path
import atexit
import os
import tempfile

TEST_STORAGE = tempfile.TemporaryDirectory(prefix="android-crash-test-")
os.environ["ANDROID_TEST_STORAGE"] = TEST_STORAGE.name
TOOLS = Path(__file__).resolve().parents[1] / "tools"
STORAGE = Path(TEST_STORAGE.name)
sys.path.insert(0, str(TOOLS))
import logcat  # noqa: E402


def cleanup_test_storage():
    import db
    if db._DB is not None:
        conn = getattr(db._DB._local, "conn", None)
        if conn is not None:
            conn.close()
    TEST_STORAGE.cleanup()


atexit.register(cleanup_test_storage)

# 真实格式：时间 PID TID 级别 Tag: 消息（时间在行首，FATAL 在行中）
RAW = (
    "09-21 17:00:10.123  1000  1000 E AndroidRuntime: FATAL EXCEPTION: main\n"
    "09-21 17:00:10.124  1000  1000 E AndroidRuntime: Process: com.zui.calendar, PID: 12345\n"
    "09-21 17:00:10.125  1000  1000 E AndroidRuntime: java.lang.RuntimeException: boom\n"
    "09-21 17:00:10.130  1000  1000 E AndroidRuntime: \tat com.zui.calendar.MainActivity.onCreate(MainActivity.kt:1)\n"
    "09-21 17:00:12.000  2000  2000 E AndroidRuntime: FATAL EXCEPTION: main\n"
    "09-21 17:00:12.000  2000  2000 E AndroidRuntime: Process: com.zui.calendar, PID: 12346\n"
    "09-21 17:00:12.001  2000  2000 E AndroidRuntime: java.lang.RuntimeException: boom again\n"
)

captured_cmds = []


class FakeResp:
    def __init__(self, out):
        self.output = out


class FakeD:
    def shell(self, cmd):
        captured_cmds.append(cmd)
        # capture_context 现在分两次取：crash 本体优先，再补 main+system 上下文
        if "main,system" in cmd:
            return FakeResp("09-21 16:59:52.000 I Sys: context line\n" * 3)
        if "-b crash" in cmd:
            # detect() 查全量缓冲用 "-b crash -d"；capture_context 用 "-b crash -t"
            return FakeResp(RAW)
        return FakeResp(RAW)


d = FakeD()
for f in STORAGE.glob(".crash_seen_*"):
    f.unlink(missing_ok=True)
ev = STORAGE / "evidence" / "_smoke_crash3"

# 1. 真实前缀样本：process/pid/time 全部提取成功（上一轮的断点就在这）
entries = logcat.parse_entries(RAW)
assert len(entries) == 2, entries
e0 = entries[0]
assert e0["process"] == "com.zui.calendar", e0
assert e0["pid"] == "12345", e0
assert e0["time"] == "09-21 17:00:10.123", e0
assert e0["head"] == "FATAL EXCEPTION: main", e0
print("1 parse_real_prefix_ok  process/pid/time 全提取")

# 2. detect：指纹含 PID → 同 App 同秒两次崩溃（PID 不同）都不吞；20s 上下文时间正确
captured_cmds.clear()
r1 = logcat.detect(d, "com.zui.calendar", [], ev)
assert r1 and r1["new"] and r1["scope"] == "target", r1
# entry = 最高危的那条；latest = 最新的那条。两条都是 target，故取最新做上下文锚点
assert r1["latest"]["pid"] == "12346", r1
assert r1["entry"]["pid"] in ("12345", "12346"), r1
ctx_cmd = [c for c in captured_cmds if "main,system" in c][-1]
# 最新崩溃时刻 17:00:12.000 − 20s = 16:59:52.000，必须出现在抓取命令里
assert "16:59:52.000" in ctx_cmd, ctx_cmd
log_file = Path(r1["log_path"])
assert log_file.exists() and "context line" in log_file.read_text(encoding="utf-8")
print("2 detect_20s_context_ok  since=16:59:50.123  log 已落盘")

# 3. 崩溃风暴：缓冲区追加第 2 条（同 App 同秒、PID 不同）→ 不被吞
for f in STORAGE.glob(".crash_seen_*"):
    f.unlink(missing_ok=True)


class GrowingD:
    """模拟真实追加：第一次只有 12345 的崩溃，第二次追加了 12346 的。"""
    outputs = [RAW.split("09-21 17:00:12")[0], RAW]

    def __init__(self):
        self.calls = 0

    def shell(self, cmd):
        if "main,system,crash" in cmd:
            return FakeResp("09-21 16:59:52.000 I Sys: context line\n")
        out = self.outputs[min(self.calls, len(self.outputs) - 1)]
        self.calls += 1
        return FakeResp(out)


gd = GrowingD()
ra = logcat.detect(gd, "com.zui.calendar", [], ev)
assert ra and ra["new"] and ra["entry"]["pid"] == "12345", ra
rb = logcat.detect(gd, "com.zui.calendar", [], ev)
assert rb and rb["new"] and rb["entry"]["pid"] == "12346", f"崩溃风暴第二条被吞: {rb}"
print("3 crash_storm_ok  同秒双崩溃 PID 区分")

# 4. 第三次读 → 指纹全部见过 → known
r3 = logcat.detect(d, "com.zui.calendar", [], ev)
assert r3 == {"known": True}, r3
print("4 dedup_ok")

# 5. 跨会话隔离：新会话的 mark 文件独立，旧指纹不吞新会话首条崩溃
import db  # noqa: E402
db.CURRENT_FILE.write_text("999", encoding="utf-8")
r4 = logcat.detect(d, "com.zui.calendar", [], ev)
assert r4 and r4["new"], f"跨会话指纹泄漏: {r4}"
db.clear_current()
print("5 session_isolated_ok")

# 清理
for f in STORAGE.glob(".crash_seen_*"):
    f.unlink(missing_ok=True)
import shutil  # noqa: E402
shutil.rmtree(ev, ignore_errors=True)

# 6. ANR 分级（复审 P0：ANR 块无 Process: 行，进程名只能从 "ANR in <pkg>" 提取；
#    缺此分支被测 App ANR 会被判 other 而放过——SKILL.md 承诺 target 含 ANR）
for f in STORAGE.glob(".crash_seen_*"):
    f.unlink(missing_ok=True)
ANR_RAW = (
    "--------- beginning of crash\n"
    "09-21 17:01:00.500  2000  2000 E ActivityManager: ANR in com.zui.calendar\n"
    "09-21 17:01:00.501  2000  2000 E ActivityManager: PID: 999\n"
    "09-21 17:01:00.502  2000  2000 E ActivityManager: Reason: Input dispatching timed out\n"
)


class AnrD:
    def shell(self, cmd):
        if "main,system,crash" in cmd:
            return FakeResp("09-21 17:00:40.000 I Sys: ctx\n")
        return FakeResp(ANR_RAW)


ra = logcat.detect(AnrD(), "com.zui.calendar", [], ev)
assert ra and ra["new"] and ra["scope"] == "target", f"ANR 分级失败: {ra}"
assert ra["entry"]["process"] == "com.zui.calendar", ra
assert ra["entry"]["pid"] == "999", ra
assert ra["log_path"] and Path(ra["log_path"]).exists()
print("6 anr_target_ok  process/pid/classify 全部正确")

# 7. overlay 包不冤枉（classify 精确匹配）
assert logcat.classify("com.zui.calendar.overlay.theme", "com.zui.calendar", []) == "other"
print("7 overlay_not_target_ok")

# 8. resume 守门时序（复审确认过的真实 bug）：无 note 的 resume 被拒后，
#    终态必须保留——否则连调两次 resume 就能绕过人工确认
import subprocess  # noqa: E402
import db  # noqa: E402

for f in STORAGE.glob(".crash_seen_*"):
    f.unlink(missing_ok=True)
sid2 = db.get_db().start_session("resume-guard", "t")
db.set_current(sid2)
db.get_db().terminate_session(sid2, "FATAL EXCEPTION test")
r = subprocess.run([sys.executable, str(TOOLS / "session.py"), "resume", "--id", str(sid2)],
                   capture_output=True)
assert r.returncode == 1, f"无 note 的 resume 应被拒绝: {r.returncode}"
assert db.get_db().get_termination(sid2), "被拒的 resume 清掉了终态——守门可被两次调用绕过"
r = subprocess.run([sys.executable, str(TOOLS / "session.py"), "resume", "--id", str(sid2),
                    "--note", "confirmed"], capture_output=True)
assert r.returncode == 0, r.stderr
assert db.get_db().get_termination(sid2) is None
print("8 resume_guard_ok  拒绝时终态保留，带 note 才解锁")

# ── 9. Review 回归：崩溃分级取最高危（不能只看最新一条）──────────────
# 场景：被测包先崩、随后一个无关包崩。旧逻辑取最新 → other 不阻断，
# 且那条 target 已被写入指纹文件、永不再上报。
for f in STORAGE.glob(".crash_seen_*"):
    f.unlink(missing_ok=True)
MIX_SAME_BATCH = (
    "09-22 10:00:00.000  1000  1000 E AndroidRuntime: FATAL EXCEPTION: main\n"
    "09-22 10:00:00.001  1000  1000 E AndroidRuntime: Process: com.zui.calendar, PID: 111\n"
    "09-22 10:00:05.000  2000  2000 E AndroidRuntime: FATAL EXCEPTION: main\n"
    "09-22 10:00:05.001  2000  2000 E AndroidRuntime: Process: com.other.app, PID: 222\n"
)


class MixD:
    def shell(self, cmd):
        if "main,system" in cmd:
            return FakeResp("09-22 09:59:50.000 I Sys: ctx\n")
        return FakeResp(MIX_SAME_BATCH)


rm = logcat.detect(MixD(), "com.zui.calendar", [], ev)
assert rm and rm["new"], rm
assert rm["scope"] == "target", f"最高危应为 target，实际 {rm['scope']}（旧 bug：取最新=other）"
assert rm["entry"]["process"] == "com.zui.calendar", rm["entry"]
assert rm["latest"]["process"] == "com.other.app", rm["latest"]
assert rm["in_scope_count"] == 1, rm
print("9 crash_grading_ok  同批内 target 与 other 并存时取 target（不再被 other 掩盖）")

# ── 10. Review 回归：nav 模式保留可滑动容器 ─────────────────────────
from common import next_targets, npm_identity, summarize_xml  # noqa: E402
NAV_XML = ('<hierarchy>'
           '<node class="android.widget.ListView" scrollable="true" clickable="false"'
           ' bounds="[0,0][100,200]" resource-id="app:id/list"/>'
           '<node class="android.widget.Button" clickable="true" text="OK"'
           ' bounds="[0,0][50,50]"/>'
           '</hierarchy>')
nav_nodes = summarize_xml(NAV_XML, mode="nav")
assert len(nav_nodes) == 2, f"nav 应保留可滑动容器，实际 {nav_nodes}"
assert any(n.get("s") for n in nav_nodes), f"缺 scrollable 标志: {nav_nodes}"
print("10 nav_keeps_scrollable_ok  不可点但可滑动的容器在 nav 模式下保留")

# ── 11. Review 回归：会话时长（走**生产路径** list_sessions，两种都要对）──
# ⚠️ 教训：早先这用例把 SQL 抄了一份进来，db.py 改坏它照样全绿 —— 必须调真接口。
import db as _dbmod  # noqa: E402
from datetime import datetime as _dt, timedelta as _td  # noqa: E402
_d = _dbmod.get_db()


def _mk_session(title, started, finished=None):
    cur = _d._conn().execute(
        "INSERT INTO sessions (title,user_input,status,kind,started_at,finished_at)"
        " VALUES (?,?,?,?,?,?)",
        (title, "x", "PASS" if finished else "running", "diag",
         started.isoformat(timespec="milliseconds"),
         finished.isoformat(timespec="milliseconds") if finished else None))
    _d._conn().commit()
    return cur.lastrowid


_now = _dt.now()
_s_fin = _mk_session("dur-fin", _now - _td(seconds=150), _now)   # 真实 150s
_s_run = _mk_session("dur-run", _now)                            # 未收尾
_by_id = {r["id"]: r["duration_seconds"] for r in _d.list_sessions(kind=None, limit=300)}

assert 100 <= (_by_id.get(_s_fin) or -1) <= 300, \
    f"已收尾会话时长应为 ~150s（实际 {_by_id.get(_s_fin)}）——localtime 被误加到 finished_at？"
assert _by_id.get(_s_run) == 0, \
    f"未收尾会话时长应夹到 0（实际 {_by_id.get(_s_run)}）"
print(f"11 duration_ok  已收尾 {_by_id[_s_fin]}s / 未收尾 {_by_id[_s_run]}s（走 list_sessions）")
_d.delete_session(_s_fin)
_d.delete_session(_s_run)

# ── 12. Review 回归：同级崩溃 tie-break 取最新（不是最旧）────────────
for f in STORAGE.glob(".crash_seen_*"):
    f.unlink(missing_ok=True)
TIE_RAW = (
    "09-22 10:00:00.000  1000  1000 E AndroidRuntime: FATAL EXCEPTION: main\n"
    "09-22 10:00:00.001  1000  1000 E AndroidRuntime: Process: com.zui.calendar, PID: 111\n"
    "09-22 10:00:05.000  2000  2000 E AndroidRuntime: FATAL EXCEPTION: main\n"
    "09-22 10:00:05.001  2000  2000 E AndroidRuntime: Process: com.zui.calendar, PID: 222\n"
)


class TieD:
    def shell(self, cmd):
        if "main,system" in cmd:
            return FakeResp("09-22 09:59:50.000 I Sys: ctx\n")
        return FakeResp(TIE_RAW)


rt = logcat.detect(TieD(), "com.zui.calendar", [], ev)
# 两条同为 target，tie-break 应取**最新**那条（PID 222）
assert rt["entry"]["pid"] == "222", \
    f"同级 tie-break 应取最新（PID 222），实际 {rt['entry']['pid']}（旧 bug：取最旧）"
assert rt["scope"] == "target", rt
print("12 tie_break_ok  同级崩溃取最新一条（(rank, idx) 排序）")

# ── 13. 权限弹窗匹配：deny 绝不点 dont-ask-again（全项目最高危规则）──
# 点错后果：设置 don't-ask-again → 后续"授予"分支的弹窗**永不再出现**，不可逆。
import perm as _perm  # noqa: E402

_P = "com.android.permissioncontroller:id/"


def _pb(*specs):
    return [{"text": t, "rid": (_P + r) if r else "", "center": [10, 20]}
            for t, r in specs]


assert (_perm.match_button(
    _pb(("拒绝并不再询问", "permission_deny_and_dont_ask_again_button"),
        ("拒绝", "permission_deny_button")), "deny") or {}).get("text") == "拒绝"
assert _perm.match_button(
    _pb(("拒绝并不再询问", "permission_deny_and_dont_ask_again_button")), "deny") is None, \
    "只有 dont-ask-again 时不该点任何东西"
assert _perm.match_button(_pb(("拒绝并不再询问", "")), "deny") is None
# grant 选最小够用授权；不碰「选择照片」「前往设置」
assert (_perm.match_button(
    _pb(("允许", "permission_allow_button"),
        ("全部允许", "permission_allow_all_button")), "grant") or {}).get("text") == "全部允许"
assert _perm.match_button(_pb(("选择照片", "permission_allow_selected_button")), "grant") is None
assert _perm.match_button(_pb(("前往设置", "")), "grant") is None
print("13 perm_match_ok  deny 绝不点 dont-ask-again；grant 选最小授权且不碰选择照片/前往设置")

# ── 14. 多权限申请：同一个框内点到框消失 ────────────────────────────
# ⚠️ 真实机制（人指正）：一次申请多个权限时，Android **不关框**，
# 就在同一个 GrantPermissionsActivity 里点一次换下一个权限。
# 早先按 (activity, 按钮文本) 去重 → 第 2 个权限被判"同一个还没消失" → break →
# **后面几个权限全都不点**（真实缺陷：清空数据后首启只点到一部分权限）。
assert _perm.DEFAULT_ACTION == "grant", f"默认动作被改动: {_perm.DEFAULT_ACTION}"


class _MultiPermD:
    """假设备：同一个权限框内依次问 3 个权限（点一次换下一个），最后关闭。"""
    N = 3

    def __init__(self):
        self.clicks = []
        self.i = 0

    def app_current(self):
        if self.i >= self.N:
            return {"package": "com.zui.calendar", "activity": ".AllInOneActivity"}
        return {"package": "com.android.permissioncontroller",
                "activity": ".permission.ui.GrantPermissionsActivity"}

    def dump_hierarchy(self):
        if self.i >= self.N:
            return "<hierarchy/>"
        return ('<hierarchy>'
                f'<node text="日历正在尝试读取权限{self.i}" bounds="[0,0][100,20]"/>'
                '<node text="允许" clickable="true" '
                f'resource-id="{_P}permission_allow_button" bounds="[0,0][10,10]"/>'
                '</hierarchy>')

    def click(self, x, y):
        self.clicks.append((x, y))
        self.i += 1                      # 点完推进到下一个权限


_md = _MultiPermD()
_mr = _perm.handle(_md, action=None, timeout_s=0.2)
assert len(_md.clicks) == _MultiPermD.N, \
    f"同一框内 {_MultiPermD.N} 个权限应连点 {_MultiPermD.N} 次，" \
    f"实际 {len(_md.clicks)} 次（旧 bug：只点 1 次就 break）"
assert _mr["detected"] and _mr["handled"], _mr
assert _mr["action_source"] == "default", _mr
assert _mr["dialog_count"] == _MultiPermD.N, _mr
print("14 perm_multigrant_ok  同一框内多权限连续点到框消失（不再点一次就停）")

# ── 15. summarize_xml 三态（nav / full / fg_package 过滤）────────────
_X = ('<hierarchy>'
      '<node class="android.widget.ListView" scrollable="true" clickable="false"'
      ' bounds="[0,0][100,200]" resource-id="app:id/list" package="com.app"/>'
      '<node class="android.widget.TextView" clickable="false" text="仅文本"'
      ' bounds="[0,0][50,50]" package="com.app"/>'
      '<node class="android.widget.Button" clickable="true" text="OK"'
      ' bounds="[0,0][60,60]" package="com.app"/>'
      '<node class="android.widget.TextView" clickable="false" text="状态栏时钟"'
      ' bounds="[0,0][40,40]" package="com.android.systemui"/>'
      '</hierarchy>')
_nav = summarize_xml(_X, mode="nav")
_full = summarize_xml(_X, mode="full")
_filt = summarize_xml(_X, mode="full", fg_package="com.app")
assert len(_nav) == 2 and any(n.get("s") for n in _nav), f"nav 应含可点+可滑动: {_nav}"
assert len(_full) == 4, f"full 应含全部语义节点: {_full}"
assert len(_filt) == 3 and not any(n.get("text") == "状态栏时钟" for n in _filt), \
    f"fg_package 应滤掉 systemui: {_filt}"
print("15 nodes_modes_ok  nav(可点+可滑动) / full(全部) / fg_package(按前台包过滤)")

# ── 16. perm.handle：点不动的权限框不要连点 ──────────────────────────
# 真实场景：按钮存在但点击无效（disabled/遮挡/坐标偏）。早先会连点 chain_max 次，
# 最坏 0.8 + 7×2.0 + 8×0.5 ≈ 18s 全耗在这一步。做法同人：点一下看有没有反应。
class _DeadButtonDev:
    """假设备：权限框一直在，点它毫无反应（屏幕内容不变）。"""
    def __init__(self):
        self.clicks = []

    def app_current(self):
        return {"package": "com.android.permissioncontroller",
                "activity": ".permission.ui.GrantPermissionsActivity"}

    def dump_hierarchy(self):
        return ('<hierarchy>'
                '<node text="日历正在尝试读取联系人" bounds="[0,0][100,20]"/>'
                '<node text="允许" clickable="true" '
                f'resource-id="{_P}permission_allow_button" bounds="[0,0][10,10]"/>'
                '</hierarchy>')

    def click(self, x, y):
        self.clicks.append((x, y))      # 点了没反应：不改任何状态


_db = _DeadButtonDev()
_dr = _perm.handle(_db, action=None, timeout_s=0.2)
assert len(_db.clicks) == 1, \
    f"点不动的框只该试 1 次，实际连点 {len(_db.clicks)} 次（会白等十几秒）"
assert _dr.get("stuck") is True, f"应标记 stuck 交回 AI：{_dr}"
assert _dr["handled"] is True, _dr
print("16 perm_dead_button_ok  点不动的权限框只试一次即停（不连点到超时）")

# ── 17. next_targets：精简可点清单（act/observe 的返回头条）──────────
_mixed = [
    {"c": [10, 10], "r": "decor_no_semantics"},                 # 三无 → 排最后
    {"c": [20, 20], "t": "课程表", "r": "item_title"},           # 有文字 → 最前
    {"c": [30, 30], "d": "导入课程表", "r": "act_import"},       # 有 desc → 中间
    {"c": [20, 20], "t": "课程表", "r": "item_title"},           # 重复 → 去重
    {"c": [40, 40], "t": "设置", "r": "act_settings", "s": True},
]
_nt = next_targets(_mixed, limit=10)
assert len(_nt) == 4, f"应去重成 4 条，实际 {len(_nt)}"
assert _nt[0].get("t") == "课程表", f"有文字的最前：{_nt[0]}"
assert next_targets(_mixed, limit=2) and len(next_targets(_mixed, limit=2)) == 2
assert next_targets([{"clickable": False, "center": [1, 1], "text": "不可点"}]) == [], \
    "full 模式里不可点的要排除"

# ── 18. next_targets 的「子节点文字冒泡」────────────────────────────
# 场景：可点容器自己没有文字（菜单项、列表行），文字在子节点里。
# 不冒泡的话 next 只有坐标没标签，AI 还得回去翻 nodes/另起 observe。
_nodes_bubble = [{"c": [2697, 371], "r": "item_title"},          # 容器无文字
                 {"c": [2697, 532], "r": "item_title"}]
_labeled = [(2697, 375, "课程表"), (2697, 535, "设置")]
_bt = next_targets(_nodes_bubble, labeled=_labeled)
assert len(_bt) == 2, _bt
assert _bt[0].get("t") == "课程表", f"应把子节点文字冒泡给容器：{_bt[0]}"
assert _bt[1].get("t") == "设置", _bt[1]
# 无 labeled 时行为不变（不报错、只是没标签）
assert next_targets(_nodes_bubble)[0].get("t") is None
# 已有的自带文字不被冒泡覆盖
_own = next_targets([{"c": [100, 100], "t": "自有文字"}], labeled=[(100, 100, "别的字")])
assert _own[0].get("t") == "自有文字", _own[0]
print("18 next_targets_bubble_ok  容器无文字时从子节点冒泡（菜单项/列表行可用）")
print("17 next_targets_ok  精简可点清单（去重/评分排序/limit/双模式兼容）")

# ── 19. 路径边的「节点归属」（自动整理草稿的核心）────────────────────
# 采集到的边只有 activity，要靠节点的「识别键」+ 当时的屏幕签名，
# 才能归成人类读得懂的节点名（如「课程表页-空态」而不是 `TimetableActivity`）。
from session import _match_node, _parse_path_nodes  # noqa: E402

_PT = """## 课程表页-空态

- **activity**: `.timetable.display.TimetableActivity`
- **认出它**: 有「还未添加课程表」
- **识别键**: `text=还未添加课程表` · `rid=btnCreateManually`

## 课程表页-周视图

- **activity**: `.timetable.display.TimetableActivity`
- **识别键**: `desc=导入课程表` · `desc=课程表设置`

## 自动采集的边
"""
_pn = _parse_path_nodes(_PT)
assert [n["name"] for n in _pn] == ["课程表页-空态", "课程表页-周视图"], _pn
assert _pn[0]["keys"] == [("text", "还未添加课程表"), ("rid", "btnCreateManually")], _pn[0]
# 同 activity 的两个形态，靠识别键分清（这正是节点名存在的意义）
assert _match_node(_pn, {"text=还未添加课程表", "rid=btnCreateManually"}) == "课程表页-空态"
assert _match_node(_pn, {"desc=导入课程表", "desc=课程表设置"}) == "课程表页-周视图"
# 一个都不命中时返回空（宁可归不出，也不乱归）
assert _match_node(_pn, {"rid=whatever"}) == "", "不该瞎猜节点"
print("19 path_node_match_ok  节点解析 + 同 activity 多形态靠识别键区分")

# ── 20. 屏幕身份签名（npm_identity）──────────────────────────────────
_IX = ('<hierarchy>'
       '<node resource-id="app:id/a" text="课程表" content-desc="导入课程表"'
       ' package="com.app" bounds="[0,0][10,10]"/>'
       '<node resource-id="app:id/a" text="课程表" package="com.app" bounds="[0,0][10,10]"/>'
       '<node resource-id="other:id/b" text="别的" package="com.other" bounds="[0,0][10,10]"/>'
       '</hierarchy>')
_sig = npm_identity(_IX, fg_package="com.app")
assert "rid=a" in _sig and "text=课程表" in _sig and "desc=导入课程表" in _sig, _sig
assert len([x for x in _sig if x == "text=课程表"]) == 1, f"应去重：{_sig}"
assert not any("别的" in x for x in _sig), f"非前台包应滤掉：{_sig}"
assert npm_identity("<bad", fg_package="com.app") == [], "坏 XML 不抛异常"
print("20 npm_identity_ok  屏幕身份签名（去重/按前台包过滤/坏XML不抛）")

# 清理
for f in STORAGE.glob(".crash_seen_*"):
    f.unlink(missing_ok=True)
db.get_db()._conn().execute("DELETE FROM events WHERE session_id=?", (sid2,))
db.get_db()._conn().execute("DELETE FROM sessions WHERE id=?", (sid2,))
db.get_db()._conn().commit()
from db import clear_current  # noqa: E402
clear_current()
shutil.rmtree(ev, ignore_errors=True)
print("ALL PASS")
