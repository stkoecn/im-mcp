"""绿泡泡数据刷新与定时自动同步调度器。

背景（实测确认的上游设计缺口）
--------------------------------------------------
``wechat-mcp`` 的索引建立在**解密快照** ``~/.wechat_mcp/decrypted/`` 之上，而该快照
只由 ``wechat-init`` 生成。上游的 ``sync_wechat_database`` 只调用 ``sync_data()``
（重建 ETL），**不会重新解密** —— 源库（加密的 ``db_storage``）里新写入的消息永远
进不了索引，工具名承诺的「保持最新状态」实际做不到。

本模块补上这一环，并提供进程内定时同步。

为什么用子进程而不是直接调 key_tool 的函数
--------------------------------------------------
``key_tool._decrypt_database`` 内部有 ``_print()`` 进度输出。MCP 走 stdio 传
JSON-RPC，**任何 stdout 输出都会破坏协议**。子进程把输出全部隔离在管道里，同时复用
已经完全验证过的 CLI 路径，比在进程内临时重定向 ``sys.stdout`` 更安全（后者与 MCP
自身的并发写存在竞态，且 ``decrypt_all`` 会用 ``sys.exit()`` 结束进程）。

定时器是**进程内后台线程**：只在 wechat-mcp 进程存活期间运行（随 Snow App 启停）。
开关与间隔持久化到 ``~/.wechat_mcp/auto_sync.json``，服务启动时自动恢复。
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import USER_WECHAT_DIR, get_decrypted_dir
from .database import sync_data

logger = logging.getLogger(__name__)

STATE_FILE = USER_WECHAT_DIR / "auto_sync.json"

# 间隔钳制区间：低于 5 分钟没有意义（一次刷新本身就要几秒），
# 高于 24 小时则失去「定时」的意义。
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 1440
DEFAULT_INTERVAL_MINUTES = 30

# 单次重新解密的超时。实测 20 个库 / 0.3GB 约 2–3 秒，
# 留足余量应对磁盘慢或微信正在大量写入。
REDECRYPT_TIMEOUT_SECONDS = 600

_stop_event = threading.Event()
_worker: Optional[threading.Thread] = None
# 手动「后台同步」线程：start_sync 用它避免调用方等待超时
_sync_thread: Optional[threading.Thread] = None
# 本次后台同步的启动时间（epoch 秒）：供 get_auto_sync_status 报告「已跑多久」
_sync_started_at: Optional[float] = None
# 僵尸判定阈值：后台同步超过该时长仍未结束，视为卡死，允许重新发起（实测一次同步 1–3 分钟）
SYNC_STALE_SECONDS = 20 * 60
# 独立同步进程的心跳状态：单独一个文件，避免 MCP 进程与 worker 同时读改写 auto_sync.json 互相覆盖
SYNC_STATE_FILE = USER_WECHAT_DIR / "sync_state.json"
# 心跳写入间隔 / 心跳过期阈值（过期且进程已退出即视为僵死）
SYNC_HEARTBEAT_SECONDS = 5
SYNC_HEARTBEAT_STALE_SECONDS = 90
# 独立进程启动宽限期：等它写出第一份心跳；超过即认定未能存活（宿主可能在 MCP 退出时
# 终止其子进程），start_sync 会清理残留并返回 started=false，让调用方回退阻塞同步。
WORKER_STARTUP_GRACE_SECONDS = 3.0
_lock = threading.RLock()


# ------------------------------------------------------------------
# 1) 重新解密（子进程，静默）
# ------------------------------------------------------------------

def _db_dir_from_keys(keys_path: Path) -> Optional[str]:
    """读取密钥文件里记录的 ``_db_dir``（``wechat-init`` 提取密钥时写入的目录）。

    为什么需要这条回退：``wechat-mcp`` 由宿主进程（Snow App / Electron）启动，
    其环境变量与用户在终端里跑 ``wechat-init`` 时并不相同 —— 缺 ``APPDATA`` /
    ``USERPROFILE`` 时 ``auto_detect_db_dir()`` 会返回 None，同步就会以
    「未找到绿泡泡 db_storage 目录」失败，而密钥文件里其实一直记着当初成功
    解密用过的那个目录。
    """
    try:
        with open(keys_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001 - 文件损坏/不可读时静默走下一级回退
        return None
    value = data.get("_db_dir") if isinstance(data, dict) else None
    if isinstance(value, str) and value.strip() and os.path.isdir(value):
        return value
    return None


def refresh_decrypted(db_dir: Optional[str] = None,
                      keys_file: Optional[Path] = None) -> Dict[str, Any]:
    """重新解密源库到解密目录（含 WAL 合并）。

    返回 ``{"ok": bool, ...}``；失败时 ``error`` 说明原因。本函数不抛异常，
    也不向 stdout 写任何内容。
    """
    keys_path = Path(keys_file) if keys_file else USER_WECHAT_DIR / "all_keys.json"
    if not keys_path.exists():
        return {"ok": False,
                "error": f"密钥文件不存在：{keys_path}。请先运行 wechat-init 提取密钥。"}

    # db_storage 定位按可靠性递减：显式参数 → 环境变量 → 自动探测 → 密钥文件记录。
    # 任一级命中即用，全部失败才报错（错误信息给出两条可操作出路）。
    if not db_dir:
        env_dir = (os.environ.get("WECHAT_DB_DIR") or "").strip()
        if env_dir and os.path.isdir(env_dir):
            db_dir = env_dir
    if not db_dir:
        try:
            from .tools.key_tool import auto_detect_db_dir
            db_dir = auto_detect_db_dir()
        except Exception as exc:  # noqa: BLE001 - 探测失败不致命，下面还有密钥文件回退
            logger.warning(f"自动检测 db_storage 失败：{exc}")
    if not db_dir:
        db_dir = _db_dir_from_keys(keys_path)
    if not db_dir or not os.path.isdir(db_dir):
        return {"ok": False, "error": (
            "未找到绿泡泡 db_storage 目录（自动检测失败，密钥文件里也没有可用的 _db_dir）。"
            "可用环境变量 WECHAT_DB_DIR 指定数据目录，或重新运行 wechat-init。"
        )}

    out_dir = get_decrypted_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "wechat_mcp.tools.key_tool", "decrypt",
        "--db-dir", str(db_dir),
        "--keys", str(keys_path),
        "--output", str(out_dir),
    ]
    env = dict(os.environ)
    # 子进程输出按 UTF-8 解码，避免 Windows 默认代码页把中文进度串弄成乱码
    env["PYTHONIOENCODING"] = "utf-8"

    started = time.time()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=REDECRYPT_TIMEOUT_SECONDS, cwd=str(USER_WECHAT_DIR.parent),
            # MCP 是 stdio 服务：绝不能把 JSON-RPC 管道继承给子进程（防御性隔离）
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"重新解密超时（>{REDECRYPT_TIMEOUT_SECONDS}s）"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"启动重新解密失败：{exc}"}

    tail = "\n".join((proc.stdout or "").strip().splitlines()[-6:])
    result: Dict[str, Any] = {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "seconds": round(time.time() - started, 1),
        "db_dir": str(db_dir),
        "output_dir": str(out_dir),
        "tail": tail,
    }
    if proc.returncode != 0:
        result["error"] = (proc.stderr or proc.stdout or "").strip()[-500:] or "重新解密失败"
    return result


# ------------------------------------------------------------------
# 2) 一次完整同步 = 重新解密 + 重建索引
# ------------------------------------------------------------------

def refresh_and_sync(force: bool = False, redecrypt: bool = True) -> Dict[str, Any]:
    """执行一次完整同步，返回两步各自的结果。"""
    out: Dict[str, Any] = {}
    if redecrypt:
        refresh = refresh_decrypted()
        out["decrypt_step"] = refresh
        if not refresh.get("ok"):
            # 解密失败就不重建索引：在旧快照上重建只会让结果看起来「成功」但内容没变
            out["etl_step"] = {"status": "skipped", "reason": "重新解密失败，未重建索引"}
            out["ok"] = False
            return out
    out["etl_step"] = sync_data(force=force)
    out["ok"] = True
    return out


def _run_once(force: bool = False) -> Dict[str, Any]:
    """跑一次并记录状态（定时器与手动同步共用）"""
    started = time.time()
    result = refresh_and_sync(force=force)
    return record_run(result, started)


# ------------------------------------------------------------------
# 3) 状态持久化
# ------------------------------------------------------------------

def _load_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"写入定时同步状态失败：{exc}")


def record_run(result: Dict[str, Any], started_at: float) -> Dict[str, Any]:
    """把一次同步结果写入 auto_sync.json（独立进程与进程内线程共用）。"""
    record = {
        "at": int(time.time()),
        "duration_seconds": round(time.time() - started_at, 1),
        "ok": bool(result.get("ok")),
        "result": result,
    }
    with _lock:
        state = _load_state()
        state["last_run"] = record
        state["run_count"] = int(state.get("run_count", 0)) + 1
        _save_state(state)
    logger.info(f"同步完成 ok={record['ok']} 耗时 {record['duration_seconds']}s")
    return record


# ------------------------------------------------------------------
# 3b) 独立同步进程的心跳状态
# ------------------------------------------------------------------

def _load_sync_state() -> Optional[Dict[str, Any]]:
    """读独立同步进程写的心跳状态（不存在 / 损坏返回 None）。"""
    try:
        with open(SYNC_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _save_sync_state(payload: Dict[str, Any]) -> None:
    """原子写心跳状态（worker 独占写，其它进程只读）。"""
    try:
        SYNC_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SYNC_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, SYNC_STATE_FILE)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"写入同步心跳状态失败：{exc}")


def _clear_sync_state() -> None:
    try:
        SYNC_STATE_FILE.unlink()
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"清理同步心跳状态失败：{exc}")


def _pid_alive(pid: Any) -> bool:
    """判断进程是否存活（跨平台，不引入第三方依赖）。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:  # noqa: BLE001 - 查询失败时保守认为存活，交给心跳超时兜底
            return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:  # noqa: BLE001
        return True
    return True


