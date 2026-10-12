"""共享底座：设备连接 + 证据落盘 + 会话事件自动记录。
所有工具无流程逻辑，只提供"看/动/读"原语。"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import os
import runtime
from datetime import datetime
from pathlib import Path
from runtime import CALL_ID, EVENT_IDS, STORAGE_DIR, recording_issues, stage, timed, timing_snapshot

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EVIDENCE_DIR = STORAGE_DIR / "evidence"


def ensure_utf8_stdio() -> None:
    """把 stdout/stderr 切到 UTF-8。

    ⚠️ 必须在 **argparse 解析之前**生效：用法错误（如给 tap 传 --text）
    会由 argparse 直接写 stderr，内容含中文。若沿用控制台默认编码（Windows
    上是 GBK），调用方按 UTF-8 解码时会抛 UnicodeDecodeError，把一个
    "参数写错" 放大成 "输出无法解析"。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


ensure_utf8_stdio()


def connect(serial: str | None):
    """延迟导入 uiautomator2：无依赖环境也能 --help / 查看用法。"""
    try:
        with stage("device_import"):
            import uiautomator2 as _u2
    except ImportError:
        fail("缺依赖：先建环境 pip install -r requirements.txt")
    with stage("device_connect"):
        return _u2.connect(serial) if serial else _u2.connect()


def try_connect(serial: str | None):
    """静默版：连不上返回 None（用于可有可无的设备操作，如 start 时清 crash 缓冲区）。"""
    try:
        import uiautomator2 as _u2
        return _u2.connect(serial) if serial else _u2.connect()
    except Exception:  # noqa: BLE001
        return None


def rel(path: Path) -> str:
    """证据路径转 skill 根相对（posix），Web UI 按此路径伺服。"""
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def knowledge_hint(package: str | None, shown: bool = False) -> dict | None:
    """知识卡浮现：告诉 AI"这个包有卡、卡里有哪些小节"。

    ⚠️ 为什么需要（真实教训）：`knowledge/` 下有 5 张真机实测的卡
    （`com.zui.calendar.md` 500 行经验），但**全链路没有任何地方提示卡存在**——
    跑 168/169 那几轮，AI 从头到尾没读过那张卡，把"首次启动三连弹框""图库导入
    标准链路"这些已经沉淀好的坑重新探了一遍。**已有资产完全没被用上。**

    设计取舍：
    - **只给标题、不给行号**：卡片会持续编辑，行号是易腐数据；
      AI 拿到标题再 `grep -n` 定位即可（两步都很便宜）。
    - **只提示一次**（shown=True 时直接返回 None）：索引本身也占 token，
      每步都带会把前面压 nodes 省下的又还回去。

    返回 None 表示：无包名 / 无卡 / 已提示过。
    """
    if not package or shown:
        return None
    card = ROOT / "knowledge" / f"{package}.md"
    path_map = ROOT / "knowledge" / "paths" / f"{package}.md"
    # 路径图节点（落脚点），有就一并给出 —— 它才是"先定位，再找出边"的入口
    path_nodes: list[str] = []
    # 这些小节不是落脚点，是路径图的辅助段，别混进节点索引
    _NOT_NODES = ("节点索引", "自动采集的边", "说明", "索引", "同 rid 多态")
    if path_map.is_file():
        try:
            for line in path_map.read_text(encoding="utf-8").splitlines():
                if line.startswith("## "):
                    name = line[3:].strip()
                    if name and not any(k in name for k in _NOT_NODES):
                        path_nodes.append(name)
        except OSError:
            pass
    if not card.is_file() and not path_nodes:
        return {
            "card": None,
            "system_card": "knowledge/_system.md",
            "tip": ("本包暂无专属知识卡。系统界面（权限弹窗/系统对话框/预装应用"
                    "卸载恢复）看 knowledge/_system.md"),
        }
    sections: list[str] = []
    if card.is_file():
        try:
            for line in card.read_text(encoding="utf-8").splitlines():
                if line.startswith("## "):
                    sections.append(line[3:].strip())
        except OSError:
            pass
    out: dict = {
        "card": f"knowledge/{package}.md" if card.is_file() else None,
        "sections": sections,
        "tip": ("**执行前先定位**：路径图是节点图（节点=落脚点，边=认什么锚点→去哪）。"
                "先认自己在哪个节点（看 activity + 屏幕上有什么），再检索该节点的出边。"
                "知识卡是坑与行为规律（按交互类型词检索命中标题，只读对应小节）。"),
        "system_card": "knowledge/_system.md",
    }
    if path_nodes:
        out["path_map"] = f"knowledge/paths/{package}.md"
        # 字段名直说用途：这是"节点索引"，供先定位、再 grep 对应节
        out["nodes"] = path_nodes
    return out


