"""
WeChat image resolver and decryptor.
Supports:
1. Legacy XOR format (auto-calculates XOR key from JPEG magic header FF D8).
2. WeChat 4.x V2 format (AES-128-ECB PKCS7 + unencrypted raw + XOR tail) using AES key.
"""

import os
import glob
import re
import struct
import base64
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

from .config import BASE_DIR, USER_WECHAT_DIR, get_decrypted_dir

_V2_MAGIC_FULL = b"\x07\x08V2\x08\x07"
_V1_MAGIC_FULL = b"\x07\x08V1\x08\x07"

def _find_image_dat_by_md5(md5: str, search_roots: list[Path]) -> Optional[Path]:
    """Search for image .dat file matching md5 under WeChat storage directories."""
    for root in search_roots:
        if not root.exists():
            continue
        attach_dir = root / "msg" / "attach"
        if attach_dir.exists():
            matches = list(attach_dir.glob(f"**/*{md5}*"))
            if matches:
                # Prioritize non-thumbnail if available, else thumbnail
                full_imgs = [m for m in matches if not m.name.endswith(("_t.dat", "_t_W.dat"))]
                return full_imgs[0] if full_imgs else matches[0]
        # Fallback broader search
        broad = list(root.glob(f"**/*{md5}*.dat"))
        if broad:
            return broad[0]
    return None

# ---------------------------------------------------------------------------
# V2 格式的 XOR 段使用**全局单字节**密钥。
#
# ⚠️ 原先按「每个文件」用 tail[-1] ^ 0xD9 推导 —— 这隐含假定明文以 JPEG 的
#    FF D9 (EOI) 结尾。对 JPEG 成立，但对 PNG/GIF/WebP 等（结尾是 IEND 等）
#    推出来的密钥是错的，会让整个 XOR 段（图片的绝大部分）变成乱码。
#    正确做法：从若干 JPEG 缩略图尾部投票推导一次，全局复用。
# ---------------------------------------------------------------------------
_XOR_KEY_CACHE: Dict[str, Optional[int]] = {}


def _detect_xor_key(img_dir: Path, aes_key: bytes) -> Optional[int]:
    """从同目录的 JPEG 缩略图（明文尾必为 FF D9）投票推导全局 XOR 密钥。"""
    cache_key = str(img_dir)
    if cache_key in _XOR_KEY_CACHE:
        return _XOR_KEY_CACHE[cache_key]

    from Crypto.Cipher import AES as _AES

    votes: Dict[int, int] = {}
    try:
        thumbs = sorted(img_dir.glob("*_t.dat"))[:48]
    except OSError:
        thumbs = []
    for p in thumbs:
        try:
            with open(p, "rb") as fp:
                data = fp.read()
        except OSError:
            continue
        if len(data) < 32 or data[:6] != _V2_MAGIC_FULL:
            continue
        _aes_size, xor_size = struct.unpack("<II", data[6:14])
        if xor_size <= 0:
            continue
        try:
            head = _AES.new(aes_key[:16], _AES.MODE_ECB).decrypt(data[15:31])
        except Exception:
            break
        if head[:3] != b"\xff\xd8\xff":
            continue
        k = data[-1] ^ 0xD9
        votes[k] = votes.get(k, 0) + 1

    result = max(votes, key=votes.get) if votes else None
    _XOR_KEY_CACHE[cache_key] = result
    return result