def _active_sync() -> Optional[Dict[str, Any]]:
    """返回仍在运行的同步记录（独立进程心跳优先，进程内线程兜底），无则 None。

    副作用：判定为僵死时会顺手清掉心跳文件 —— 否则一次异常退出会让之后每次
    start_sync 都误报 already_running。
    """
    st = _load_sync_state()
    if st:
        try:
            fresh = (time.time() - float(st.get("heartbeat_at") or st.get("started_at") or 0)) <= SYNC_HEARTBEAT_STALE_SECONDS
        except (TypeError, ValueError):
            fresh = False
        alive = _pid_alive(st.get("pid"))
        if fresh and alive:
            return st
        logger.warning(
            f"清理已结束/僵死的同步记录（pid={st.get('pid')} fresh={fresh} alive={alive}）"
        )
        _clear_sync_state()
    with _lock:
        if _sync_thread is not None and _sync_thread.is_alive():
            return {
                "mode": "thread",
                "pid": os.getpid(),
                "started_at": _sync_started_at or time.time(),
            }
    return None


# ------------------------------------------------------------------
# 4) 后台定时器
# ------------------------------------------------------------------

def _clamp_interval(minutes: Any) -> int:
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        m = DEFAULT_INTERVAL_MINUTES
    return max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, m))


