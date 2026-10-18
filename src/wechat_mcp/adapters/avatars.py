"""绿泡泡 4.x 头像索引（``head_image.db`` → ``avatars``）

此前 ``head_image.db`` 与其他库一样"已解密但从未被使用"。它是一张极简表：

    CREATE TABLE head_image(
        username TEXT PRIMARY KEY,   -- wxid_* / *@chatroom / *@openim
        md5 TEXT,
        image_buffer BLOB,           -- 头像原图字节（实测 JPEG 3247 / PNG 37）
        update_time INTEGER
    )

实测（本机 3284 条）：``image_buffer`` 头为 ``FF D8 FF E0 … JFIF``，即**可直接
渲染的图片**，无需任何解密，体积 91 B – 45 KB（均值约 4.9 KB）。

本模块把它落盘成真实图片文件，中间库只存「username → 文件路径」，与
``images.py`` 的 ``get_message_image`` 保持同一套契约：**返回 file_path，
由宿主读盘渲染，不走 base64**（避免把图片塞进大模型上下文）。

落盘命名用 ``md5``（同一张头像常被多个 username 共用，天然去重）；``md5``
缺失时退化为内容 sha1。同名文件已存在且大小一致则跳过写入。
"""

import hashlib
import logging
import sqlite3
from pathlib import Path

from ..config import USER_WECHAT_DIR

logger = logging.getLogger(__name__)

# 头像落盘目录（与 images/ 同级，便于统一清理）
AVATAR_DIR_NAME = "avatars"

# 魔数 → 扩展名。顺序即优先级，全部按实际字节判定，不信数据库里的任何提示。
_MAGIC = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"RIFF", "webp"),  # RIFF....WEBP，前缀足够区分
)


def detect_image_format(buf: bytes) -> str:
    """按魔数判定图片格式，未知返回空串（调用方据此丢弃脏数据）"""
    for magic, ext in _MAGIC:
        if buf.startswith(magic):
            return ext
    return ""


def get_avatar_dir() -> Path:
    return USER_WECHAT_DIR / AVATAR_DIR_NAME


def sync_avatars_v4(decrypted_dir: Path, cur: sqlite3.Cursor) -> None:
    """从 head_image.db 建立头像索引并把图片落盘到 ~/.wechat_mcp/avatars/"""
    head_db = decrypted_dir / "head_image" / "head_image.db"
    if not head_db.exists():
        return

    avatar_dir = get_avatar_dir()
    avatar_dir.mkdir(parents=True, exist_ok=True)

    conn = None
    try:
        conn = sqlite3.connect(f"file:{head_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "head_image" not in tables:
            logger.warning("head_image.db 中未找到 head_image 表，跳过头像索引")
            return

        # 列名按 PRAGMA 探测：不同版本列名可能有差异，硬编码缺列会让整表读取失败
        cols = [r[1] for r in conn.execute("PRAGMA table_info(head_image)").fetchall()]
        if "username" not in cols or "image_buffer" not in cols:
            logger.warning(f"head_image 缺少 username/image_buffer 列，跳过（实际列: {cols}）")
            return

        def _col(name: str) -> str:
            return name if name in cols else f"NULL AS {name}"

        select_cols = ", ".join(["username", _col("md5"), "image_buffer", _col("update_time")])

        written = 0
        indexed = 0
        skipped = 0
        for row in conn.execute(f"SELECT {select_cols} FROM head_image"):
            username = row["username"]
            buf = row["image_buffer"]
            if not username or not buf:
                continue
            if not isinstance(buf, (bytes, bytearray)):
                skipped += 1
                continue
            buf = bytes(buf)

            fmt = detect_image_format(buf)
            if not fmt:
                # 不是可识别的图片（占位数据 / 新版容器），丢弃而不是写出坏文件
                skipped += 1
                continue

            stem = row["md5"] or hashlib.sha1(buf).hexdigest()
            out_file = avatar_dir / f"{stem}.{fmt}"
            # 同一 md5 会被多个 username 共用；已存在且大小一致就不重复写盘
            if not (out_file.exists() and out_file.stat().st_size == len(buf)):
                try:
                    out_file.write_bytes(buf)
                    written += 1
                except OSError as exc:
                    logger.warning(f"写入头像失败 {out_file}: {exc}")
                    continue

            cur.execute(
                """
                INSERT INTO avatars
                (username, md5, format, size_bytes, file_path, update_time)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    md5         = excluded.md5,
                    format      = excluded.format,
                    size_bytes  = excluded.size_bytes,
                    file_path   = excluded.file_path,
                    update_time = excluded.update_time
                """,
                (username, row["md5"] or "", fmt, len(buf), str(out_file), row["update_time"]),
            )
            indexed += 1

        logger.info(f"Indexed {indexed} avatars ({written} files written, {skipped} skipped)")
    except Exception as exc:
        # 头像索引失败不应中断主同步（调用方另有一层 try，这里只负责记录）
        logger.warning(f"Error building avatar index: {exc}")
    finally:
        # 异常路径同样要释放只读句柄，避免长期运行下句柄泄漏
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
