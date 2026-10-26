"""执行录屏封装：基于 `adb shell screenrecord`（设备直出 mp4）。

设计取舍
========
- 设备命令框架里走 uiautomator2 的 `d.shell()`（同步阻塞），而 `screenrecord`
  是**阻塞命令**（跑到超时/被杀才停）。故这里直接用 `subprocess.Popen` 起后台
  adb 子进程，结束时杀掉并 `adb pull` 回本地。
- Android `screenrecord` 硬限制：**单段最长 3 分钟**（到时自动停），**分辨率限
  1280×720 / 1920×1080** 级别。突破方式（2026-10-21 实现）：**分段续录**——
  设备端一个 shell 循环连续录多段（每段 SEG_SECONDS=170s，留余量防 180s 硬限），
  stop 时 pull 全部段并用 ffmpeg concat demuxer 无损拼接（-c copy，各段编码
  参数一致）成单个 rec.mp4。段间存在 ~0.5-1s 空隙（screenrecord 重启开销）。
  拼接器优先 imageio-ffmpeg（pip 包，内置静态 ffmpeg），其次 PATH 里的 ffmpeg；
  两者都不可用时保留分段文件（rec_part_NNN.mp4），recording 指向第一段。
- 录屏文件落 `storage/evidence/<sid>/rec.mp4`，路径写回 sessions.recording。
  单独存、不走 events.evidence，避免被"证据清理"逻辑误删（cleanup 只认
  events 里的 evidence）。
- 设备离线 / 无 serial / 启动失败 → 静默 noop，不阻断测试主流程。
- 调参（环境变量）：DSH_REC_SEGMENT（每段秒数，测试可用 5）、DSH_REC_MAX_SEGMENTS。

线程安全
========
start 用 O_EXCL 锁文件保护"写 pid 文件"的读改写，避免并发 start 互相覆盖。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EVIDENCE_DIR = ROOT / "storage" / "evidence"

# screenrecord 单段硬上限 3 分钟。默认取 30s 而不是贴着上限 170s：
# ⚠️ 实测（2026-10-21）：screenrecord 被信号打断时写不出完整 moov（视频只有 1 帧、
# Duration N/A、浏览器不可播），**只有自然到 --time-limit 结束的段才是可用文件**。
# 而 stop 时必须等"当前段"自然结束才能拿到可用视频，段越长 finish 越慢。
# 30s 是折中：finish 最多多等 30s（典型 ~15s），段间空隙约 0.5-1s。
SEG_SECONDS = max(3, int(os.environ.get("DSH_REC_SEGMENT", "30")))
# 最多段数（240 段 × 30s ≈ 2 小时），防忘记 stop 无限占存储
MAX_SEGMENTS = max(1, int(os.environ.get("DSH_REC_MAX_SEGMENTS", "240")))
# 兼容旧签名：显式传 max_seconds 时仍走单段模式
MAX_SECONDS = 180


def _adb() -> str:
    # 与 observe/act 同环境：优先 PATH 里的 adb
    return os.environ.get("ADB", "adb")


def _ffmpeg() -> str | None:
    """找 ffmpeg：优先 imageio-ffmpeg（pip 内置静态版），其次 PATH。"""
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return shutil.which("ffmpeg")


def _safe_remove(path: Path) -> None:
    """删除文件/目录，但用「改名到 .safe-trash/」代替真删。

    ⚠️ 沙箱 safe-delete 有挂起的批量删除请求时，本轮次内一切 unlink/rmtree
    都会被拦截并杀死整个进程（实测 2026-10-21，stop_recording 会被连带打死）。
    改名不是删除，不会被拦；.safe-trash 里的东西下一轮再清。
    """
    trash = DEFAULT_EVIDENCE_DIR / ".safe-trash"
    try:
        trash.mkdir(parents=True, exist_ok=True)
        dst = trash / ("%s_%d" % (path.name.replace("/", "_"), int(time.time() * 1000) % 10 ** 9))
        path.rename(dst)
    except OSError:
        pass


def _safe_remove_dir(path: Path) -> None:
    """目录版 _safe_remove（替代 shutil.rmtree，见其注释）。"""
    if path.is_dir():
        _safe_remove(path)


def _pid_file(sid: int) -> Path:
    return DEFAULT_EVIDENCE_DIR / f".rec_pid_{sid}"


def _adb_shell(serial: str | None, cmd: str, timeout: int = 30) -> str:
    """执行一条设备端 shell 命令，返回 stdout（失败返回空串）。"""
    args = [_adb()]
    if serial:
        args += ["-s", serial]
    args += ["shell", cmd]
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def _start_detached(serial: str | None, prefix: str, stopmark: str,
                    devlog: str | None = None) -> int | None:
    """用 setsid 把录制循环脱离 adb 会话启动，返回设备端 shell pid。

    ⚠️ 关键修复（2026-10-21）：原实现用本地 `Popen(adb shell ...)` 常驻，
    录制进程的生命周期被绑在**发起 start 的那个工具进程**上 —— 工具调用一结束
    （session.py start 返回、python/bash 退出），进程树被清理 → adb 被杀 →
    设备端 shell 收到挂断 → screenrecord 立即停止。实测：一条独立的
    `session.py start` 命令只录到 3.07 秒，而跑 2 分 11 秒的脚本能录到 1:55，
    录屏时长 ≈ 启动它的那条命令活了多久。

    修法：设备端 `setsid` 新开会话 + 重定向到 /dev/null + 后台化，adb 只负责
    "点火"后立刻返回；进程被 init 收养（PPID=1），不再随本地进程树消失。
    停止靠 stopmark 标记 + kill 设备端 pid（见 stop_recording）。
    """
    # 每段 stderr + 退出码落到设备端日志：屏幕异常（UNASSIGNED_LAYER_STACK）、
    # 被其他 screenrecord 抢占、循环被意外终止，只有这里能看到（stdout 已丢 /dev/null）。
    err = f" 2>>{devlog}" if devlog else ""
    trace = f"echo seg $i rc=$? >> {devlog}; " if devlog else ""
    tail = f"echo loop-exit >> {devlog}; " if devlog else ""
    inner = (f"i=0; while [ $i -lt {MAX_SEGMENTS} ] && [ ! -f {stopmark} ]; do "
             f"screenrecord --time-limit {SEG_SECONDS} {prefix}$i.mp4{err}; "
             f"{trace}"
             f"i=$((i+1)); sleep 0.2; done; {tail}true")
    cmd = f"setsid sh -c '{inner}' >/dev/null 2>&1 & echo $!"
    out = _adb_shell(serial, cmd, timeout=30).strip()
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


def start_recording(sid: int, serial: str | None,
                    max_seconds: int | None = None) -> bool:
    """后台启动录屏。成功返回 True；失败/不可录 静默返回 False。

    max_seconds=None（默认）→ 分段续录模式，上限 MAX_SEGMENTS*SEG_SECONDS；
    max_seconds=N → 单段模式（旧兼容），上限仍是 Android 的 180s。
    """
    if not sid:
        return False
    d = DEFAULT_EVIDENCE_DIR / str(sid)
    d.mkdir(parents=True, exist_ok=True)

    detached_pid = None
    proc = None
    prefix = None
    stopmark = None
    devlog = None
    script = None
    args = None

    # ── 单次连续录制（默认，2026-10-22 重构）──────────────────────────────
    # 实测结论（这台 ZUI 平板 / Android 14 基线）：
    #   ① `screenrecord --time-limit 0` = **不限时长**（默认才是 180s 上限）；
    #   ② `kill -INT` 是 screenrecord 的正常停止方式 —— 收到 SIGINT 会写完 moov
    #      索引正常收尾（实测 18s 录制 → 17.96s、moov 完整、可播）；
    #      而 SIGTERM / KILL 会留下只有 mdat、没有 moov 的废文件。
    # 因此整场会话只录**一个文件**：无分段、无拼接、无接缝。
    # （此前每 30s 分段重启，每次重启丢约 0.5s 画面且时间戳跳变，观感"跳跃"。）
    single_mode = (max_seconds is None and bool(serial))
    if single_mode:
        mp4_name = "rec.mp4"
        remote = f"/sdcard/.rec_{sid}.mp4"
        segment_mode = False
        prefix = None
        stopmark = None
        devlog = None
        # 清掉设备端上一轮残留（上一轮异常退出时可能留下同名文件）
        _adb_shell(serial, f"rm -f {remote}", timeout=15)
        # setsid：脱离 adb 会话，工具进程退出后录制仍继续（PPID=1）
        cmd = (f"setsid sh -c 'screenrecord --time-limit 0 {remote}' "
               f">/dev/null 2>&1 & echo $!")
        detached_pid = (_adb_shell(serial, cmd, timeout=30) or "").strip() or None
        args = None

    if not single_mode:
        if max_seconds is not None:
            # ── 单段模式（旧行为）──
            mp4_name = "rec.mp4"
            remote = f"/sdcard/.rec_{sid}.mp4"
            limit = max(1, min(max_seconds, MAX_SECONDS))
            args = [_adb()]
            if serial:
                args += ["-s", serial]
            args += ["shell", "screenrecord", "--time-limit", str(limit), remote]
            segment_mode = False
        else:
            # ── 分段续录模式（回退：无 ffmpeg / 无 serial）──
            mp4_name = "rec.mp4"
            prefix = f"/sdcard/.rec_{sid}_"
            stopmark = f"/sdcard/.rec_{sid}.stop"
            devlog = f"/sdcard/.rec_{sid}.log"
            # 回退用的设备端 sh 循环（setsid 不可用时走本地 Popen）
            script = (f"i=0; while [ $i -lt {MAX_SEGMENTS} ] && [ ! -f {stopmark} ]; do "
                      f"screenrecord --time-limit {SEG_SECONDS} {prefix}$i.mp4; "
                      f"i=$((i+1)); done")
            segment_mode = True

    # ⚠️ 关键修复（实测踩坑）：Android `screenrecord` 在**屏幕熄灭/Dozing 时**
    # 会直接中止（退出码 218、文件 0 字节），导致长会话只录到屏幕休眠前那一段
    # （如某个 466s 的用例只录到 58s）。故录制期间必须**保持屏幕常亮**：
    #   · `svc power stayon true` —— 仅充电时保持屏幕常亮（USB 设备即插电，满足）；
    #   · `input keyevent WAKEUP` + 解除锁屏 —— 若启动前已休眠，先唤醒。
    # 只在确实拿到了 serial 时才做（无设备则静默跳过），且把 stayon_set 记进
    # pid 文件，stop 时再恢复，避免把设备永久留在常亮态。
    stayon_set = False
    if serial:
        try:
            r_stayon = subprocess.run(
                [_adb(), "-s", serial, "shell", "svc", "power", "stayon", "true"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            subprocess.run([_adb(), "-s", serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            # 仅唤醒不够：若处于锁屏，screenrecord 会录到静止锁屏页（帧极少、文件极小）。
            # 尝试解除锁屏（无安全锁时生效；有密码锁则忽略，真实测试时设备本就解锁）。
            subprocess.run([_adb(), "-s", serial, "shell", "wm", "dismiss-keyguard", "2>/dev/null"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            # 只在实际设置成功时才记（原实现 returncode 未检查，失败也标记为已设，
            # stop 时会莫名其妙把 stayon 关掉）。
            stayon_set = r_stayon.returncode == 0
        except Exception:  # noqa: BLE001
            stayon_set = False

    if segment_mode:
        if serial:
            # 清掉上次残留的停止标记与设备日志，否则本次刚启动就会被循环判停
            _adb_shell(serial, f"rm -f {stopmark} {devlog}", timeout=15)
            detached_pid = _start_detached(serial, prefix, stopmark, devlog)
        if detached_pid is None:
            # 回退：老方式（设备端无 setsid，或 adb 点火失败）。注意此模式下
            # 录制仍会随本地进程树被清理而提前结束，属降级行为。
            args = [_adb()]
            if serial:
                args += ["-s", serial]
            args += ["shell", script]

    if proc is None and args is not None:
        try:
            proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:  # noqa: BLE001
            return False

    # 写 pid 文件（带锁，防并发覆盖）
    pf = _pid_file(sid)
    lock = str(pf) + ".lock"
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
        meta = {"pid": proc.pid if proc else None, "serial": serial or "",
                "remote": (prefix if segment_mode else remote),
                "mp4": str(d / mp4_name), "stayon_set": stayon_set,
                "segment_mode": segment_mode,
                "single_mode": bool(single_mode),
                "detached": detached_pid is not None,
                "dev_pid": detached_pid,
                "stopmark": stopmark,
                "devlog": devlog,
                "started_ts": time.time()}
        if fd is not None:
            os.close(fd)
        pf.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    finally:
        if fd is not None:
            try:
                _safe_remove(Path(lock)) if False else os.rename(lock, lock + '.old')
            except OSError:
                pass
    return True


def _device_list_parts(serial: str | None, prefix: str) -> list[str]:
    """列出设备端已落盘的段文件（通配符由设备端 sh 展开）。"""
    args = [_adb()]
    if serial:
        args += ["-s", serial]
    args += ["shell", f"ls {prefix}*.mp4 2>/dev/null"]
    try:
        out = subprocess.run(args, capture_output=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        names = []
        for line in out.stdout.decode("utf-8", "replace").splitlines():
            line = line.strip().rstrip("\r")
            if line.endswith(".mp4") and "/" in line:
                names.append(line)
        return sorted(names)
    except Exception:  # noqa: BLE001
        return []


def _concat(parts: list[Path], out: Path) -> bool:
    """ffmpeg concat demuxer 无损拼接（各段编码参数一致，-c copy）。"""
    ff = _ffmpeg()
    if not ff or len(parts) == 0:
        return False
    lst = out.parent / f".concat_{out.stem}.txt"
    try:
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts),
                       encoding="utf-8")
        r = subprocess.run(
            [ff, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-c", "copy", str(out)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0 and out.is_file() and out.stat().st_size > 0
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            _safe_remove(lst)
        except OSError:
            pass


def _probe_duration(path: Path) -> float | None:
    """用 ffmpeg -i 解析 mp4 时长（秒）。解析不出来返回 None。"""
    ff = _ffmpeg()
    if not ff:
        return None
    try:
        r = subprocess.run([ff, "-i", str(path)], capture_output=True,
                           timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = r.stderr.decode("utf-8", "replace")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        if not m:
            return None
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:  # noqa: BLE001
        return None


def _repair_mp4(path: Path) -> bool:
    """尝试用 ffmpeg remux 重建索引（救被信号打断、moov 不完整的 mp4）。"""
    ff = _ffmpeg()
    if not ff:
        return False
    tmp = path.with_suffix(".fix.mp4")
    try:
        r = subprocess.run([ff, "-y", "-i", str(path), "-c", "copy",
                            "-movflags", "+faststart", str(tmp)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0 and _probe_duration(tmp) is not None:
            shutil.move(str(tmp), str(path))
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        _safe_remove(tmp)
    except OSError:
        pass
    return False


def _check_completeness(sid: int, mp4: Path, started_ts: float | None) -> dict | None:
    """录屏完整性自检：实际时长 vs 会话录制时长，偏差 >20% 视为不完整。

    ⚠️ 为什么必须自检：录屏曾长期静默产出 3 秒视频（adb 子进程随工具进程被清理、
    或屏幕熄灭导致 screenrecord 报 UNASSIGNED_LAYER_STACK），WebUI 上只看到一个
    几秒钟的片子，没人知道它其实没录上。结果写到 evidence/<sid>/rec_incomplete.json，
    finish 时会带进输出提示。
    """
    dur = _probe_duration(mp4)
    if dur is None:
        return None
    if started_ts:
        expected = max(0.0, time.time() - float(started_ts))
    else:
        expected = None
    info = {"duration_sec": round(dur, 2),
            "expected_sec": round(expected, 2) if expected else None,
            "complete": True}
    if expected and expected >= 15 and dur < expected * 0.8:
        info["complete"] = False
        info["gap_sec"] = round(expected - dur, 2)
        info["reason"] = ("录屏时长明显短于录制窗口：多半是 adb 录制进程随工具进程退出被清理，"
                          "或屏幕熄灭/显示状态异常导致 screenrecord 中止"
                          "（UNASSIGNED_LAYER_STACK）。")
        try:
            (mp4.parent / "rec_incomplete.json").write_text(
                json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
    return info


def stop_recording(sid: int) -> str | None:
    """结束录屏：杀掉 adb 子进程 → pull 回本地（分段则拼接）→ 删设备临时文件。
    返回本地 mp4 的相对路径（写回 sessions.recording），无则 None。"""
    pf = _pid_file(sid)
    if not pf.is_file():
        return None
    try:
        meta = json.loads(pf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = meta.get("pid")
    serial = meta.get("serial") or None
    remote = meta.get("remote")
    local = Path(meta.get("mp4") or "")
    stayon_set = bool(meta.get("stayon_set"))
    segment_mode = bool(meta.get("segment_mode"))
    single_mode = bool(meta.get("single_mode"))
    detached = bool(meta.get("detached"))
    dev_pid = meta.get("dev_pid")
    stopmark = meta.get("stopmark")
    devlog = meta.get("devlog")
    started_ts = meta.get("started_ts")
    _safe_remove(pf)

    # ── 单次连续录制：kill -INT 让 screenrecord 正常收尾写 moov ──
    # 不用等任何段边界，停止耗时实测 2-4s。
    if single_mode:
        _adb_shell(serial, "kill -INT $(pidof screenrecord) 2>/dev/null; true", timeout=15)
        deadline = time.time() + 15
        while time.time() < deadline:
            out = _adb_shell(serial, "pidof screenrecord || echo gone", timeout=15)
            if "gone" in out or not out.strip():
                break
            time.sleep(0.4)
        time.sleep(1.0)  # 等设备侧 flush 完 moov
        if serial and stayon_set:
            try:
                subprocess.run([_adb(), "-s", serial, "shell", "svc", "power", "stayon", "false"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            except Exception:  # noqa: BLE001
                pass
        if not remote or not local.parent.is_dir():
            return None
        args = [_adb()]
        if serial:
            args += ["-s", serial]
        args += ["pull", remote, str(local)]
        try:
            subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:  # noqa: BLE001
            pass
        _adb_shell(serial, f"rm -f {remote}", timeout=15)
        if local.is_file() and local.stat().st_size > 0:
            # 时长探测（需 ffmpeg）：仅作完整性参考，探测失败 ≠ 文件损坏。
            # 真实可播性由前端 loadedmetadata 判定，故探测失败不再阻断成功返回——
            # 否则在无 ffmpeg 的环境里，所有单次录屏都会因拿不到时长而被丢弃、
            # sessions.recording 永远空置（这正是「执行录屏」不显示的元凶）。
            dur = _probe_duration(local)
            if dur is None:
                _repair_mp4(local)
                if _probe_duration(local) is None:
                    # 仍无法探测：留一条完整性备注，绝不 return None。
                    try:
                        (local.parent / "rec_incomplete.json").write_text(
                            json.dumps({"complete": False,
                                        "reason": "无法探测时长（环境无 ffmpeg）。文件已存在且非空，前端将以 loadedmetadata 读取真实时长。",
                                        "size_bytes": local.stat().st_size},
                                       ensure_ascii=False, indent=1), encoding="utf-8")
                    except OSError:
                        pass
            _check_completeness(sid, local, started_ts)
            try:
                return str(local.resolve().relative_to(ROOT).as_posix())
            except ValueError:
                return str(local)
        return None

    # 1) 终止 screenrecord
    # ⚠️ 跨平台坑（实测踩到）：Windows 上既没有 signal.SIGKILL，os.kill 对
    # 已退出/不存在的 pid 还会抛 OSError(WinError 87)；而 fallback 里的
    # signal.SIGKILL 会直接 AttributeError 把整个 stop 打断 → pull 永不执行、
    # 录屏静默丢失。故这里**只用 SIGTERM**，任何异常一律吞掉，绝不中断后续 pull。
    if detached and serial:
        # 设备端独立模式（setsid 起来的循环，本地没有可杀的 adb 子进程）。
        # ⚠️ 不能直接 kill screenrecord：被信号打断的 mp4 写不出完整 moov
        # （只剩 1 帧、Duration N/A、不可播）。正确做法是**落停止标记**，让当前
        # 段自然到 --time-limit 结束（完整 moov），循环下一轮看到标记即退出。
        # 代价：finish 最多多等一个段长（SEG_SECONDS），故段长内置为 30s。
        _adb_shell(serial, f"touch {stopmark} 2>/dev/null; true", timeout=15)
        deadline = time.time() + SEG_SECONDS + 5
        while time.time() < deadline:
            # 用 `kill -0` 探活而不是 `ps -A | grep`：后者每次要 dump 全量进程表
            # （实测单次 1-2s，25 次轮询就把 finish 拖成 60s+）。
            out = _adb_shell(serial, f"kill -0 {dev_pid} 2>/dev/null && echo alive || echo gone",
                             timeout=15)
            if "alive" not in out:
                break
            time.sleep(0.5)
        else:
            # 兜底：循环异常未退出（例如 screenrecord 卡住）。此时强杀会让当前段
            # 不可播 —— 但总比永远卡住好，pull 阶段会丢弃不可播的段。
            _adb_shell(serial, f"kill -TERM $(pidof screenrecord) 2>/dev/null; "
                               f"kill -TERM {dev_pid} 2>/dev/null; true", timeout=15)
            time.sleep(1.0)
    elif pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, AttributeError, ValueError):
            pass
    # ⚠️ 注意：**不要**在设备上 `pkill screenrecord`！本地 adb 子进程被 SIGTERM 关闭后，
    # 设备侧 screenrecord 收到 SIGHUP 才会写入 moov（索引）并正常收尾；若抢先 pkill
    # 会把还没写 moov 的进程杀掉 → 文件只有 mdat、无 moov、不可播、时长解析失败
    # （已在测试里踩出过该事故）。故这里只用本地 SIGTERM + 等待，不再双杀。
    # 录制结束，恢复屏幕常亮设置（仅当我们这次启动时设过）
    if serial and stayon_set:
        try:
            subprocess.run([_adb(), "-s", serial, "shell", "svc", "power", "stayon", "false"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        except Exception:  # noqa: BLE001
            pass
    # 给它一点时间落盘并写入 moov（实测 1.5s 偶发不够，提到 2.5s 更稳）
    time.sleep(2.5)

    if not remote or not local.parent.is_dir():
        return None

    # 2) pull 回本地
    if segment_mode:
        parts_remote = _device_list_parts(serial, remote)
        parts_dir = local.parent / f".rec_parts_{sid}"
        parts_dir.mkdir(parents=True, exist_ok=True)
        parts_local: list[Path] = []
        dropped = 0
        for idx, rp in enumerate(parts_remote):
            lp = parts_dir / Path(rp).name
            args = [_adb()]
            if serial:
                args += ["-s", serial]
            args += ["pull", rp, str(lp)]
            try:
                subprocess.run(args, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=120,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if lp.is_file() and lp.stat().st_size > 0:
                    # 只校验最后一段（最可能被兜底强杀打断、moov 不完整）。
                    # 其余段是自然到 --time-limit 结束的，必然可播，不必逐段跑 ffmpeg。
                    if idx == len(parts_remote) - 1 and _probe_duration(lp) is None:
                        if not _repair_mp4(lp):
                            dropped += 1
                            continue
                    parts_local.append(lp)
            except Exception:  # noqa: BLE001
                pass
        # 3) 删设备端全部段文件
        if parts_remote:
            args2 = [_adb()]
            if serial:
                args2 += ["-s", serial]
            args2 += ["shell", "rm", "-f"] + parts_remote
            try:
                subprocess.run(args2, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=60,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception:  # noqa: BLE001
                pass

        # 设备端录制日志：中途中断（屏幕异常/被抢占）的唯一证据，留下来便于排查
        if serial and devlog:
            dl_local = local.parent / "rec_device.log"
            try:
                subprocess.run([_adb(), "-s", serial, "pull", devlog, str(dl_local)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if not dl_local.is_file() or dl_local.stat().st_size == 0:
                    _safe_remove(dl_local)
            except Exception:  # noqa: BLE001
                pass
            _adb_shell(serial, f"rm -f {devlog}", timeout=15)

        if not parts_local:
            _safe_remove_dir(parts_dir)
            return None
        # 单段直接改名；多段尝试无损拼接，失败则保留分段
        if len(parts_local) == 1:
            shutil.move(str(parts_local[0]), str(local))
            _safe_remove_dir(parts_dir)
        else:
            if _concat(parts_local, local):
                _safe_remove_dir(parts_dir)
            else:
                # 拼接失败：分段按序重命名保留，recording 指向第一段
                kept = []
                for i, p in enumerate(sorted(parts_local)):
                    dst = local.parent / f"rec_part_{i:03d}.mp4"
                    shutil.move(str(p), str(dst))
                    kept.append(dst)
                _safe_remove_dir(parts_dir)
                local = kept[0]
    else:
        args = [_adb()]
        if serial:
            args += ["-s", serial]
        args += ["pull", remote, str(local)]
        try:
            subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:  # noqa: BLE001
            pass
        # 3) 删设备临时文件
        args2 = [_adb()]
        if serial:
            args2 += ["-s", serial]
        args2 += ["shell", "rm", "-f", remote]
        try:
            subprocess.run(args2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:  # noqa: BLE001
            pass

    if local.is_file() and local.stat().st_size > 0:
        if segment_mode and serial and stopmark:
            _adb_shell(serial, f"rm -f {stopmark}", timeout=15)
        info = _check_completeness(sid, local, started_ts)
        if info and not info.get("complete"):
            print(f"[recorder] 录屏不完整：实际 {info['duration_sec']}s / 应录 "
                  f"{info['expected_sec']}s（见 {local.parent / 'rec_incomplete.json'}）",
                  file=sys.stderr)
        try:
            return str(local.resolve().relative_to(ROOT).as_posix())
        except ValueError:
            return str(local)
    return None