def _loop(interval_seconds: int) -> None:
    # Event.wait 可被 stop() 立即打断，避免 stop 后还要等满一个间隔
    while not _stop_event.wait(interval_seconds):
        try:
            _run_once()
        except Exception as exc:  # noqa: BLE001 - 定时任务不能因单次异常而退出
            logger.warning(f"定时同步失败：{exc}")


def _start_worker(interval_minutes: int) -> None:
    global _worker
    _stop_event.clear()
    _worker = threading.Thread(
        target=_loop, args=(interval_minutes * 60,),
        name="wechat-auto-sync", daemon=True,
    )
    _worker.start()


def _stop_worker() -> None:
    global _worker
    _stop_event.set()
    _worker = None


def restore_on_startup() -> None:
    """MCP 服务启动时按持久化状态恢复定时器（未开启则什么都不做）"""
    state = _load_state()
    if not state.get("enabled"):
        return
    interval = _clamp_interval(state.get("interval_minutes"))
    _start_worker(interval)
    logger.info(f"已恢复定时同步：每 {interval} 分钟")


# ------------------------------------------------------------------
# 5) 对外 API（供 MCP 工具调用）
# ------------------------------------------------------------------

def set_auto_sync(enabled: bool, interval_minutes: Optional[int] = None) -> Dict[str, Any]:
    """开启 / 关闭定时同步；开启时按 interval_minutes 定时执行完整同步。"""
    state = _load_state()
    interval = _clamp_interval(
        interval_minutes if interval_minutes is not None
        else state.get("interval_minutes")
    )
    state["enabled"] = bool(enabled)
    state["interval_minutes"] = interval
    _save_state(state)

    _stop_worker()
    if enabled:
        _start_worker(interval)
    return get_auto_sync_status()


