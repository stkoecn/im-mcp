"""绿泡泡数据库版本适配与路由器"""

from pathlib import Path
import sqlite3
import logging

from .v3 import is_v3_layout, sync_v3
from .v4 import is_v4_layout, sync_v4

logger = logging.getLogger(__name__)


def detect_version(decrypted_dir: Path) -> str:
    """自动探测解密数据库的绿泡泡版本 ('v4', 'v3', 或 'unknown')"""
    if is_v4_layout(decrypted_dir):
        return "v4"
    if is_v3_layout(decrypted_dir):
        return "v3"
    return "unknown"


def route_sync(decrypted_dir: Path, cur: sqlite3.Cursor, version_hint: str = "auto",
               force: bool = False) -> str:
    """按版本路由执行同步逻辑"""
    version = version_hint
    if version == "auto":
        version = detect_version(decrypted_dir)

    logger.info(f"Routing database sync using adapter: {version}")

    if version == "v4":
        sync_v4(decrypted_dir, cur, force=force)
        return "v4"
    elif version == "v3":
        sync_v3(decrypted_dir, cur)
        return "v3"
    else:
        # 宽容处理：依次尝试 v4 和 v3
        logger.warning(f"Unable to accurately determine layout for {decrypted_dir}, attempting v4 then v3")
        sync_v4(decrypted_dir, cur, force=force)
        sync_v3(decrypted_dir, cur)
        return "fallback"
