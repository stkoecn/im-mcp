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
_lock = threading.RLock()


# ------------------------------------------------------------------
# 1) 重新解密（子进程，静默）
# ------------------------------------------------------------------

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

    if not db_dir:
        try:
            from .tools.key_tool import auto_detect_db_dir
            db_dir = auto_detect_db_dir()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"自动检测 db_storage 失败：{exc}"}
    if not db_dir or not os.path.isdir(db_dir):
        return {"ok": False, "error": "未找到绿泡泡 db_storage 目录。"}

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
    record = {
        "at": int(time.time()),
        "duration_seconds": round(time.time() - started, 1),
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
    with _lock:
        last = state.get("last_run")
    return {
        "enabled": bool(state.get("enabled")),
        "running": bool(_worker is not None and _worker.is_alive()),
        "interval_minutes": _clamp_interval(state.get("interval_minutes")),
        "min_interval_minutes": MIN_INTERVAL_MINUTES,
        "max_interval_minutes": MAX_INTERVAL_MINUTES,
        "run_count": int(state.get("run_count", 0)),
        "last_run": last,
        "state_file": str(STATE_FILE),
        "note": "定时同步在 wechat-mcp 进程内运行，随该进程（Snow App）启停；"
                "开关与间隔已持久化，进程重启后自动恢复。",
    }


def sync_now(force: bool = False, redecrypt: bool = True) -> Dict[str, Any]:
    """立即执行一次同步（供 sync_wechat_database 工具调用）"""
    started = time.time()
    result = refresh_and_sync(force=force, redecrypt=redecrypt)
    with _lock:
        state = _load_state()
        record = {
            "at": int(time.time()),
            "duration_seconds": round(time.time() - started, 1),
            "ok": bool(result.get("ok")),
            "result": result,
        }
        state["last_run"] = record
        state["run_count"] = int(state.get("run_count", 0)) + 1
        _save_state(state)
    return result