def get_auto_sync_status() -> Dict[str, Any]:
    """查询定时同步状态（含上次运行结果）"""
    state = _load_state()
    active = _active_sync() or {}
    started = float(active.get("started_at") or 0)
    return {
        "enabled": bool(state.get("enabled")),
        "running": bool(_worker is not None and _worker.is_alive()),
        "syncing": bool(active),
        # 独立进程与进程内线程都走这条路径：调用方据此区分「正常在跑」与「卡死」
        "sync_mode": active.get("mode"),
        "sync_pid": active.get("pid"),
        "sync_started_at": int(started) if started else None,
        "sync_elapsed_seconds": round(time.time() - started, 1) if started else None,
        "interval_minutes": _clamp_interval(state.get("interval_minutes")),
        "min_interval_minutes": MIN_INTERVAL_MINUTES,
        "max_interval_minutes": MAX_INTERVAL_MINUTES,
        "run_count": int(state.get("run_count", 0)),
        "last_run": state.get("last_run"),
        "state_file": str(STATE_FILE),
        "note": "定时同步在 wechat-mcp 进程内运行，随该进程（Snow App）启停；"
                "开关与间隔已持久化，进程重启后自动恢复。手动同步默认起独立进程"
                "（脱离 MCP 进程树），因此 MCP 被宿主回收也不会中断。",
    }


def sync_now(force: bool = False, redecrypt: bool = True) -> Dict[str, Any]:
    """立即执行一次同步（**阻塞式**，供 sync_wechat_database 工具调用）"""
    started = time.time()
    result = refresh_and_sync(force=force, redecrypt=redecrypt)
    record_run(result, started)
    return result


# ------------------------------------------------------------------
# 6) 后台手动同步（供有硬超时的调用方使用）
# ------------------------------------------------------------------

def _sync_worker(force: bool, redecrypt: bool) -> None:
    """进程内线程版同步（**兜底路径**：独立进程起不来时才用）。

    ⚠️ 短连接宿主（实测 Snow App：每次工具调用 spawn 进程、寿命 < 1 秒）下这个线程
    会随 MCP 进程被杀，同步跑不完 —— 此时必须依赖 `_spawn_detached_worker`。
    """
    global _sync_thread, _sync_started_at
    started = _sync_started_at or time.time()
    try:
        sync_now(force=force, redecrypt=redecrypt)
    except Exception as exc:  # noqa: BLE001 - 后台线程不能把异常抛到线程外
        logger.warning(f"后台同步失败：{exc}")
        # 关键：异常也必须落盘。调用方按 run_count / last_run 判定完成，
        # 不写就永远等不到「跑完」的信号（面板会一直停在「同步中」）。
        try:
            record_run({"ok": False, "error": f"后台同步异常：{exc}"}, started)
        except Exception as save_exc:  # noqa: BLE001 - 记录失败状态本身不能再抛
            logger.warning(f"写入同步失败状态失败：{save_exc}")
    finally:
        with _lock:
            _sync_thread = None
            _sync_started_at = None


def _spawn_detached_worker(force: bool, redecrypt: bool) -> Optional[subprocess.Popen]:
    """起一个**脱离 MCP 进程树**的独立同步进程；环境不支持时返回 None（调用方回退线程）。

    为什么必须脱离：部分宿主（实测 Snow App）对 stdio MCP 采用短连接 —— 每次工具调用
    spawn 一个进程、调用结束即退出（实测进程寿命 < 1 秒）。此时进程内的后台线程会随进程
    被杀，而一次完整同步要 53 秒，**永远跑不完**。Windows 用 CREATE_BREAKAWAY_FROM_JOB
    脱离宿主 job（实测连 taskkill /T 都杀不掉），POSIX 用 start_new_session 另开会话。
    """
    cmd = [sys.executable, "-m", "wechat_mcp.sync_worker"]
    if force:
        cmd.append("--force")
    if not redecrypt:
        cmd.append("--no-redecrypt")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # worker 的 stdout/stderr 落盘：DEVNULL 会把启动期异常完全吞掉，出问题时
    # 只看到「started=true 但状态永不更新」，无法判断是启动失败还是被宿主终止。
    log_path = os.path.join(str(USER_WECHAT_DIR), "sync_worker.log")
    try:
        log_fh = open(log_path, "ab")
    except OSError:
        log_fh = subprocess.DEVNULL
    kwargs: Dict[str, Any] = {
        "cwd": str(USER_WECHAT_DIR.parent),
        "env": env,
        # MCP 走 stdio 传 JSON-RPC：绝不能继承管道，也不能把输出混进协议流
        "stdin": subprocess.DEVNULL,
        "stdout": log_fh,
        "stderr": subprocess.STDOUT,
    }

    def _spawn(flags: int, extra: Optional[Dict[str, Any]] = None) -> subprocess.Popen:
        proc = subprocess.Popen(cmd, creationflags=flags, **kwargs, **(extra or {}))
        # 句柄必须活到子进程启动完成；挂在 proc 上避免被 GC 提前关闭
        proc._log_fh = log_fh  # type: ignore[attr-defined]
        return proc

    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        base = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            return _spawn(base | CREATE_BREAKAWAY_FROM_JOB)
        except OSError as exc:
            logger.warning(f"breakaway 启动失败（{exc}），退回不带 breakaway 的 detached")
            try:
                proc = _spawn(base)
                proc._spawn_breakaway_error = str(exc)  # type: ignore[attr-defined]
                return proc
            except OSError as exc2:
                logger.warning(f"独立同步进程启动失败：{exc2}")
                if log_fh is not subprocess.DEVNULL:
                    log_fh.close()
                return None
    try:
        return _spawn(0, {"start_new_session": True})
    except OSError as exc:
        logger.warning(f"独立同步进程启动失败：{exc}")
        if log_fh is not subprocess.DEVNULL:
            log_fh.close()
        return None