def auto_name() -> str:
    """证据名自动派生：<会话id>/step<N>——探索时不该逼 AI 先想命名。

    计数器自增用 O_EXCL 锁文件保护（读改写非原子，并行调用会拿到同一个
    step 号互相覆盖证据）；抢不到锁短暂重试，超时降级为直读（宁重号不断功能）。
    """
    import os
    try:
        from db import current_session
        sid = current_session() or "loose"
    except Exception:  # noqa: BLE001
        sid = "loose"
    seq_file = STORAGE_DIR / ".evidence_seq"
    lock = str(seq_file) + ".lock"
    seq_file.parent.mkdir(parents=True, exist_ok=True)
    n = 1
    try:
        n = int(seq_file.read_text().strip()) + 1
    except (OSError, ValueError):
        n = 1
    fd = None
    for _ in range(20):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            time.sleep(0.01)
        except OSError:
            break
    try:
        if fd is not None:
            os.close(fd)
            try:
                n = int(seq_file.read_text().strip()) + 1
            except (OSError, ValueError):
                pass
        # ⚠️ 降级路径也必须写入：抢不到锁时若跳过写入，下次调用会读到同一个 n，
        # 证据互相覆盖——而加锁本来就是为了防这个（真实缺陷）。
        # 宁可有极小概率重号，也不能必然重号。
        seq_file.write_text(str(n), encoding="utf-8")
    except OSError:
        pass
    finally:
        if fd is not None:
            try:
                os.unlink(lock)
            except OSError:
                pass
    return f"{sid}/step{n:04d}"


def evidence_paths(base_dir: str | None, name: str | None) -> tuple[Path, Path, Path]:
    if not name:
        name = auto_name()
    d = Path(base_dir) if base_dir else DEFAULT_EVIDENCE_DIR
    d = d / name
    d.mkdir(parents=True, exist_ok=True)
    return d / "shot.png", d / "dump.xml", d / "meta.json"


def log_auto(kind: str, tool: str, detail: str, data: dict | None = None,
             evidence: str | None = None, duration_ms: int | None = None,
             started_at: str | None = None) -> int | None:
    """Persist an event or explicitly stop; an executed action must not be replayed."""
    sid = None
    try:
        from db import current_session, get_db
        sid = current_session()
        if sid:
            with stage("event_write"):
                eid = get_db().log_event(
                    sid, kind, tool, detail,
                    {**(data or {}), "timing": timing_snapshot()},
                    evidence, duration_ms, started_at)
            if eid is None:
                raise RuntimeError("Event writer returned no event ID")
            EVENT_IDS.append(eid)
            return eid
        return None
    except (OSError, ValueError, TypeError, RuntimeError, sqlite3.Error) as error:
        payload = {
            "recording_failed": True, "call_id": CALL_ID, "session_id": sid,
            "stage": "event_write", "error": str(error),
            "action_may_have_executed": runtime.EXECUTION_STATE != "not_executed",
            "evidence": evidence,
            "hint": "Do not replay actions. Preserve evidence and resolve incomplete recording.",
        }
        if sid is not None:
            try:
                directory = STORAGE_DIR / "recording_errors" / str(sid)
                directory.mkdir(parents=True, exist_ok=True)
                (directory / f"{CALL_ID}.json").write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            except OSError as marker_error:
                payload["marker_error"] = str(marker_error)
        print(f"Recording failed: {error}", file=sys.stderr)
        emit(False, payload)
        raise SystemExit(1)


