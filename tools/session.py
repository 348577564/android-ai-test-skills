"""session：测试会话生命周期。开会话后，observe/act/read 自动把每次调用
作为事件写入当前会话；AI 只需在断言时记 finding、结束时 finish。

用法：
  python session.py start --title "日历新建课程表" --input "用户口述的用例原文..."
  python session.py finding --status PASS --event-id <ID> --expect "出现新建成功toast" --actual "OCR读到'创建成功'"
  python session.py finish --status PASS --summary "3/4 PASS，1 BLOCKED(无图库权限)"
  python session.py list
  python session.py current
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from common import emit, knowledge_hint
import sqlite3
from db import ROOT, STATUSES, clear_current, current_session, get_db, set_current
from runtime import STORAGE_DIR

# Windows 控制台默认 GBK，中文/特殊符号会炸 → 全部 CLI 输出强制 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass


def maybe_file(v: str) -> str:
    """`@路径` → 读 UTF-8 文件内容作为值。

    中文长文本直接放命令行参数，在部分终端/管道里会被 GBK 双重解码
    （旧 skill 的 GBK 事故同族）；用例口述这种长文本一律建议走 @文件。
    """
    if v and v.startswith("@"):
        return Path(v[1:]).read_text(encoding="utf-8")
    return v


def _parse_path_nodes(text: str) -> list[dict]:
    """解析路径图，得到每个节点：{name, activity, keys:[(kind,value)], anchors:{...}}。

    识别键来自节点里的 `- **识别键**: \\`kind=value\\` · ...` 行（机器可读），
    用于把采集到的边**归到节点名**（而不是只有裸 activity）。
    """
    nodes: list[dict] = []
    cur: dict | None = None
    for ln in text.splitlines():
        if ln.startswith("## "):
            name = ln[3:].strip()
            if name in ("节点索引（先认自己在哪）", "自动采集的边"):
                cur = None
                continue
            cur = {"name": name, "activity": "", "keys": []}
            nodes.append(cur)
            continue
        if cur is None:
            continue
        s = ln.strip()
        if s.startswith("- **activity**:"):
            cur["activity"] = s.split(":", 1)[1].strip().strip("`")
        elif s.startswith("- **识别键**:"):
            for part in s.split(":", 1)[1].split("·"):
                p = part.strip().strip("`").strip()
                if "=" in p:
                    k, v = p.split("=", 1)
                    cur["keys"].append((k.strip(), v.strip()))
    return nodes


def _match_node(nodes: list[dict], snapshot_keys: set[str]) -> str:
    """按「识别键命中数」判定当前落在哪个节点。

    snapshot_keys：当时屏幕上出现过的 `rid=xxx` / `text=xxx` / `desc=xxx`。
    命中最多者胜；一个都不中就返回空（宁可归不出，也不乱归）。
    """
    best, best_n = "", 0
    for n in nodes:
        hit = sum(1 for k, v in n["keys"] if f"{k}={v}" in snapshot_keys)
        if hit > best_n:
            best, best_n = n["name"], hit
    return best


def apply_pending_paths(only: str | None = None,
                        package: str | None = None) -> dict:
    """把 `storage/pending_paths.md` 里的草稿边写入路径图。

    写入位置：路径图末尾的「## 自动采集的边」区（不碰人工整理好的各节点节）——
    人工节的锚点表带 desc/text 与 ⚠️ 提示，自动边只有 rid/text，混进去反而降质。
    人看过草稿后，可自行把高频边**手抄进对应节点**。
    """
    draft = STORAGE_DIR / "pending_paths.md"
    if not draft.is_file():
        return {"ok": False, "error": "没有待确认草稿（先跑 paths 采集）"}

    text = draft.read_text(encoding="utf-8")
    pkg = package
    if not pkg:
        m = re.search(r"app:\s*`([^`]+)`", text)
        if not m:
            return {"ok": False, "error": "草稿里没有 app 包名"}
        pkg = m.group(1)
    path = ROOT / "knowledge" / "paths" / f"{pkg}.md"
    if not path.is_file():
        return {"ok": False, "error": f"路径图不存在：knowledge/paths/{pkg}.md"}

    # 解析草稿里的边：`N. **源** --[锚点]--> **目标**`
    picks = set()
    if only:
        for p in only.split(","):
            p = p.strip()
            if p.isdigit():
                picks.add(int(p))
    rows: list[tuple[str, str, str]] = []
    for m in re.finditer(r"^\s*(\d+)\.\s+\*\*(.+?)\*\*\s+--\[(.+?)\]-->\s+\*\*(.+?)\*\*",
                         text, re.M):
        idx, a, v, b = int(m.group(1)), m.group(2), m.group(3), m.group(4)
        if picks and idx not in picks:
            continue
        rows.append((a, v, b))
    if not rows:
        return {"ok": False, "error": "草稿里没有可应用的边（或 --only 没选中任何一条）"}

    lines = path.read_text(encoding="utf-8").splitlines()
    marker = "## 自动采集的边"
    if marker not in lines:
        lines += ["", marker, "",
                  "<!-- 自动整理、经人确认写入；可手抄进上方节点 -->", ""]
    existing = set(l.strip() for l in lines)
    added_lines = []
    for a, v, b in rows:
        row = f"- `{a}` --[{v}]--> `{b}`"
        if row not in existing:
            added_lines.append(row)
    if added_lines:
        lines += added_lines
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 应用过的边从草稿里删掉，剩下的留着下次确认
    remaining = []
    for m in re.finditer(r"^\s*(\d+)\.\s+\*\*(.+?)\*\*\s+--\[(.+?)\]-->\s+\*\*(.+?)\*\*",
                         text, re.M):
        if int(m.group(1)) not in {i for i, _ in enumerate(rows, 1)}:
            remaining.append(m.group(0))
    # 简化：直接按"是否已写入路径图"判断剩余
    rest = [r for r in re.findall(
        r"^\s*\d+\.\s+\*\*(.+?)\*\*\s+--\[(.+?)\]-->\s+\*\*(.+?)\*\*", text, re.M)
        if f"- `{r[0]}` --[{r[1]}]--> `{r[2]}`" not in set(l.strip() for l in
                                                           path.read_text(encoding="utf-8").splitlines())]
    head = [f"# 路径图更新建议（剩余 {len(rest)} 条待确认）", "", f"app: `{pkg}`", "",
            "> 已应用的不再列出。确认后执行 `python tools/session.py paths-apply`", ""]
    body = []
    for i, (a, v, b) in enumerate(rest, 1):
        body += [f"{i}. **{a}** --[{v}]--> **{b}**", ""]
    draft.write_text("\n".join(head + body + ["---", ""]), encoding="utf-8")

    return {"ok": True, "package": pkg,
            "applied": len(added_lines), "skipped_duplicate": len(rows) - len(added_lines),
            "remaining": len(rest),
            "path": f"knowledge/paths/{pkg}.md"}


def collect_paths(db, session_id: int) -> str | None:
    """采集本次会话的「路径边」，**生成待确认草稿**（不直接改路径图）。

    记什么（对齐路径图模型）：
        节点（从哪个落脚点出发）→ 锚点（点了什么控件）→ 节点（到了哪）

    为什么要"草稿 + 确认"：
        自动采集只能拿到 activity + 点击时的屏幕快照，归节点靠识别键 ——
        大部分能归对，但边界情况（同 activity 多形态、弹层）需要人扫一眼。
        所以**先出草稿，人确认后再写入**，而不是自动改卡。

    采集规则（避免噪声）：
      - 只取带 `via`（点了什么）**且**前后 activity 发生变化的动作 —— 那才是"路径边"；
      - 同 activity 内的点击（弹菜单/弹窗）不记为页面级跳转；
      - 无 via 的动作跳过（当时没记定位依据，采不出可信的锚点）。
    """
    s = db.get_session(session_id)
    if not s:
        return None
    pkg = s.get("package")
    if not pkg:
        return None

    path = ROOT / "knowledge" / "paths" / f"{pkg}.md"
    nodes = _parse_path_nodes(path.read_text(encoding="utf-8")) if path.is_file() else []

    def _snapshot_keys(ev) -> set[str]:
        """取某次事件落库的**屏幕身份签名**（act 时随事件写入的 `sig`）。"""
        try:
            j = json.loads(ev.get("data_json") or "{}")
        except Exception:  # noqa: BLE001
            return set()
        return set(j.get("sig") or [])

    evs = s["events"]

    def _act_of(ev) -> str:
        try:
            return (json.loads(ev.get("data_json") or "{}").get("activity") or "")
        except Exception:  # noqa: BLE001
            return ""

    edges: list[tuple[str, str, str]] = []   # (源节点, 锚点, 目标节点)
    for i, e in enumerate(evs):
        if e["kind"] != "act":
            continue
        data = json.loads(e["data_json"] or "{}")
        act = data.get("action") or {}
        via = (act.get("via") or "").strip()
        if not via:
            continue                          # 没记定位依据 → 采不出可信锚点
        # 边 = 「点之前的页面」--[锚点]--> 「点之后的页面」。
        # 这条 act 自带的 activity 是**点击之后**的，来源要往前找最近一条有 activity 的事件。
        src_act, src_snap = "", set()
        for prev in reversed(evs[:i]):
            a = _act_of(prev)
            if a:
                src_act = a
                src_snap = _snapshot_keys(prev)
                break
        dst_act = _act_of(e)
        if not src_act or not dst_act or src_act == dst_act:
            continue                          # 同页（弹菜单/弹窗）→ 不是页面级路径边
        src_node = _match_node(nodes, src_snap) or src_act
        dst_node = _match_node(nodes, _snapshot_keys(e)) or dst_act
        edges.append((src_node, via, dst_node))
    if not edges:
        return None

    # 去重（同一条边可能多次走到）
    seen, uniq = set(), []
    for a, v, b in edges:
        if a == b:
            continue          # 自环：归到同一节点（如「图库导入流程」内部的子步骤），不是路径边
        if (a, v, b) not in seen:
            seen.add((a, v, b))
            uniq.append((a, v, b))

    # 已有内容里出现过的**完整边**（源节点 + 锚点 + 目标节点）才跳过。
    # ⚠️ 不能只拿 via 去搜全文 —— 锚点名（如 rid=iv_more）在卡里到处出现，
    # 会把所有边都误判成"已存在"（真实缺陷）。
    existing_txt = path.read_text(encoding="utf-8") if path.is_file() else ""
    fresh = [(a, v, b) for (a, v, b) in uniq if v not in existing_txt
             or f"{a}** --[{v}]--> **{b}" not in existing_txt]

    # 写草稿（不直接改路径图 —— 等人确认）
    draft = STORAGE_DIR / "pending_paths.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    block = [
        f"# 路径图更新建议（会话 #{session_id}）",
        "",
        f"app: `{pkg}`　路径图: `knowledge/paths/{pkg}.md`",
        "",
        "> 自动整理出的**待确认草稿**，尚未写入路径图。",
        "> 确认后执行：`python tools/session.py paths-apply --id "
        f"{session_id}`（或加 `--only 1,3` 只挑几条）",
        "",
    ]
    if not fresh:
        block += ["（本次没有新的边需要补充）", ""]
    for i, (a, v, b) in enumerate(fresh, 1):
        block += [f"{i}. **{a}** --[{v}]--> **{b}**", ""]
    block += ["---", "", "<!-- 本文件每次采集时覆盖重写 -->", ""]
    draft.write_text("\n".join(block), encoding="utf-8")

    if not fresh:
        return f"无需追加（会话 #{session_id} 的边都已在路径图中）"
    return (f"草稿已生成 storage/pending_paths.md（{len(fresh)} 条待确认边）；"
            f"确认后执行 `session.py paths-apply --id {session_id}`")




def run_case_cmd(db, args) -> None:
    """用例库子命令：add/list/show/update/save（从会话提升）。"""
    if args.ccmd == "add":
        cid = db.add_case(maybe_file(args.title), maybe_file(args.input),
                          args.package)
        print(json.dumps({"ok": True, "case_id": cid,
                          "hint": "复跑：session.py start --case " + str(cid)},
                         ensure_ascii=False))
    elif args.ccmd == "list":
        cases = db.list_cases()
        print(json.dumps(cases, ensure_ascii=False, indent=2))
    elif args.ccmd == "show":
        c = db.get_case(args.id)
        if not c:
            raise SystemExit(f"用例 #{args.id} 不存在")
        print(json.dumps(c, ensure_ascii=False, indent=2))
    elif args.ccmd == "update":
        ok = db.update_case(args.id,
                            maybe_file(args.title) if args.title else None,
                            maybe_file(args.input) if args.input else None,
                            args.package)
        print(json.dumps({"ok": ok}, ensure_ascii=False))
    elif args.ccmd == "save":
        s = db.get_session(args.session)
        if not s:
            raise SystemExit(f"会话 #{args.session} 不存在")
        if not (s.get("user_input") or "").strip():
            raise SystemExit("该会话没有口述用例文本，无法提升")
        title = maybe_file(args.title) if args.title else (s.get("title") or f"会话{args.session}用例")
        if s.get("case_id"):
            db.update_case(s["case_id"], title=title, user_input=s["user_input"])
            cid = s["case_id"]
            action = "updated"
        else:
            cid = db.add_case(title, s["user_input"], s.get("package"))
            action = "created"
        # ⚠️ 入库必须**顺手关联会话并计入统计**，否则用例库 run_count/last_status 永远是空的
        # （真实事故：170 BLOCKED 入库后 run=0 last=None，统计与实际脱节）。
        # 结果状态**不限于 PASS** —— FAIL / BLOCKED 同样是有效测试结果，必须计入。
        try:
            conn = db._conn()
            conn.execute("UPDATE sessions SET case_id=? WHERE id=?", (cid, args.session))
            conn.execute(
                "UPDATE cases SET last_session_id=?, last_status=?, last_run_at=?,"
                " run_count=(SELECT COUNT(*) FROM sessions WHERE case_id=?), updated_at=?"
                " WHERE id=?",
                (args.session, s.get("status"), db._now(), cid, db._now(), cid))
            conn.commit()
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"ok": False, "error": f"关联失败: {e}"}, ensure_ascii=False))
            raise SystemExit(1)
        print(json.dumps({"ok": True, "case_id": cid, "action": action,
                          "run_count": (db.get_case(cid) or {}).get("run_count"),
                          "last_status": s.get("status"),
                          "hint": "复跑：session.py start --case " + str(cid)},
                         ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="测试会话生命周期")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start")
    s.add_argument("--title", default=None)
    s.add_argument("--input", default=None, help="用户口述的用例原文（追溯锚点）")
    s.add_argument("--case", type=int, default=None, dest="case_id",
                   help="引用用例库：自动带出标题与用例文本，无需重复输入")
    s.add_argument("--device", default=None)
    s.add_argument("--diag", action="store_true",
                   help="标记为验证/调试会话：不进「测试记录」库、不计入仪表盘成功率。"
                        "用于工具自检、探针验证等非正式测试的跑动")
    s.add_argument("--package", default=None,
                   help="被测包名（强烈建议显式声明）：崩溃分级的 target 依据。"
                        "不声明则靠首个 observe 回填前台包——但被测 App 崩溃后"
                        "前台会回落到桌面，会把被测包崩溃误判成 other")
    s.add_argument("--related", default="",
                   help="关联包（逗号分隔），其崩溃同样按 BLOCKED 处理；"
                        "权限框宿主默认已含")

    s = sub.add_parser("finding")
    s.add_argument("--status", required=True, choices=STATUSES)
    s.add_argument("--expect", default="")
    s.add_argument("--actual", default="")
    s.add_argument("--note", default="")
    s.add_argument("--event-id", action="append", type=int, default=[],
                   help="关联本会话证据事件；可重复传入多个事件 ID")
    s.add_argument("--id", type=int, default=None,
                   help="记录到指定会话（暂停/等待中的会话用 --id）")

    s = sub.add_parser("finish")
    s.add_argument("--status", required=True, choices=STATUSES)
    s.add_argument("--summary", default="")
    s.add_argument("--package", default=None)
    s.add_argument("--no-paths", action="store_true", dest="no_paths",
                   help="PASS 时默认采集路径边；加此参数跳过")
    s.add_argument("--id", type=int, default=None,
                   help="收尾指定会话（暂停/等待中的会话已解绑，须用 --id）")

    s = sub.add_parser("pause", help="遇阻暂停或向用户提问（中间态，记录保留可续跑）")
    s.add_argument("--reason", required=True, help="为什么停：卡在哪、试过什么")
    s.add_argument("--ask", action="store_true",
                   help="同时向用户提问（状态=waiting 而非 paused）")

    s = sub.add_parser("resume", help="恢复暂停/等待中的会话")
    s.add_argument("--id", type=int, required=True)
    s.add_argument("--note", default="", help="用户的答复或恢复原因，随事件入库")

    c = sub.add_parser("case", help="正式用例库：口述用例的沉淀与复用")
    csub = c.add_subparsers(dest="ccmd", required=True)
    ca = csub.add_parser("add")
    ca.add_argument("--title", required=True)
    ca.add_argument("--input", required=True)
    ca.add_argument("--package", default=None)
    csub.add_parser("list")
    cs = csub.add_parser("show")
    cs.add_argument("--id", type=int, required=True)
    cu = csub.add_parser("update")
    cu.add_argument("--id", type=int, required=True)
    cu.add_argument("--title", default=None)
    cu.add_argument("--input", default=None)
    cu.add_argument("--package", default=None)
    cv = csub.add_parser("save", help="把会话的口述用例提升为正式用例（首次跑新用例后用）")
    cv.add_argument("--session", type=int, required=True)
    cv.add_argument("--title", default=None, help="缺省沿用会话标题")

    s = sub.add_parser("perm-intent",
                       help="声明/查看/清除权限响应意图（act/observe 遇到权限弹窗时按此自动响应）")
    s.add_argument("--action", default=None, choices=["grant", "deny"],
                   help="遇到权限弹窗点【同意】(grant) 还是【拒绝】(deny)")
    s.add_argument("--perm", default="",
                   help="权限类型标记（如 media_images/camera/location），仅用于记录")
    s.add_argument("--clear", action="store_true", help="清除意图（分支测完就清）")
    s.add_argument("--show", action="store_true", help="只查看当前意图")
    s.add_argument("--id", type=int, default=None, help="指定会话（默认当前）")

    s = sub.add_parser("paths", help="采集路径边 → 生成待确认草稿 storage/pending_paths.md")
    s.add_argument("--id", type=int, default=None)

    s = sub.add_parser("paths-apply",
                       help="把待确认草稿写入路径图（人确认后执行）")
    s.add_argument("--only", default=None,
                   help="只应用草稿里的第几条（如 1,3）；缺省=全部")
    s.add_argument("--package", default=None, help="目标包名（缺省取草稿里的）")

    sub.add_parser("list")
    sub.add_parser("current")
    sub.add_parser("abandon", help="放弃当前会话（不写结论，仅解绑）")

    args = p.parse_args()
    db = get_db()

    if args.cmd == "start":
        case = db.get_case(args.case_id) if args.case_id else None
        if args.case_id and not case:
            raise SystemExit(f"用例 #{args.case_id} 不存在，session.py case list 查看")
        title = maybe_file(args.title) if args.title else (case["title"] if case else None)
        user_input = maybe_file(args.input) if args.input else (case["input"] if case else None)
        if not title or not user_input:
            raise SystemExit("需要 --case <id>（复用库中用例）或 --title/--input（新用例，"
                             "中文长文本用 @文件）")
        # ⚠️ 用例库里已有同一条用例时**当场提示用 --case 复用**：
        # 早先只在 finish 时提示"尚未进入用例库"，那时会话已结束、提示等于没用
        # （真实事故：168/169/170 三条都建成了游离会话，用例库统计全不对）。
        # 匹配用**用例编号**（标题里的「_168」这种），不用全等 ——
        # 标题后缀常有人手写差异（如「图库导入入口」vs「图库导入课程表」），全等匹配不到。
        existing_case = None
        if not args.case_id and title:
            import re as _re
            m = _re.search(r"[_\-](\d{2,4})(?!\d)", title)
            num = m.group(1) if m else None
            for c in db.list_cases(include_mock=False):
                ct = (c.get("title") or "").strip()
                if ct == title.strip():
                    existing_case = c
                    break
                if num and _re.search(rf"[_\-]{num}(?!\d)", ct):
                    existing_case = c
                    break
        sid = db.start_session(title, user_input, args.device, case_id=args.case_id,
                               kind="diag" if args.diag else "test")
        set_current(sid)
        if args.package:
            # 显式声明优先：首个 observe 的 backfill 不会覆盖已有值
            db.set_package(sid, args.package)
        related = [x.strip() for x in args.related.split(",") if x.strip()]
        if related:
            db.set_related(sid, related)
        # 扩 logcat 缓冲区防冲（-G 5M）+ 清 crash 缓冲区归因；设备不在也不影响建会话
        from common import try_connect
        d = try_connect(args.device)
        if d is not None:
            notes = []
            for cmd, okmsg in (("logcat -G 5M", "logcat 缓冲区已扩到 5M"),
                               ("logcat -b crash -c", "crash 缓冲区已清空")):
                try:
                    d.shell(cmd)
                    notes.append(okmsg)
                except Exception:
                    notes.append(f"{cmd} 失败（可手动 logcat.py setup / clear）")
            device_note = "；".join(notes)
        else:
            device_note = "设备未连接（连上后手动跑 logcat.py setup + logcat.py clear）"
        (STORAGE_DIR / f".crash_seen_{sid}").unlink(missing_ok=True)
        out = {"ok": True, "session_id": sid, "related": related,
               "note": f"后续 observe/act/read 自动记入本会话；{device_note}"}
        # 知识卡浮现：有卡就提示路径 + 小节索引（只提示一次，见 common.knowledge_hint）
        hint = knowledge_hint(args.package or (case["package"] if case else None))
        if hint:
            out["knowledge_hint"] = hint
            # 只要发出了 hint 就落标记（无卡包也有 system_card 提示，同样别重复发）
            db.mark_knowledge_shown(sid)
        # 用例库关联提醒：**当场提示**才有用（finish 时才提示＝会话已结束，等于没用）
        if existing_case:
            out["case_exists"] = {
                "case_id": existing_case["id"],
                "title": existing_case["title"],
                "run_count": existing_case.get("run_count"),
                "last_status": existing_case.get("last_status"),
            }
            out["case_hint"] = (
                f"⚠️ 用例库已有同名用例 #{existing_case['id']}（跑过 "
                f"{existing_case.get('run_count') or 0} 次，上次 {existing_case.get('last_status')}）。"
                f"**本次未关联它** —— 跑的是同一条就该用 `start --case {existing_case['id']}` 复用；"
                f"已经开跑了可结束前用 `case save --session {sid}` 关联。")
        elif not args.case_id:
            out["case_hint"] = (f"新用例（未关联用例库）。跑完后 `case save --session {sid}` 入库 —— "
                                f"**PASS / FAIL / BLOCKED 都该入库**（失败与阻断也是测试结果）。")
        print(json.dumps(out, ensure_ascii=False))
    elif args.cmd == "finding":
        sid = args.id if getattr(args, "id", None) else current_session()
        if not sid:
            raise SystemExit("没有进行中的会话，先 session.py start（或用 --id 指定）")
        fid = db.add_finding(sid, args.status, maybe_file(args.expect),
                             maybe_file(args.actual), maybe_file(args.note),
                             event_ids=args.event_id)
        emit(True, {"finding_id": fid, "evidence_event_ids": args.event_id})
    elif args.cmd == "pause":
        sid = current_session()
        if not sid:
            raise SystemExit("没有进行中的会话")
        status = "waiting" if args.ask else "paused"
        db.set_session_status(sid, status)
        db.log_event(sid, "ask" if args.ask else "pause",
                     f"session {'ask' if args.ask else 'pause'}",
                     maybe_file(args.reason))
        clear_current(sid)
        print(json.dumps({"ok": True, "session_id": sid, "status": status,
                          "note": ("把问题抛给用户，答复后用 resume --id 恢复"
                                   if args.ask else "已暂停；resume 续跑或 finish 收尾")},
                         ensure_ascii=False))
    elif args.cmd == "resume":
        # resume 也用于"崩溃终止后人工确认继续"：必须清除终态，否则工具仍拒绝执行。
        # ⚠️ 顺序要紧：先校验理由，**通过后才清终态**。
        # 曾经先 clear 再校验，导致"无理由被拒"的那次调用已把终态抹掉，
        # 第二次不带理由的 resume 反而放行（真实 bug）。
        was_terminated = db.get_termination(args.id)
        note = maybe_file(args.note) if args.note else ""
        if was_terminated and not note:
            raise SystemExit(
                f"会话 #{args.id} 因崩溃被终止，继续执行必须给出理由："
                f"resume --id {args.id} --note \"为什么可以继续（如已确认偶发、已恢复前置）\"")
        db.clear_termination(args.id)
        db.set_session_status(args.id, "running")
        set_current(args.id)
        if note:
            db.log_event(args.id, "note", "session resume", note)
        out = {"ok": True, "session_id": args.id, "status": "running"}
        if was_terminated:
            out["note"] = "已清除崩溃终态，会话恢复运行"
            out["was_terminated"] = True
            out["terminate_reason"] = was_terminated
        print(json.dumps(out, ensure_ascii=False))
    elif args.cmd == "finish":
        sid = args.id if getattr(args, "id", None) else current_session()
        if not sid:
            raise SystemExit("没有进行中的会话（暂停/等待中的会话用 --id 指定）")
        summary = maybe_file(args.summary)
        ok = db.finish_session(sid, args.status, summary, args.package)
        cleanup_warning = None
        try:
            clear_current(sid)
        except (ValueError, OSError) as error:
            cleanup_warning = f"Conclusion committed, but current pointer cleanup failed: {error}"
        crash_n = db.count_crash_events(sid)
        stats = db.finding_stats(sid)
        self_check = []
        if cleanup_warning:
            self_check.append(cleanup_warning)
        # bug1 修复：会话没有关联用例时主动提示入库。
        # 用例不会自动进库（什么时候沉淀是人的决定），但**必须提醒**，
        # 否则跑完的用例悄悄丢掉、仪表盘/用例库永远是旧的。
        case_hint = None
        row = db.get_session(sid) or {}
        if not row.get("case_id") and (row.get("user_input") or "").strip():
            case_hint = (f"本会话尚未进入用例库。沉淀为正式用例："
                         f"python session.py case save --session {sid}")
        # 采集路径边（不再导出步骤序列）。
        # 为什么换掉：步骤序列是最易腐的知识（换个状态/版本就废），
        # 而「从哪个落脚点、点什么锚点、到了哪」是耐用的结构。
        # ⚠️ **不看会话结论**：BLOCKED / FAIL 的会话里，崩溃前走过的路径同样有效
        # （实测 170 走到最后一步才崩，前面的「导入课程表→图库导入→裁剪→解析」全是真路径）。
        # 只要有 act 事件就能采出边 —— 早先只采 PASS，白丢了这些。
        path_note = None
        if not getattr(args, "no_paths", False):
            try:
                path_note = collect_paths(db, sid)
            except Exception as e:  # noqa: BLE001
                self_check.append(f"⚠️ 路径采集失败：{e}")

        out = {"ok": ok, "session_id": sid, "crash_events": crash_n,
               "findings": stats, "self_check": self_check,
               "case_hint": case_hint,
               "hint": (f"本会话有 {crash_n} 个崩溃事件——若涉及被测/关联包，"
                        "结论应为 BLOCKED 且报告需附 crash.log 证据路径；"
                        "无关包崩溃仅需在报告中说明")
               if crash_n else "无崩溃事件"}
        if path_note:
            out["paths_collected"] = path_note
            out["note"] = ("已采集路径边到 knowledge/paths/<包名>.md"
                           "（落脚点 + 锚点 + 去向；下次操作可直接查，不用探索）")
        emit(True, out)
    elif args.cmd == "case":
        run_case_cmd(db, args)
    elif args.cmd == "abandon":
        clear_current()
        print(json.dumps({"ok": True}, ensure_ascii=False))
    elif args.cmd == "perm-intent":
        sid = args.id if getattr(args, "id", None) else current_session()
        if not sid:
            raise SystemExit("没有进行中的会话（或用 --id 指定）")
        if args.clear:
            db.clear_perm_intent(sid)
            print(json.dumps({"ok": True, "session_id": sid, "intent": None,
                              "note": "权限意图已清除"}, ensure_ascii=False))
        elif args.show or not args.action:
            cur = db.get_perm_intent(sid)
            print(json.dumps({"ok": True, "session_id": sid, "intent": cur,
                              "hint": "声明：perm-intent --action grant|deny [--perm 类型]"
                              } if cur else {
                                  "ok": True, "session_id": sid, "intent": None,
                                  "hint": "当前无意图：遇到权限弹窗只会记录、不会点击"},
                             ensure_ascii=False))
        else:
            ok = db.set_perm_intent(sid, args.action, args.perm)
            db.log_event(sid, "perm", "perm-intent",
                         f"权限意图: {args.action}"
                         + (f"（{args.perm}）" if args.perm else ""))
            print(json.dumps({"ok": ok, "session_id": sid,
                              "intent": {"action": args.action, "permission": args.perm},
                              "hint": (f"后续 act/observe 遇到权限弹窗将自动点"
                                       f"【{'同意' if args.action == 'grant' else '拒绝'}】"
                                       f"；分支测完请 perm-intent --clear")},
                             ensure_ascii=False))
    elif args.cmd == "paths":
        # 手动采集某会话的路径边 → 草稿（finish 时已自动做）
        note = collect_paths(db, args.id if getattr(args, "id", None) else current_session())
        print(json.dumps({"ok": bool(note), "paths": note}, ensure_ascii=False))
    elif args.cmd == "paths-apply":
        print(json.dumps(apply_pending_paths(
            only=getattr(args, "only", None),
            package=getattr(args, "package", None)), ensure_ascii=False))
    elif args.cmd == "list":
        print(json.dumps(db.list_sessions(20), ensure_ascii=False, indent=2))
    elif args.cmd == "current":
        print(json.dumps({"session_id": current_session()}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (sqlite3.Error, ValueError, OSError) as error:
        emit(False, {"error": str(error), "stage": "session",
                     "rejected": isinstance(error, ValueError),
                     "hint": "Correct the input or storage issue; rejected finish does not commit."})
        raise SystemExit(1)