def _find_thumbnail_sibling(dat_path: Path) -> Optional[Path]:
    """同目录下的 <md5>_t.dat 缩略图（解出来是可直接渲染的 JPEG）。"""
    base = dat_path.name[:-4] if dat_path.name.endswith(".dat") else dat_path.name
    for suffix in ("_t_W", "_t", "_h"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    for name in (f"{base}_t.dat", f"{base}_t_W.dat"):
        cand = dat_path.parent / name
        if cand.exists():
            return cand
    return None


def decrypt_image_dat(dat_path: Path, aes_key: Optional[bytes] = None) -> Tuple[Optional[bytes], str, Optional[str]]:
    """
    Decrypt a WeChat .dat image file.
    Returns: (decrypted_bytes, format_ext, error_message)
    """
    if not dat_path.exists():
        return None, "", f"Image dat file not found: {dat_path}"

    with open(dat_path, "rb") as fp:
        data = fp.read()

    if len(data) < 16:
        return None, "", "File too small or corrupted"

    head6 = data[:6]

    # Case 1: V2 / V1 Format (AES-128-ECB + XOR)
    if head6 in (_V2_MAGIC_FULL, _V1_MAGIC_FULL):
        if not aes_key:
            return None, "", "WeChat 4.x V2 image encryption requires image_aes_key (not currently loaded)"
        
        try:
            from Crypto.Cipher import AES
        except ImportError:
            return None, "", "pycryptodome not installed, required for AES decryption"

        if len(data) < 15:
            return None, "", "Invalid V2 dat header"

        aes_size, xor_size = struct.unpack("<II", data[6:14])
        # PKCS7 aligned size
        aligned_aes_size = aes_size + (16 - aes_size % 16) if aes_size % 16 != 0 else aes_size + 16
        
        cipher_part = data[15 : 15 + aligned_aes_size]
        if len(cipher_part) < aligned_aes_size:
            return None, "", "Corrupted V2 dat file (truncated AES payload)"

        try:
            cipher = AES.new(aes_key[:16], AES.MODE_ECB)
            dec_aes = cipher.decrypt(cipher_part)
            # Remove PKCS7 padding
            pad_len = dec_aes[-1]
            if 1 <= pad_len <= 16:
                dec_aes = dec_aes[:-pad_len]
        except Exception as e:
            return None, "", f"AES decryption failed: {e}"

        # Tail XOR
        raw_mid = data[15 + aligned_aes_size : len(data) - xor_size]
        tail_xor = data[len(data) - xor_size :]
        
        if tail_xor:
            # XOR 密钥是**全局单字节**：优先从同目录 JPEG 缩略图投票推导
            # （对 PNG/GIF/WebP 也正确）；推不出来才退回老行为
            # （按本文件尾部 FF D9 推导，只对 JPEG 正确）。
            xor_key = None
            if aes_key:
                xor_key = _detect_xor_key(dat_path.parent, aes_key)
            if xor_key is None:
                xor_key = tail_xor[-1] ^ 0xD9
            dec_tail = bytes([b ^ xor_key for b in tail_xor])
        else:
            dec_tail = b""

        full_img = dec_aes + raw_mid + dec_tail
        if full_img.startswith(b"\xff\xd8\xff"):
            fmt = "jpg"
        elif full_img.startswith(b"\x89PNG"):
            fmt = "png"
        elif full_img.startswith(b"GIF8"):
            fmt = "gif"
        elif full_img.startswith(b"RIFF") and full_img[8:12] == b"WEBP":
            fmt = "webp"
        elif full_img.startswith(b"wxgf"):
            fmt = "wxgf"
        else:
            fmt = "bin"
        return full_img, fmt, None

    # Case 2: Legacy XOR format (Auto-detect XOR key)
    # JPEG magic: FF D8 FF
    k1 = data[0] ^ 0xFF
    k2 = data[1] ^ 0xD8
    if k1 == k2:
        xor_key = k1
        full_img = bytes([b ^ xor_key for b in data])
        return full_img, "jpg", None

    # PNG magic: 89 50 4E 47
    k1 = data[0] ^ 0x89
    k2 = data[1] ^ 0x50
    if k1 == k2:
        xor_key = k1
        full_img = bytes([b ^ xor_key for b in data])
        return full_img, "png", None

    # GIF magic: 47 49 46 38 ('GIF8')
    k1 = data[0] ^ 0x47
    k2 = data[1] ^ 0x49
    if k1 == k2:
        xor_key = k1
        full_img = bytes([b ^ xor_key for b in data])
        return full_img, "gif", None

    return None, "", "Unknown image dat encryption format"


def get_image_for_message(message_id: int, include_base64: bool = False) -> Dict[str, Any]:
    """
    Resolve and decrypt an image for a specific message ID.
    Returns dictionary with image path, metadata, and optional base64 content.
    """
    import sqlite3
    from .database import get_connection

    conn = get_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if not row:
        conn.close()
        return {"error": f"Message id {message_id} not found"}

    if row["local_type"] != 3 and not (row["content"] and "[图片]" in row["content"]):
        conn.close()
        return {"error": f"Message id {message_id} is not an image message (local_type={row['local_type']})"}

    source_file = row["source_file"]
    local_id = row["local_id"]
    create_time = row["create_time"]
    decrypted_dir = get_decrypted_dir()
    
    # 1. Try to find image MD5
    target_md5 = None
    
    # Attempt A: Check message_resource.db
    res_db = decrypted_dir / "message" / "message_resource.db"
    if res_db.exists():
        try:
            r_conn = sqlite3.connect(res_db)
            r_cur = r_conn.cursor()
            res_row = r_cur.execute(
                "SELECT packed_info FROM MessageResourceInfo WHERE message_create_time = ? AND message_local_id = ?",
                (create_time, local_id)
            ).fetchone()
            if res_row and res_row[0]:
                m = re.search(rb"[0-9a-fA-F]{32}", res_row[0])
                if m:
                    target_md5 = m.group(0).decode("ascii").lower()
            r_conn.close()
        except Exception:
            pass

    # Attempt B: Check specific Msg table if target_md5 still not found
    if not target_md5 and source_file:
        src_db = decrypted_dir / "message" / source_file
        if src_db.exists():
            try:
                s_conn = sqlite3.connect(src_db)
                s_cur = s_conn.cursor()
                # Find matching table
                tables = [r[0] for r in s_cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() if r[0].startswith("Msg_")]
                for tbl in tables:
                    m_row = s_cur.execute(f"SELECT packed_info_data FROM {tbl} WHERE local_id = ?", (local_id,)).fetchone()
                    if m_row and m_row[0]:
                        m = re.search(rb"[0-9a-fA-F]{32}", m_row[0])
                        if m:
                            target_md5 = m.group(0).decode("ascii").lower()
                            break
                s_conn.close()
            except Exception:
                pass

    conn.close()

    if not target_md5:
        return {"error": f"Could not locate image metadata or MD5 for message {message_id}"}

    # 2. Search physical dat file
    # Look into possible WeChat root paths
    candidates = [
        USER_WECHAT_DIR,
        Path.home() / "Documents" / "WeChat Files",
        Path(os.environ.get("APPDATA", "")) / "Tencent" / "WeChat",
    ]
    # Check if we can auto-detect from config ini
    try:
        from .tools.key_tool import auto_detect_db_dir
        db_dir = auto_detect_db_dir()
        if db_dir:
            # db_dir is like .../xwechat_files/<user_hash>/db_storage
            # parent is xwechat_files/<user_hash>
            user_root = Path(db_dir).parent
            candidates.insert(0, user_root)
            candidates.insert(1, user_root.parent)
    except Exception:
        pass

    all_keys_path = USER_WECHAT_DIR / "all_keys.json"
    if all_keys_path.exists():
        try:
            import json
            with open(all_keys_path, "r", encoding="utf-8") as f:
                k_data = json.load(f)
                if "wechat_dir" in k_data and k_data["wechat_dir"]:
                    candidates.insert(0, Path(k_data["wechat_dir"]))
        except Exception:
            pass

    dat_file = _find_image_dat_by_md5(target_md5, candidates)
    if not dat_file:
        return {
            "error": f"Image file for MD5 {target_md5} not found in local WeChat storage. It may have been cleaned by WeChat or not downloaded yet.",
            "md5": target_md5
        }

    # 3. Decrypt
    # Try load image_aes_key if available
    image_aes_key = None
    if all_keys_path.exists():
        try:
            import json
            with open(all_keys_path, "r", encoding="utf-8") as f:
                k_data = json.load(f)
                if "image_aes_key" in k_data and k_data["image_aes_key"]:
                    image_aes_key = k_data["image_aes_key"].encode("ascii")
        except Exception:
            pass

    img_bytes, fmt, err = decrypt_image_dat(dat_file, aes_key=image_aes_key)
    if err:
        return {
            "error": err,
            "md5": target_md5,
            "dat_path": str(dat_file),
            "hint": "If this is a WeChat 4.x V2 image, please open 1-2 images in WeChat client to load the key into memory, then run image key extraction."
        }

    # 3.5 原图若是 wxgf 私有容器（内含 HEVC，需微信原生解码器才能出图，宿主无法直接渲染），
    #     回退到同目录的 <md5>_t.dat 缩略图 —— 它是可直接渲染的 JPEG。
    if img_bytes[:4] == b"wxgf":
        thumb = _find_thumbnail_sibling(dat_file)
        if thumb is not None:
            t_bytes, t_fmt, t_err = decrypt_image_dat(thumb, aes_key=image_aes_key)
            if not t_err and t_bytes[:3] == b"\xff\xd8\xff":
                img_bytes, fmt = t_bytes, (t_fmt or "jpg")

    # 4. Save decrypted image to cache
    output_dir = USER_WECHAT_DIR / "images"
    output_dir.mkdir(parents=True, exist_ok=True)
    out_file = output_dir / f"{message_id}_{target_md5}.{fmt}"
    with open(out_file, "wb") as fp:
        fp.write(img_bytes)

    res = {
        "success": True,
        "message_id": message_id,
        "md5": target_md5,
        "format": fmt,
        "size_bytes": len(img_bytes),
        "file_path": str(out_file),
    }

    if include_base64:
        res["base64"] = base64.b64encode(img_bytes).decode("ascii")

    return res