def backfill_package(package: str | None) -> None:
    """首个 observe 回填当前会话的被测包名。"""
    if not package:
        return
    try:
        from db import current_session, get_db
        sid = current_session()
        if sid:
            get_db().set_package(sid, package)
    except Exception:
        pass


def _rotation(d: "u2.Device", shot=None) -> int:
    """屏幕旋转角（0/1/2/3），跨 uiautomator2 版本稳健取法。

    ⚠️ uiautomator2 3.x 移除了 Device.rotation（AttributeError 会让每次
    observe/act 直接失败）；Device.info 在 Android 17 上又会抛
    ApplicationSharedMemory not initialized。故此处三级降级，
    最后用截图长宽比推断（横屏=1），拿不到就返回 -1 而不是抛异常——
    取证不该因为一个展示字段而整条链路失败。
    """
    for attr in ("rotation", "getRotation"):
        try:
            v = getattr(d, attr)
            v = v() if callable(v) else v
            if isinstance(v, int) and 0 <= v <= 3:
                return v
        except Exception:  # noqa: BLE001
            pass
    try:
        w, h = d.window_size()
        if w and h:
            return 1 if w > h else 0
    except Exception:  # noqa: BLE001
        pass
    if shot is not None:
        try:
            return 1 if shot.size[0] > shot.size[1] else 0
        except Exception:  # noqa: BLE001
            pass
    return -1


def capture(d: "u2.Device", png: Path, xml: Path, meta: Path,
            extra: dict | None = None,
            event: dict | None = None) -> dict:
    """截图 + dump + 前台状态，落盘并返回摘要 dict。

    event={"kind","tool","detail","duration_ms"} 时自动写入当前会话事件流。
    """
    with stage("screenshot"):
        shot = d.screenshot()
    with stage("png_save"):
        shot.save(png)
    with stage("ui_dump"):
        xml_text = d.dump_hierarchy()
    with stage("rotation"):
        rotation = _rotation(d, shot)
    with stage("xml_save"):
        xml.write_text(xml_text, encoding="utf-8")
    with stage("foreground"):
        cur = d.app_current() or {}
    info = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "package": cur.get("package"),
        "activity": cur.get("activity"),
        "rotation": rotation,
        "size": list(shot.size),
    }
    if extra:
        info.update(extra)
    info["call_id"] = CALL_ID
    info["timing"] = timing_snapshot()
    with stage("meta_save"):
        meta.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    if event is not None:
        log_auto(event["kind"], event["tool"], event["detail"], info,
                 rel(png), event.get("duration_ms"), event.get("started_at"))
    return info


# ---------- dump 摘要 ----------

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


@timed("screen_signature")
def npm_identity(xml_text: str, fg_package: str | None = None,
                 limit: int = 80) -> list[str]:
    """给一屏算一个**身份签名**：页面上出现过的 `kind=value` 短串集合。

    用途：事后自动整理路径边时，判断"这一步落在路径图的哪个节点"
    （路径图节点带「识别键」，形如 `rid=btnCreateManually` / `text=全部课程表`）。

    为什么存签名而不是存 nodes：签名是几十个短串（可随事件落库），
    nodes 是上百个对象（落库太占地方）。
    """
    import xml.etree.ElementTree as ET
    out: list[str] = []
    seen: set[str] = set()
    try:
        root = ET.fromstring(xml_text)
    except Exception:  # noqa: BLE001
        return out
    for el in root.iter("node"):
        a = el.attrib
        if fg_package and a.get("package") and a.get("package") != fg_package:
            continue
        rid = (a.get("resource-id") or "").split("/")[-1]
        if rid and f"rid={rid}" not in seen:
            seen.add(f"rid={rid}")
            out.append(f"rid={rid}")
        t = (a.get("text") or "").strip()
        if t and f"text={t}" not in seen:
            seen.add(f"text={t}")
            out.append(f"text={t}")
        d = (a.get("content-desc") or "").strip()
        if d and f"desc={d}" not in seen:
            seen.add(f"desc={d}")
            out.append(f"desc={d}")
        if len(out) >= limit:
            break
    return out