def start_sync(force: bool = False, redecrypt: bool = True) -> Dict[str, Any]:
    """立即返回，把完整同步交给**独立进程**（起不来时回退进程内线程）。

    适用于调用方有硬超时的场景（宿主对单次 MCP 调用限制 120s，而「重新解密 + 重建索引」
    在数据量大时会超过该上限）。进度与结果用 ``get_auto_sync_status`` 的 ``syncing`` /
    ``last_run`` 查询 —— 独立进程把心跳与结果落盘，因此即使 MCP 进程被宿主回收，
    同步也会继续跑完（这是与「进程内线程」的关键区别）。
    """
    global _sync_thread, _sync_started_at
    active = _active_sync()
    if active:
        elapsed = time.time() - float(active.get("started_at") or time.time())
        # 僵尸自愈：超过阈值仍未结束就视为卡死，允许重新发起（否则一次卡死会让之后
        # 每次调用都返回 already_running，面板永远停在「同步中」）
        if elapsed <= SYNC_STALE_SECONDS:
            return {
                "started": False,
                "already_running": True,
                "elapsed_seconds": round(elapsed, 1),
                "mode": active.get("mode"),
                "note": "已有同步在运行，请用 get_auto_sync_status 查看进度。",
            }
        logger.warning(f"检测到僵死的同步（已运行 {elapsed:.0f}s），强制重新发起")
        _clear_sync_state()
        with _lock:
            _sync_thread = None
            _sync_started_at = None

    proc = _spawn_detached_worker(force, redecrypt)
    if proc is not None:
        # Popen 成功 ≠ 同步在跑。部分宿主（实测 Snow App 的短连接）会在 MCP 进程退出时
        # 终止其子进程；此时状态文件永不更新，调用方只能干等超时。这里等 worker 写出心跳，
        # 并用**心跳里的 pid** 校验存活 —— 它可能不是 Popen 的 pid（uv 的 python.exe 是启动器，
        # 真实 worker 是它的子进程）。失败则清理残留并明确返回 started=false，让调用方回退阻塞同步。
        deadline = time.time() + WORKER_STARTUP_GRACE_SECONDS
        while time.time() < deadline:
            time.sleep(0.3)
            st = _load_sync_state()
            pid = (st or {}).get("pid")
            if isinstance(pid, int) and _pid_alive(pid):
                return {
                    "started": True,
                    "mode": "detached",
                    "pid": pid,
                    "note": "同步已在独立进程开始（重新解密 + 重建索引），用 get_auto_sync_status 查看结果。",
                }
        tail = ""
        try:
            with open(os.path.join(str(USER_WECHAT_DIR), "sync_worker.log"), "rb") as f:
                tail = f.read().decode("utf-8", "replace")[-1200:]
        except OSError:
            pass
        _clear_sync_state()
        return {
            "started": False,
            "mode": "detached",
            "error": "独立同步进程未能存活（宿主可能在 MCP 退出时终止了子进程）",
            "hint": "请改用阻塞同步 sync_wechat_database（本机实测约 72s）。",
            "exit_code": proc.poll(),
            "log_tail": tail,
        }

    # 回退：进程内后台线程（长连接宿主，或 spawn 受限的环境）
    with _lock:
        _sync_started_at = time.time()
        _sync_thread = threading.Thread(
            target=_sync_worker, args=(force, redecrypt),
            name="wechat-manual-sync", daemon=True,
        )
        _sync_thread.start()
    return {
        "started": True,
        "mode": "thread",
        "note": "独立进程不可用，已改用进程内后台线程；若宿主是短连接（每次调用重启进程），"
                "同步可能随进程退出而中断，建议改用 sync_wechat_database 阻塞同步。",
    }
