import sqlite3
import re
import hashlib
import threading
from pathlib import Path
import logging

from .config import DB_PATH, get_decrypted_dir
from .adapters import route_sync
from .adapters.avatars import sync_avatars_v4
from .adapters.media import sync_emoticons_v4, sync_favorites_v4, sync_media_v4

logger = logging.getLogger(__name__)

# 线程本地连接登记：工具函数退出时统一释放，保证异常路径也不会泄漏连接
_conn_tls = threading.local()


def release_connections() -> None:
    """关闭当前线程已打开的全部数据库连接（可重复调用，close 幂等）"""
    for conn in getattr(_conn_tls, "conns", []):
        try:
            conn.close()
        except Exception:
            pass
    _conn_tls.conns = []


def get_connection():
    from .config import DB_PATH
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if not hasattr(_conn_tls, "conns"):
        _conn_tls.conns = []
    _conn_tls.conns.append(conn)
    return conn

def init_schema():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS sessions (
            username TEXT PRIMARY KEY,
            type TEXT,
            display_name TEXT,
            message_count INTEGER DEFAULT 0,
            first_time INTEGER,
            last_time INTEGER,
            last_msg_preview TEXT
        );

        CREATE TABLE IF NOT EXISTS contacts (
            username TEXT PRIMARY KEY,
            alias TEXT,
            display_name TEXT,
            type INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS chatroom_members (
            chatroom_username TEXT,
            member_username TEXT,
            display_name TEXT,
            PRIMARY KEY (chatroom_username, member_username)
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT,
            local_id INTEGER,
            session_username TEXT,
            sender_username TEXT,
            create_time INTEGER,
            local_type INTEGER,
            content TEXT,
            UNIQUE(source_file, session_username, local_id)
        );

        CREATE INDEX IF NOT EXISTS idx_messages_session_time ON messages(session_username, create_time);
        CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(create_time);

        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            content,
            content='messages',
            content_rowid='id',
            tokenize='unicode61'
        );

        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
        END;

        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
        END;

        CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
            INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
        END;

        CREATE TABLE IF NOT EXISTS media_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            md5 TEXT NOT NULL,
            kind TEXT NOT NULL,
            file_name TEXT,
            file_size INTEGER,
            modify_time INTEGER,
            chat_hash TEXT,
            month TEXT,
            rel_path TEXT,
            abs_path TEXT,
            -- 同一 md5 可能出现在多个会话目录下，主键只取 md5 会互相覆盖丢路径
            UNIQUE(md5, rel_path)
        );

        CREATE INDEX IF NOT EXISTS idx_media_md5 ON media_files(md5);
        CREATE INDEX IF NOT EXISTS idx_media_kind ON media_files(kind);
        CREATE INDEX IF NOT EXISTS idx_media_month ON media_files(month);

        CREATE TABLE IF NOT EXISTS favorites (
            local_id INTEGER PRIMARY KEY,
            type INTEGER,
            update_time INTEGER,
            title TEXT,
            description TEXT,
            from_user TEXT,
            raw_content TEXT
        );

        CREATE TABLE IF NOT EXISTS emoticons (
            md5 TEXT PRIMARY KEY,
            caption TEXT,
            package_id TEXT,
            package_name TEXT,
            kind TEXT,
            thumb_url TEXT,
            cdn_url TEXT,
            designer_id TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_emoticons_package ON emoticons(package_id);

        CREATE TABLE IF NOT EXISTS avatars (
            username TEXT PRIMARY KEY,
            md5 TEXT,
            format TEXT,
            size_bytes INTEGER,
            file_path TEXT,
            update_time INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_avatars_md5 ON avatars(md5);
    """)
    conn.commit()
    conn.close()

# ETL 解析/中间库结构版本号。解析逻辑或表结构变化时必须递增，
# 否则老用户升级后源库文件未变 -> 指纹一致 -> 直接 skipped，新逻辑永不生效。
ETL_SCHEMA_VERSION = "5"

# 派生表（media_files / favorites / emoticons / avatars）结构版本。这些表每次
# ETL 都会从源库整体重建，因此结构升级时直接 DROP 重建最安全，不会丢用户数据。
DERIVED_SCHEMA_VERSION = "2"


def _migrate_derived_tables(conn) -> None:
    """派生表结构升级：DROP 后由随后的 ETL 重建"""
    cur = conn.cursor()
    cur.execute("SELECT value FROM meta WHERE key='derived_schema'")
    row = cur.fetchone()
    if row and row[0] == DERIVED_SCHEMA_VERSION:
        return
    for table in ("media_files", "favorites", "emoticons", "avatars"):
        cur.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()
    init_schema()
    cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('derived_schema', ?)",
                (DERIVED_SCHEMA_VERSION,))
    conn.commit()
    logger.info(f"派生表结构已升级到 {DERIVED_SCHEMA_VERSION}，等待本次 ETL 重建")


def compute_fingerprint(decrypted_dir: Path) -> str:
    """按 ETL 版本 + 派生表版本 + 源库文件修改时间与大小计算指纹

    派生表版本必须计入：``_migrate_derived_tables`` 会在版本变化时 DROP 派生表，
    若指纹不变则 sync_data 会直接 skipped，派生表被删掉后再也不会重建。
    """
    items = [f"etl:{ETL_SCHEMA_VERSION}", f"derived:{DERIVED_SCHEMA_VERSION}"]
    for p in sorted(decrypted_dir.rglob("*.db")):
        try:
            stat = p.stat()
            items.append(f"{p.name}:{stat.st_size}:{stat.st_mtime_ns}")
        except Exception:
            pass
    return hashlib.md5("\n".join(items).encode()).hexdigest()

def _collect_stats(cur: sqlite3.Cursor) -> dict:
    """统计中间库当前的数据量（供 CLI 与 MCP 工具返回真实数量）"""
    def count(table: str) -> int:
        try:
            return cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            return 0

    return {
        "contacts": count("contacts"),
        "sessions": count("sessions"),
        "messages": count("messages"),
        "chatroom_members": count("chatroom_members"),
        "media_files": count("media_files"),
        "favorites": count("favorites"),
        "emoticons": count("emoticons"),
        "avatars": count("avatars"),
    }


def sync_data(force: bool = False) -> dict:
    """将解密后的 sqlite 数据同步到统一中间库"""
    decrypted_dir = get_decrypted_dir()
    if not decrypted_dir.exists():
        return {"status": "error", "message": f"Decrypted dir {decrypted_dir} not found"}

    init_schema()
    conn = get_connection()
    _migrate_derived_tables(conn)
    cur = conn.cursor()

    fp = compute_fingerprint(decrypted_dir)
    cur.execute("SELECT value FROM meta WHERE key='fingerprint'")
    row = cur.fetchone()
    if not force and row and row[0] == fp:
        stats = _collect_stats(cur)
        conn.close()
        return {"status": "skipped", "message": "No new database changes", **stats}

    # 按版本路由执行解密数据库同步 (v3 / v4 / 扩展版本)
    adapter_used = route_sync(decrypted_dir, cur)

    # 媒体文件索引与收藏条目（绿泡泡 4.x 的独立数据库）。
    # 任一附加库解析失败都不应中断主流程，但要如实记录到返回值里。
    warnings: list = []
    if adapter_used in ("v4", "fallback"):
        for name, fn in (("media", sync_media_v4), ("favorites", sync_favorites_v4),
                         ("emoticons", sync_emoticons_v4), ("avatars", sync_avatars_v4)):
            try:
                fn(decrypted_dir, cur)
            except Exception as exc:  # noqa: BLE001 - 单个附加库失败不影响主同步
                warnings.append(f"{name}: {exc}")
                logger.warning(f"Sync {name} failed: {exc}")

    # 更新会话统计与 FTS 索引
    # 顺带补齐会话的首条消息时间：v3/v4 的会话表都不提供该字段，
    # 此前 sessions.first_time 恒为 NULL（写了但从不填）
    cur.execute("""
        UPDATE sessions SET
            message_count = (
                SELECT count(*) FROM messages WHERE messages.session_username = sessions.username
            ),
            first_time = (
                SELECT min(create_time) FROM messages WHERE messages.session_username = sessions.username
            )
    """)

    # 重建 FTS 索引：历史版本用 INSERT OR REPLACE 写入，旧词条会残留
    # （recursive_triggers 默认关闭，AFTER DELETE 触发器不触发）。
    # 现在 UPSERT + messages_au 触发器已能增量保持一致，故只在 ETL 版本变化后
    # 做一次全量 rebuild 自愈，避免每次 ETL 都白跑一遍 25 万行。
    try:
        cur.execute("SELECT value FROM meta WHERE key='fts_rebuilt_version'")
        fts_row = cur.fetchone()
        if not fts_row or fts_row[0] != ETL_SCHEMA_VERSION:
            cur.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('fts_rebuilt_version', ?)",
                        (ETL_SCHEMA_VERSION,))
    except sqlite3.Error as exc:
        warnings.append(f"fts rebuild: {exc}")
        logger.warning(f"FTS rebuild failed: {exc}")

    cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('fingerprint', ?)", (fp,))
    conn.commit()
    stats = _collect_stats(cur)
    conn.close()
    result = {
        "status": "success",
        "message": f"Database synchronized successfully (adapter: {adapter_used})",
        "adapter": adapter_used,
        **stats,
    }
    if warnings:
        result["warnings"] = warnings
    return result