@timed("node_labels")
def labeled_nodes(xml_text: str, fg_package: str | None = None) -> list[tuple]:
    """取页面上**有文字的节点**位置：[(center, text), ...]。

    用途：给 `next_targets` 做"子节点文字冒泡" —— 可点容器自己没有文字时，
    用这些文字的位置反查它属于哪个可点节点。
    """
    import xml.etree.ElementTree as ET
    out = []
    try:
        root = ET.fromstring(xml_text)
    except Exception:  # noqa: BLE001
        return out
    for el in root.iter("node"):
        a = el.attrib
        if fg_package and a.get("package") and a.get("package") != fg_package:
            continue
        t = (a.get("text") or "").strip()
        if not t:
            continue
        m = _BOUNDS_RE.search(a.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        out.append(((x1 + x2) // 2, (y1 + y2) // 2, t))
    return out


@timed("next_targets")
def next_targets(nodes: list[dict], limit: int = 12,
                 labeled: list[tuple] | None = None) -> list[dict]:
    """从 nodes 里挑出"下一步最可能点的目标"，给一份**精简可点清单**。

    ⚠️ 为什么需要（真实教训，被同一个人纠正多次）：
    AI 的执行循环常变成「act → observe 拿坐标 → act → observe …」——
    但它其实**不需要**额外 observe：act 返回的 nodes 就是动作后的画面。
    多一次 observe = 工具 2s + 决策等待 10~15s，纯粹浪费。

    根因是"下一个目标是什么/在哪"要在一大坨 nodes 里翻，太费劲 →
    干脆另起一次 observe。本函数把这件事变便宜：**直接给精简可点清单**。

    兼容两种 nodes 格式：
      - nav 模式（短键 i/t/r/c/s）：**本身就是可点/可滑动集合**，全部算候选
      - full 模式（完整键 + clickable）：只取 clickable 的

    `labeled`：`[(center, text), ...]` —— 页面上的**文字节点**位置。
    可点容器常常自己没有文字（文字在子节点里，如菜单项、列表行），
    此时用**空间包含**把子节点文字认领给该可点节点（冒泡），
    否则清单里会出现一堆"没有标签只有坐标"的项，AI 仍得回去翻 nodes。
    """
    def _bubble(c) -> str:
        """找出位于该可点节点范围内的文字（取最长的一条）。"""
        if not labeled:
            return ""
        cx, cy = c[0], c[1]
        best = ""
        for lx, ly, lt in labeled:
            # 文字中心落在可点节点附近（容器通常比文字大，用宽容的邻近判定）
            if abs(lx - cx) <= 400 and abs(ly - cy) <= 80 and len(lt) > len(best):
                best = lt
        return best

    cands = []
    seen = set()
    for n in nodes:
        # nav 模式没有 clickable 字段（短键格式本身就是筛选过的），full 模式才判
        if "clickable" in n and not n.get("clickable"):
            continue
        c = n.get("center") or n.get("c")
        if not c:
            continue
        text = n.get("text") or n.get("t") or ""
        desc = n.get("desc") or ""
        rid = n.get("rid") or n.get("r") or ""
        if not text and not desc:
            text = _bubble(c)          # 容器无文字 → 从子节点冒泡
        key = (tuple(c), text, rid)
        if key in seen:
            continue
        seen.add(key)
        # 有语义的优先；三无的（纯容器/无描述图标）排后
        score = (3 if text else 0) + (2 if desc else 0) + (1 if rid else 0)
        item = {"c": list(c)}
        if text:
            item["t"] = text
        if desc:
            item["d"] = desc
        if rid:
            item["r"] = rid.split("/")[-1]
        if n.get("scrollable") or n.get("s"):
            item["s"] = True
        cands.append((score, item))
    cands.sort(key=lambda x: -x[0])
    return [c[1] for c in cands[:limit]]


# 装饰性系统界面：状态栏 / 导航栏 / 桌面 taskbar。
# 这些节点与"被测 App 的界面"无关，纯噪音（实测在某 App 页占 full 节点的 72%）。
_DECOR_PKGS = ("com.android.systemui", "com.zui.launcher", "com.android.launcher",
               "com.android.launcher3")


@timed("node_summary")
def summarize_xml(xml_text: str, max_nodes: int = 300,
                  mode: str = "nav", fg_package: str | None = None) -> list[dict]:
    """把 UI 树压成给 LLM 读的节点列表。

    两种模式（实测同一屏 3840×2560 的压缩效果）：
      mode="nav"  —— **导航模式（默认）**：只给**可点 + 可滑动**的节点，字段名用短键。
                     可滑动容器必须留（不可点但能滑，正是"该往哪滑"的线索）；
                     实测 62 节点/6826 字符 → 9 节点/509 字符（省 93%）。
                     nav 字段：i=下标 t=文字 r=rid 末段 c=中心坐标
                             s=可滑动 off=不可用 checked/selected=选中态
      mode="full" —— **检查模式**：给全部有语义的节点（clickable/scrollable/
                     有 rid/text/desc），字段名完整。断言"页面上出现了什么文字"
                     时必须用这个，否则会漏判（nav 不含不可点的文本节点）。

    过滤（fg_package 非空时启用）：**只保留前台包自己的节点**，
    丢掉其它包的（状态栏/导航栏/桌面 taskbar 等装饰）。

    ⚠️ 为什么按"前台包"过滤而不是"被测包"：被测 App 崩溃后前台会回落到桌面、
    权限弹窗宿主是 permissioncontroller、流程跳到相机/图库——这些时刻前台包
    **本来就不是被测包**，若按被测包过滤会把整个屏幕滤空，崩溃和弹窗就看不见了。
    按前台包过滤则天然兼容：屏幕上是什么，就保留什么。

    为什么要分模式 + 过滤：nodes 每步都进 LLM 上下文，导航时给全量是纯浪费。
    """
    import xml.etree.ElementTree as ET
    full = (mode == "full")
    nodes: list[dict] = []
    for el in ET.fromstring(xml_text).iter("node"):
        a = el.attrib
        rid, text, desc = a.get("resource-id", ""), a.get("text", ""), a.get("content-desc", "")
        clickable = a.get("clickable") == "true"
        scrollable = a.get("scrollable") == "true"
        if fg_package:
            pkg = a.get("package", "")
            # 只留前台包的节点；节点没标 package 时按装饰包名兜底排除
            if pkg:
                if pkg != fg_package:
                    continue
            elif any(d in rid for d in _DECOR_PKGS):
                continue
        if not full and not (clickable or scrollable):
            # 导航模式：留可点的 + 可滑动的。
            # ⚠️ 必须带 scrollable——不可点但可滑动的容器（列表/滚轮）正是
            # "该往哪滑"的线索；只留 clickable 会让 AI 在长列表里找不到滑动目标。
            continue
        if full and not (clickable or scrollable or rid or text or desc):
            continue                      # 检查模式：跳过纯容器
        m = _BOUNDS_RE.search(a.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2

        if full:
            node = {"bounds": [x1, y1, x2, y2], "center": [cx, cy]}
            if rid:
                node["rid"] = rid
            if text:
                node["text"] = text
            if desc:
                node["desc"] = desc
            if clickable:
                node["clickable"] = True
            if scrollable:
                node["scrollable"] = True
            if a.get("enabled") == "false":
                node["enabled"] = False
            for st in ("checked", "selected"):
                if a.get(st) == "true":
                    node[st] = True
        else:
            # 短键名：i=过滤后列表下标 t=文字 r=rid 末段 c=中心坐标（i 与 dump.xml 序号无关）
            node = {"i": len(nodes), "c": [cx, cy]}
            if text:
                node["t"] = text
            if desc:
                node["t"] = (node.get("t") + " " + desc).strip() if node.get("t") else desc
            if rid:
                node["r"] = rid.split("/")[-1]
            if scrollable:
                node["s"] = True
            if a.get("enabled") == "false":
                node["off"] = True
            for st in ("checked", "selected"):
                if a.get(st) == "true":
                    node[st] = True
        nodes.append(node)
        if len(nodes) >= max_nodes:
            break
    return nodes


def add_common_args(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    p.add_argument("--serial", default=None, help="多设备时指定 adb serial")
    p.add_argument("--dir", default=None, help="证据根目录（默认 storage/evidence）")
    p.add_argument("--name", default=None,
                   help="证据名，如 case172/step01；缺省自动派生 <会话id>/step<N>")
    p.add_argument("--full", action="store_true",
                   help="nodes 给全量语义节点（检查模式；默认导航模式，只给可点/可滑动节点）")
    p.add_argument("--no-filter", action="store_true", dest="no_filter",
                   help="不过滤系统界面节点（默认只保留当前前台包的节点）")
    p.add_argument("--settle", type=int, default=600,
                   help="动作后稳定窗口毫秒数，默认 600（仅 act 用）")
    p.add_argument("--perm-action", default=None, choices=["grant", "deny"],
                   dest="perm_action",
                   help="声明权限意图（**仅 act 用**）：测「拒绝」路径时才需要，"
                        "不声明默认点同意")
    p.add_argument("--perm", default=None,
                   help="配合 --perm-action：权限类型标记（如 media_images/camera）")
    p.add_argument("--expect", default=None, dest="expect",
                   help="检查点（**仅 act 用**）：动作后校验落点。"
                        "形如 activity=EditTimetableActivity 或 rid=et_schedule_name，"
                        "逗号=AND、竖线=任一。由工具判", )
    return p


def add_common_args_all_subcommands(p: argparse.ArgumentParser) -> None:
    """把全部公共参数也挂到每个子命令上（须在 add_subparsers 之后调）。

    ⚠️ 背景：argparse 的父级参数**只能写在子命令之前**，于是
    `act.py tap --serial X` 报 unrecognized arguments，而
    `observe.py --serial X` 却正常——同一份 SKILL.md 说"多设备加 --serial"，
    两个工具的正确写法却不同，AI 必然踩（真实事故）。
    这里给每个子命令再挂一份同名参数，两种位置都能解析。

    ⚠️⚠️ 关键坑（踩过，务必保留 SUPPRESS）：
    argparse 解析时**父子同名 dest 会互相覆盖**——子解析器先以默认值写入
    namespace，父级再解析时（若参数写在前面）又被子命令的默认值盖回去，
    结果是"命令不报错、但值丢了"。实测 `--perm-action grant` 写在子命令前时
    解析结果为 None，意图静默失效。
    解法：子命令上的同名参数一律 `default=argparse.SUPPRESS`——
    这样**只有显式传了该 flag 才会写入 namespace**，没传就不碰父级的值。
    """
    specs = [
        ("--serial", {"default": argparse.SUPPRESS, "help": argparse.SUPPRESS}),
        ("--dir", {"default": argparse.SUPPRESS, "help": argparse.SUPPRESS}),
        ("--name", {"default": argparse.SUPPRESS, "help": argparse.SUPPRESS}),
        ("--full", {"action": "store_true", "default": argparse.SUPPRESS,
                    "help": argparse.SUPPRESS}),
        ("--no-filter", {"action": "store_true", "dest": "no_filter",
                         "default": argparse.SUPPRESS, "help": argparse.SUPPRESS}),
        ("--settle", {"type": int, "default": argparse.SUPPRESS,
                      "help": argparse.SUPPRESS}),
        ("--perm-action", {"default": argparse.SUPPRESS,
                           "choices": ["grant", "deny"],
                           "dest": "perm_action", "help": argparse.SUPPRESS}),
        ("--perm", {"default": argparse.SUPPRESS, "help": argparse.SUPPRESS}),
        ("--expect", {"default": argparse.SUPPRESS, "dest": "expect",
                      "help": argparse.SUPPRESS}),
    ]
    subparsers = [a for a in p._actions if isinstance(a, argparse._SubParsersAction)]
    for sp in subparsers:
        for choice in sp.choices.values():
            existing = getattr(choice, "_option_string_actions", {})
            for flag, kw in specs:
                if flag not in existing:
                    choice.add_argument(flag, **kw)


def emit(ok: bool, payload: dict) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK
    except AttributeError:
        pass
    if ok:
        runtime.execution("executed")
    result = {"ok": ok, **payload, "call_id": CALL_ID,
              "event_ids": list(EVENT_IDS), "execution_state": runtime.EXECUTION_STATE}
    with stage("serialization"):
        text = json.dumps(result, ensure_ascii=False, indent=2)
    timing = {**timing_snapshot(), "response_bytes": len((text + "\n").encode("utf-8"))}
    timing_file = os.environ.get("ANDROID_TEST_TIMING_FILE")
    if timing_file:
        try:
            path = Path(timing_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(timing), encoding="utf-8")
        except OSError as error:
            print(text)
            print(json.dumps({"audit_failed": True, "call_id": CALL_ID,
                              "stage": "timing_write", "error": str(error)}), file=sys.stderr)
            raise SystemExit(1)
    if os.environ.get("ANDROID_TEST_VERBOSE_TIMING") == "1":
        result["timing"] = timing
        text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)


def fail(msg: str, *, rejected: bool = False) -> None:
    emit(False, {"error": msg, **({"rejected": True} if rejected else {})})
    sys.exit(1)


def guard_terminated(tool: str) -> None:
    """硬性拦截：会话已因崩溃终止时，拒绝执行任何观测/动作。

    ⚠️ 用户明确要求："发现 Crash/ANR 就直接停止这个 case 的执行，不要做多余的事"。
    只发 `terminate` 信号靠 AI 自觉是不够的——AI 可能仍去截图/点击，
    产生不可信数据和无谓动作。故在工具入口硬拦。

    放行方式：session.py resume --id <N>（人工确认后清除终态）。
    未开会话（无 current）时不拦，保持工具可独立使用。
    """
    try:
        runtime.execution("not_executed")
        from db import current_session, get_db
        sid = current_session()
        if not sid:
            return
        if recording_issues(STORAGE_DIR, sid):
            fail(f"Session {sid} has incomplete recording. Resolve it and finish ERROR/BLOCKED; do not replay actions.", rejected=True)
        reason = get_db().get_termination(sid)
    except (OSError, ValueError, sqlite3.Error) as error:
        fail(f"Cannot verify session guard: {error}")
    if not reason:
        return
    emit(False, {
        "terminated": True,
        "rejected": True,
        "session_id": sid,
        "error": f"会话 #{sid} 已终止，拒绝执行 {tool}",
        "reason": reason,
        "hint": ("被测/关联包已崩溃/ANR —— 本 case 到此结束，不要做多余的事。"
                 "请记 BLOCKED finding 并 finish --status BLOCKED 收尾。"
                 f"确需继续请先 session.py resume --id {sid}（并说明理由）"),
    })
    sys.exit(3)  # 独立退出码，便于调用方区分"被拦截"与"执行失败"


def settle(sec: float = 0.6) -> None:
    """动作后给画面一个极短的稳定窗口。这是工具层唯一的"等待"，固定且极短。"""
    time.sleep(sec)
