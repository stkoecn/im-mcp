#!/usr/bin/env python3
"""wcdb-key-tool (Windows) — 绿泡泡数据库密钥提取工具

Windows 绿泡泡数据库密钥提取工具。完整支持仍在进程内存里缓存明文 raw key
的绿泡泡版本（4.0.x 一代）。绿泡泡 4.1+ 以后，主路径改为只读运行时
`Config.Cipher` 扫描 + HMAC 校验；`capture-experimental` 仅保留为研究性备用
路线，不是主路径。

Usage:
    python3 wcdb_key_tool_windows.py extract              # 提取密钥（优先 runtime 扫描，老版本回退内存扫描）
    python3 wcdb_key_tool_windows.py capture-experimental  # [研究性备用] 断点抓 passphrase
    python3 wcdb_key_tool_windows.py set-passphrase <64位hex>  # 手动填入已获取的 passphrase
    python3 wcdb_key_tool_windows.py decrypt               # 解密数据库
    python3 wcdb_key_tool_windows.py extract --decrypt     # 提取 + 解密一步完成

Requirements:
    - Python 3.10+（Windows 自带 bcrypt.dll，无需第三方加密库）
    - 建议以管理员身份运行（读取其他进程内存需要足够权限）
    - capture-experimental 需要 cdb.exe（"Debugging Tools for Windows"，
      Windows SDK 的可选组件，或随 WinDbg 安装）

Known Gap:
    研究性断点路线仍保留，但默认不会影响 extract；主路径是只读运行时扫描，
    只在读到的候选值通过 HMAC 校验后才保存。

https://github.com/TANGandXUE/wcdb-key-tool
"""
from __future__ import annotations

import argparse
import ctypes
import glob
import hashlib
import hmac as hmac_mod
import json
import logging
import os
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

_print = lambda *a, **kw: print(*a, flush=True, **kw)  # noqa: E731

# ============================================================
# Constants
# ============================================================
PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
IV_SZ = 16
HMAC_SZ = 64
RESERVE_SZ = 80  # IV(16) + HMAC(64)
SQLITE_HDR = b"SQLite format 3\x00"

PASSPHRASE_FILE = os.path.join(os.path.expanduser("~"), ".wcdb-key-tool", "wechat-passphrase.json")


# ============================================================
# AES-CBC via OS Native Cryptography (Zero 3rd-party dependencies)
# ============================================================

if sys.platform == "win32":
    import ctypes.wintypes as wt

    _bcrypt = ctypes.WinDLL("bcrypt")
    _bcrypt.BCryptOpenAlgorithmProvider.argtypes = [ctypes.POINTER(wt.HANDLE), ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
    _bcrypt.BCryptSetProperty.argtypes = [wt.HANDLE, ctypes.c_wchar_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_ulong]
    _bcrypt.BCryptGenerateSymmetricKey.argtypes = [
        wt.HANDLE, ctypes.POINTER(wt.HANDLE), ctypes.c_char_p, ctypes.c_ulong,
        ctypes.c_char_p, ctypes.c_ulong, ctypes.c_ulong,
    ]
    _bcrypt.BCryptDecrypt.argtypes = [
        wt.HANDLE, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_void_p,
        ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong), ctypes.c_ulong,
    ]

    def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
        """AES-256-CBC 解密（无 padding），走 Windows CNG 的 bcrypt.dll。"""
        h_alg = wt.HANDLE()
        status = _bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), "AES", None, 0)
        if status != 0:
            raise RuntimeError(f"BCryptOpenAlgorithmProvider failed: {status:#x}")
        try:
            mode = ("ChainingModeCBC\x00").encode("utf-16-le")
            status = _bcrypt.BCryptSetProperty(h_alg, "ChainingMode", mode, len(mode), 0)
            if status != 0:
                raise RuntimeError(f"BCryptSetProperty failed: {status:#x}")

            h_key = wt.HANDLE()
            status = _bcrypt.BCryptGenerateSymmetricKey(h_alg, ctypes.byref(h_key), None, 0, key, len(key), 0)
            if status != 0:
                raise RuntimeError(f"BCryptGenerateSymmetricKey failed: {status:#x}")
            try:
                iv_buf = ctypes.create_string_buffer(iv, len(iv))
                out_buf = ctypes.create_string_buffer(len(data))
                result_len = ctypes.c_ulong(0)
                status = _bcrypt.BCryptDecrypt(
                    h_key, data, len(data), None,
                    iv_buf, len(iv),
                    out_buf, len(out_buf), ctypes.byref(result_len), 0,
                )
                if status != 0:
                    raise RuntimeError(f"BCryptDecrypt failed: {status:#x}")
                return out_buf.raw[: result_len.value]
            finally:
                _bcrypt.BCryptDestroyKey(h_key)
        finally:
            _bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)

elif sys.platform == "darwin":
    import ctypes.util

    _libSystem = ctypes.CDLL(ctypes.util.find_library("System"))
    _libSystem.CCCrypt.argtypes = [
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_char_p, ctypes.c_size_t,
        ctypes.c_char_p,
        ctypes.c_char_p, ctypes.c_size_t,
        ctypes.c_char_p, ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    _libSystem.CCCrypt.restype = ctypes.c_int32

    _kCCDecrypt = 1
    _kCCAlgorithmAES = 0

    def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
        """AES-256-CBC 解密（无 padding），走 macOS CommonCrypto 的 CCCrypt。"""
        out_buf = ctypes.create_string_buffer(len(data) + 32)
        out_len = ctypes.c_size_t(0)
        status = _libSystem.CCCrypt(
            _kCCDecrypt, _kCCAlgorithmAES, 0,
            key, len(key),
            iv,
            data, len(data),
            out_buf, len(out_buf),
            ctypes.byref(out_len),
        )
        if status != 0:
            raise RuntimeError(f"CCCrypt 解密失败: status={status}")
        return out_buf.raw[: out_len.value]

else:
    # Linux 或其他平台尝试使用 OpenSSL (libcrypto)
    import ctypes.util

    _libcrypto_path = ctypes.util.find_library("crypto")
    if _libcrypto_path:
        _libcrypto = ctypes.CDLL(_libcrypto_path)
        # EVP_CIPHER_CTX 接口或 ctypes 回退
        def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
            try:
                from Crypto.Cipher import AES
                cipher = AES.new(key, AES.MODE_CBC, iv)
                return cipher.decrypt(data)
            except ImportError:
                raise RuntimeError("非 Windows/macOS 平台请安装 pycryptodome: pip install pycryptodome")
    else:
        def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
            from Crypto.Cipher import AES
            cipher = AES.new(key, AES.MODE_CBC, iv)
            return cipher.decrypt(data)


# ============================================================
# HMAC Verification（与 SQLCipher4 规范一致，跨平台通用）
# ============================================================

def verify_enc_key(enc_key: bytes, db_page1: bytes) -> bool:
    salt = db_page1[:SALT_SZ]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)
    hmac_data = db_page1[SALT_SZ: PAGE_SZ - 80 + 16]
    stored_hmac = db_page1[PAGE_SZ - 64: PAGE_SZ]
    hm = hmac_mod.new(mac_key, hmac_data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored_hmac


# ============================================================
# DB File Collection
# ============================================================

def collect_db_files(db_dir: str) -> tuple[list, dict]:
    db_files: list = []
    salt_to_dbs: dict[str, list[str]] = {}
    for root, _dirs, files in os.walk(db_dir):
        for name in files:
            if not name.endswith(".db") or name.endswith("-wal") or name.endswith("-shm"):
                continue
            path = os.path.join(root, name)
            size = os.path.getsize(path)
            if size < PAGE_SZ:
                continue
            with open(path, "rb") as f:
                page1 = f.read(PAGE_SZ)
            rel = os.path.relpath(path, db_dir)
            salt = page1[:SALT_SZ].hex()
            db_files.append((rel, path, size, salt, page1))
            salt_to_dbs.setdefault(salt, []).append(rel)
    return db_files, salt_to_dbs


# ============================================================
# 内存扫描（绿泡泡 4.0.x：raw key 仍以明文缓存在进程内存 —— 目前 Windows 唯一可用方案）
# ============================================================

_HEX_RE = re.compile(rb"x'([0-9a-fA-F]{64,192})'")
WINDOWS_CONFIG_CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
WINDOWS_CONFIG_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)
WINDOWS_MAX_USER_ADDRESS = 0x0000_8000_0000_0000
WINDOWS_CONFIG_BLOB_MAX = 1024
WINDOWS_CONFIG_LITERAL_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")


def _get_pids_windows() -> list[tuple[int, int]]:
    r = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    pids: list[tuple[int, int]] = []
    for line in r.stdout.strip().split("\n"):
        if not line.strip():
            continue
        p = line.strip('"').split('","')
        if len(p) >= 5:
            pid = int(p[1])
            mem = int(p[4].replace(",", "").replace(" K", "").strip() or "0")
            pids.append((pid, mem))
    if not pids:
        raise RuntimeError("Weixin.exe 未运行")
    pids.sort(key=lambda x: x[1], reverse=True)
    for pid, mem in pids:
        _print(f"[+] Weixin.exe PID={pid} ({mem // 1024}MB)")
    return pids


def _xor_repeat(data: bytes, mask: bytes) -> bytes:
    return bytes(value ^ mask[index % len(mask)] for index, value in enumerate(data))


def _u64_from(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 8 > len(data):
        return 0
    return struct.unpack_from("<Q", data, offset)[0]


def _probable_32_byte_key(data: bytes) -> bool:
    return len(data) == KEY_SZ and len(set(data)) >= 15 and data not in {b"\x00" * KEY_SZ, b"\xff" * KEY_SZ}


def _find_bytes_in_regions(
    regions: list[tuple[int, int]],
    read_region: callable,
    needle: bytes,
) -> set[int]:
    addresses: set[int] = set()
    overlap = max(0, len(needle) - 1)
    for data_base, haystack in _iter_windows_region_chunks(regions, read_region, overlap=overlap):
        pos = haystack.find(needle)
        while pos >= 0:
            addresses.add(data_base + pos)
            pos = haystack.find(needle, pos + 1)
    return addresses


def _iter_windows_region_chunks(
    regions: list[tuple[int, int]],
    read_region: callable,
    *,
    chunk_size: int = 2 * 1024 * 1024,
    overlap: int = 0,
):
    """Yield readable chunks from Windows process regions with optional overlap."""
    for base, size in regions:
        offset = 0
        tail = b""
        tail_base = base
        while offset < size:
            current_size = min(chunk_size, size - offset)
            chunk = read_region(base + offset, current_size) or b""
            data_base = tail_base if tail else base + offset
            data = tail + chunk
            if data:
                yield data_base, data
                if overlap:
                    tail = data[-overlap:]
                    tail_base = data_base + max(0, len(data) - len(tail))
                else:
                    tail = b""
                    tail_base = base + offset + current_size
            else:
                tail = b""
                tail_base = base + offset + current_size
            offset += current_size


def _windows_v411_config_key_candidates(blob: bytes) -> list[tuple[str, str | None]]:
    """Extract redaction-safe key/salt candidates from a Config.Cipher blob."""
    if not blob or len(blob) > WINDOWS_CONFIG_BLOB_MAX:
        return []
    decoded = _xor_repeat(blob, WINDOWS_CONFIG_XOR_MASK)
    out: list[tuple[str, str | None]] = []
    seen: set[tuple[str, str | None]] = set()
    for match in WINDOWS_CONFIG_LITERAL_RE.finditer(decoded):
        run = match.group(1).decode("ascii").lower()
        starts = [0]
        if len(run) > 96:
            starts.extend(range(0, len(run) - 63, 32))
            starts.append(len(run) - 64)
        for start in dict.fromkeys(starts):
            if start < 0 or start + 64 > len(run):
                continue
            enc_key_hex = run[start:start + 64]
            try:
                enc_key = bytes.fromhex(enc_key_hex)
            except ValueError:
                continue
            if not _probable_32_byte_key(enc_key):
                continue
            embedded_salt = None
            if start + 96 <= len(run):
                embedded_salt = run[start + 64:start + 96]
            item = (enc_key_hex, embedded_salt)
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


def _verify_windows_direct_key_candidate(
    enc_key_hex: str,
    embedded_salt: str | None,
    db_files: list,
    salt_to_dbs: dict,
    key_map: dict[str, str],
    remaining_salts: set[str],
) -> int:
    if not remaining_salts:
        return 0
    try:
        enc_key = bytes.fromhex(enc_key_hex)
    except ValueError:
        return 0
    if not _probable_32_byte_key(enc_key):
        return 0
    matched = 0
    target_salts = [embedded_salt] if embedded_salt in remaining_salts else list(remaining_salts)
    for salt_hex in target_salts:
        if salt_hex not in remaining_salts:
            continue
        for _rel, _path, _sz, s, page1 in db_files:
            if s == salt_hex and verify_enc_key(enc_key, page1):
                key_map[salt_hex] = enc_key_hex
                remaining_salts.discard(salt_hex)
                matched += 1
                break
    return matched


def _scan_windows_v411_config_cipher(
    pid: int,
    regions: list[tuple[int, int]],
    read_region: callable,
    read_mem: callable,
    db_files: list,
    salt_to_dbs: dict,
    key_map: dict[str, str],
    remaining_salts: set[str],
    print_fn: callable,
) -> dict:
    """Read-only Windows WeChat 4.1.11 Config.Cipher key scan."""
    stats = {
        "needle_occurrences": 0,
        "string_object_refs": 0,
        "node_candidates": 0,
        "config_ptr_candidates": 0,
        "blob99_count": 0,
        "candidate_count": 0,
        "verified_candidates": 0,
        "matched_salts": 0,
    }
    needle_addresses = _find_bytes_in_regions(regions, read_region, WINDOWS_CONFIG_CIPHER_NAME)
    stats["needle_occurrences"] = len(needle_addresses)
    if not needle_addresses:
        return stats

    pair_patterns = [
        struct.pack("<Q", addr) + struct.pack("<Q", len(WINDOWS_CONFIG_CIPHER_NAME))
        for addr in needle_addresses
    ]
    seen_config_ptrs: set[int] = set()
    seen_candidates: set[tuple[str, str | None]] = set()

    for base, data in _iter_windows_region_chunks(regions, read_region, overlap=0x80):
        if not remaining_salts:
            break
        for pattern in pair_patterns:
            pos = data.find(pattern)
            while pos >= 0:
                stats["string_object_refs"] += 1
                qaddr = base + pos
                node_base = qaddr - 0x10
                node = read_mem(node_base, 0x50)
                if not node or len(node) < 0x40:
                    pos = data.find(pattern, pos + 1)
                    continue
                if _u64_from(node, 0x10) not in needle_addresses or _u64_from(node, 0x18) != len(WINDOWS_CONFIG_CIPHER_NAME):
                    pos = data.find(pattern, pos + 1)
                    continue
                config_ptr = _u64_from(node, 0x28)
                if not (0x10000 <= config_ptr < WINDOWS_MAX_USER_ADDRESS):
                    pos = data.find(pattern, pos + 1)
                    continue
                stats["node_candidates"] += 1
                seen_config_ptrs.add(config_ptr)

                obj = read_mem(config_ptr + 0x88, 0x28)
                if not obj or len(obj) < 0x18:
                    pos = data.find(pattern, pos + 1)
                    continue
                data_ptr = _u64_from(obj, 0x8)
                data_len = _u64_from(obj, 0x10)
                if not (0 < data_len <= WINDOWS_CONFIG_BLOB_MAX and 0x10000 <= data_ptr < WINDOWS_MAX_USER_ADDRESS):
                    pos = data.find(pattern, pos + 1)
                    continue
                blob = read_mem(data_ptr, int(data_len))
                if not blob or len(blob) != data_len:
                    pos = data.find(pattern, pos + 1)
                    continue
                if data_len == 99:
                    stats["blob99_count"] += 1
                for enc_key_hex, embedded_salt in _windows_v411_config_key_candidates(blob):
                    candidate = (enc_key_hex, embedded_salt)
                    if candidate in seen_candidates:
                        continue
                    seen_candidates.add(candidate)
                    stats["candidate_count"] += 1
                    matched = _verify_windows_direct_key_candidate(
                        enc_key_hex,
                        embedded_salt,
                        db_files,
                        salt_to_dbs,
                        key_map,
                        remaining_salts,
                    )
                    if matched:
                        stats["verified_candidates"] += 1
                        stats["matched_salts"] += matched
                pos = data.find(pattern, pos + 1)

    stats["config_ptr_candidates"] = len(seen_config_ptrs)
    if stats["matched_salts"]:
        print_fn(
            "[+] Windows 4.1 runtime Config.Cipher scan matched "
            f"{stats['matched_salts']}/{len(salt_to_dbs)} salts "
            f"(pid={pid}, candidates={stats['candidate_count']})"
        )
    else:
        print_fn(
            "[INFO] Windows 4.1 runtime Config.Cipher scan found no verified key "
            f"(pid={pid}, nodes={stats['node_candidates']}, candidates={stats['candidate_count']})"
        )
    return stats


def _scan_memory_raw_key(db_dir: str, keys_file: str) -> dict:
    """提取 Windows 绿泡泡数据库密钥。

    先尝试 Windows 4.1+ 的只读 runtime Config.Cipher 扫描，再回退到老版本
    raw key 内存扫描。
    """
    db_files, salt_to_dbs = collect_db_files(db_dir)
    if not db_files:
        raise RuntimeError(f"在 {db_dir} 未找到可解密的 .db 文件")
    _print(f"找到 {len(db_files)} 个数据库, {len(salt_to_dbs)} 个不同的 salt")

    kernel32 = ctypes.windll.kernel32
    MEM_COMMIT = 0x1000
    READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}

    class MBI(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_uint64), ("AllocationBase", ctypes.c_uint64),
            ("AllocationProtect", wt.DWORD), ("_pad1", wt.DWORD),
            ("RegionSize", ctypes.c_uint64), ("State", wt.DWORD),
            ("Protect", wt.DWORD), ("Type", wt.DWORD), ("_pad2", wt.DWORD),
        ]

    def read_mem(h, addr, sz):
        buf = ctypes.create_string_buffer(sz)
        n = ctypes.c_size_t(0)
        if kernel32.ReadProcessMemory(h, ctypes.c_uint64(addr), buf, sz, ctypes.byref(n)):
            return buf.raw[: n.value]
        return None

    def enum_regions(h):
        regs = []
        addr = 0
        mbi = MBI()
        while addr < 0x7FFFFFFFFFFF:
            if kernel32.VirtualQueryEx(h, ctypes.c_uint64(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)) == 0:
                break
            if mbi.State == MEM_COMMIT and mbi.Protect in READABLE and 0 < mbi.RegionSize < 500 * 1024 * 1024:
                regs.append((mbi.BaseAddress, mbi.RegionSize))
            nxt = mbi.BaseAddress + mbi.RegionSize
            if nxt <= addr:
                break
            addr = nxt
        return regs

    pids = _get_pids_windows()
    key_map: dict[str, str] = {}
    remaining_salts = set(salt_to_dbs.keys())
    t0 = time.time()

    runtime_stats: list[dict] = []
    for pid, _mem_kb in pids:
        h = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not h:
            _print(f"[WARN] 无法打开进程 PID={pid}，跳过（尝试以管理员身份运行）")
            continue
        try:
            regions = enum_regions(h)
            runtime_stats.append(_scan_windows_v411_config_cipher(
                pid,
                regions,
                lambda base, size, _h=h: read_mem(_h, base, size),
                lambda addr, size, _h=h: read_mem(_h, addr, size),
                db_files,
                salt_to_dbs,
                key_map,
                remaining_salts,
                _print,
            ))
        finally:
            kernel32.CloseHandle(h)
        if not remaining_salts:
            _print("[+] Windows 4.1 runtime Config.Cipher scan covered all databases")
            _save_results(db_files, salt_to_dbs, key_map, db_dir, keys_file)
            return key_map

    if key_map:
        _print(f"[INFO] Windows 4.1 runtime Config.Cipher scan partial: {len(key_map)}/{len(salt_to_dbs)} salts")
    elif runtime_stats:
        _print("[INFO] Windows 4.1 runtime Config.Cipher scan did not resolve keys; trying legacy scan")

    for pid, mem_kb in pids:
        h = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not h:
            _print(f"[WARN] 无法打开进程 PID={pid}，跳过（尝试以管理员身份运行）")
            continue
        try:
            for base, size in enum_regions(h):
                if not remaining_salts:
                    break
                data = read_mem(h, base, size)
                if not data:
                    continue
                for m in _HEX_RE.finditer(data):
                    hex_str = m.group(1).decode()
                    if len(hex_str) < 96:
                        continue
                    enc_key_hex, salt_hex = hex_str[:64], hex_str[64:96]
                    if salt_hex not in remaining_salts:
                        continue
                    enc_key = bytes.fromhex(enc_key_hex)
                    for rel, _path, _sz, s, page1 in db_files:
                        if s == salt_hex and verify_enc_key(enc_key, page1):
                            key_map[salt_hex] = enc_key_hex
                            remaining_salts.discard(salt_hex)
                            _print("  [FOUND] verified key material")
                            break
        finally:
            kernel32.CloseHandle(h)
        if not remaining_salts:
            break

    _print(f"\n扫描完成: {time.time() - t0:.1f}s, {len(pids)} 个进程")
    if not key_map:
        raise RuntimeError(
            "未能从进程内存提取到密钥。若绿泡泡已是 4.1+ 版本，主路径会先尝试只读 "
            "runtime Config.Cipher 扫描，再回退到老版本内存扫描；如果仍失败，请确认"
            "绿泡泡已登录并且数据库目录正确。"
        )
    _save_results(db_files, salt_to_dbs, key_map, db_dir, keys_file)
    return key_map


# ============================================================
# [LEGACY / 研究性备用] CNG 断点捕获 passphrase（绿泡泡 4.1+ 猜想方案）
# ============================================================
#
# ⚠️ 警告：以下代码从未在真实 Windows + 绿泡泡环境里跑通过，纯粹是技术推理，
# 请不要当作已解决的方案使用，也不要在文档/宣传里说 Windows 新版已攻克。
#
# 推理依据：
# Linux 版 WCDB 的 PBKDF2 是自己实现/静态链接的，找不到系统导出符号，
# 只能靠 ELF 静态分析定位 WCDB 内部函数断点（见 elf_analyzer.py）。
# macOS 版 WCDB 则直接调用系统 CommonCrypto 的 CCKeyDerivationPBKDF——
# 一个系统自带、有公开符号的导出函数，不需要逆向绿泡泡自己的二进制
# （见 wcdb_key_tool_macos.py 里的 capture_passphrase_lldb）。
# 如果 Windows 版 WCDB 同样选择调用系统密码库而非自己实现，最接近的
# 系统函数是 CNG（Cryptography API: Next Generation）的
# BCryptDeriveKeyPBKDF2，导出自系统自带的 bcrypt.dll。
#
# 如果这个假设成立：用 WinDbg 的命令行版 cdb.exe attach 绿泡泡进程，在
# bcrypt!BCryptDeriveKeyPBKDF2 上下条件断点。x64 调用约定
# （BCryptDeriveKeyPBKDF2(hPrf, pbPassword, cbPassword, pbSalt, ...)）下，
# 第 2 个参数 pbPassword 在 RDX，第 3 个参数 cbPassword 在 R8；只在
# R8==32（32 字节 passphrase）时触发，命中后用 `db` 命令读 32 字节。
#
# 需要一台真实 Windows 绿泡泡环境验证的开放问题（欢迎提 Issue/PR 反馈）：
#   1. 绿泡泡 Windows 版是否真的调用 BCryptDeriveKeyPBKDF2——也可能走的是
#      旧版 CryptoAPI（CryptDeriveKey）、或者跟 Linux 一样自己实现/静态
#      链接了 PBKDF2，那样这个断点永远不会命中
#   2. 断点条件 r8==32 是否会命中大量无关调用（系统里其他地方也可能用
#      同一个函数派生 32 字节密钥），需不需要加其他过滤条件
#   3. attach 绿泡泡主进程是否需要管理员权限、是否会被反调试机制拦截
#   4. 下面 _parse_db_output() 解析 cdb.exe `db` 命令输出的正则，没有
#      真实输出样本校对过，格式很可能对不上，需要用真实输出调整

DEFAULT_CDB_TIMEOUT = 180


class ExperimentalCaptureError(RuntimeError):
    pass


def check_cdb_prerequisites() -> list[str]:
    issues = []
    if not shutil.which("cdb"):
        issues.append(
            "未检测到 cdb.exe，请安装 \"Debugging Tools for Windows\""
            "（Windows SDK 的可选组件，或随 WinDbg 安装）"
        )
    return issues


def _find_wechat_pid_windows() -> int | None:
    r = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    for line in r.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.strip('"').split('","')
        if len(parts) >= 2:
            return int(parts[1])
    return None


def _parse_db_output(text: str) -> str | None:
    """从 cdb.exe `db` 命令的输出中解析 32 字节 passphrase。

    WinDbg `db` 典型输出形如（未经真实样本核对，格式可能有出入）：
        00007ff6`12345678  8c fc 66 8a 1b 2c 3d 4e-5f 60 71 82 93 a4 b5 c6  ................
    """
    hex_bytes: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^[0-9a-f`]+\s+((?:[0-9a-f]{2}[\s\-]+){15}[0-9a-f]{2})\s", line, re.IGNORECASE)
        if m:
            hex_bytes += re.findall(r"[0-9a-f]{2}", m.group(1))
    return "".join(hex_bytes[:32]).lower() if len(hex_bytes) >= 32 else None


def capture_passphrase_experimental(pid: int | None = None, timeout: int = DEFAULT_CDB_TIMEOUT) -> str:
    """[实验性/未验证] attach 绿泡泡进程，猜测性地断在 bcrypt!BCryptDeriveKeyPBKDF2 上。

    需要用户在捕获期间于绿泡泡内退出登录、重新登录来触发断点（如果这个
    猜测成立的话）。没有真机验证过，随时可能因为假设不成立而永远等不到命中。
    """
    issues = check_cdb_prerequisites()
    if issues:
        raise ExperimentalCaptureError("; ".join(issues))

    if pid is None:
        pid = _find_wechat_pid_windows()
    if not pid:
        raise ExperimentalCaptureError("未找到绿泡泡进程，请先启动并登录绿泡泡")

    _print("=" * 60)
    _print("  [实验性/未验证] 尝试断点捕获 passphrase")
    _print("  这条路从未在真机上验证过，可能永远等不到命中")
    _print("=" * 60)

    bp_action = (
        '.if (@r8 = 0n32) '
        '{ .echo WCDB_PASSPHRASE_BEGIN; db @rdx L20; .echo WCDB_PASSPHRASE_END } '
        '.else { g }'
    )
    cdb_cmd = f'bp bcrypt!BCryptDeriveKeyPBKDF2 "{bp_action}"; g'

    proc = subprocess.Popen(
        ["cdb", "-p", str(pid), "-c", cdb_cmd],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    buf = ""
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.2)
                continue
            buf += line
            if "WCDB_PASSPHRASE_END" in buf:
                break
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    if "WCDB_PASSPHRASE_BEGIN" not in buf:
        raise ExperimentalCaptureError(
            "超时未命中断点。可能是本猜测方案的假设不成立（绿泡泡没有调用"
            "BCryptDeriveKeyPBKDF2），也可能只是没有在捕获期间重新登录绿泡泡。"
        )

    segment = buf.split("WCDB_PASSPHRASE_BEGIN", 1)[1].split("WCDB_PASSPHRASE_END", 1)[0]
    ph = _parse_db_output(segment)
    if not ph:
        raise ExperimentalCaptureError(
            "断点命中了，但没能从 cdb.exe 输出里解析出 32 字节数据——"
            "大概率是 _parse_db_output() 的正则没对上真实输出格式，需要用真实样本调整。"
            f"\n原始输出片段：\n{segment[:500]}"
        )
    logger.info("[实验性] 断点命中并解析出 32 字节数据，但尚未验证这就是真正的 passphrase")
    return ph


# ============================================================
# LLDB 断点捕获（macOS 微信 4.1.10+：断点 CommonCrypto CCKeyDerivationPBKDF）
# ============================================================

DEFAULT_LLDB_TIMEOUT = 0  # 0 表示持续监听，直到捕获成功或手动中断


class LLDBPassphraseCaptureError(RuntimeError):
    pass


def check_lldb_prerequisites() -> list[str]:
    issues = []
    if not shutil.which("lldb"):
        issues.append("未检测到 lldb，请安装 Xcode Command Line Tools: xcode-select --install")
    return issues


def _find_wechat_pid_macos() -> int | None:
    try:
        r = subprocess.run(["pgrep", "-x", "WeChat"], capture_output=True, text=True)
        pids = [int(p) for p in r.stdout.split() if p.strip().isdigit()]
        return pids[0] if pids else None
    except Exception:
        return None


def _cleanup_lldb_orphans_macos(
    keep_pids: "set[int] | None" = None,
    tracee_pid: "int | None" = None,
) -> None:
    """清理残留的 lldb 进程。

    ⚠️ 重大安全约束（经真机实验确认）：
      向 LLDB.framework 的 debugserver 发送 ANY 信号（SIGTERM 或 SIGKILL）
      都会使 macOS 调试子系统连带把 tracee（微信）SIGKILL 强杀！
      实验证据：kill -TERM debugserver → tracee 立即 "Killed: 9"。
      因此本函数 **绝不触碰 debugserver**，无论信号强弱。

    只清理孤立的 lldb 前端进程（它们退出时会自行关闭 debugserver）。
    若 tracee 仍存活，则完全不清理，宁肯留下惰性孤儿进程。
    """
    if tracee_pid:
        _resume_tracee_if_stopped(tracee_pid)

    def _alive(p: int) -> bool:
        try:
            os.kill(p, 0)
            return True
        except OSError:
            return False

    # 硬性安全阀：tracee 存活时绝不做任何清理动作。
    if tracee_pid and _alive(tracee_pid):
        return

    protected = {p for p in (keep_pids or set()) if p and _alive(p)}

    # 仅向孤立的 lldb 前端发 SIGTERM；绝不使用 -9，绝不触碰 debugserver。
    try:
        r = subprocess.run(["pgrep", "-x", "lldb"], capture_output=True, text=True, timeout=3)
        for tok in r.stdout.split():
            if tok.isdigit() and int(tok) not in protected:
                try:
                    os.kill(int(tok), signal.SIGTERM)
                except OSError:
                    pass
    except Exception:
        pass

    # 刻意不做 SIGKILL 兜底，也不清理 debugserver：
    # 残留的 lldb 只是惰性孤儿进程，不会伤害微信；清理它的收益远小于杀掉微信的风险。
    # 用户可随时手动结束，因此宁可不清理也不冒杀微信的风险。
    return


def _resume_tracee_if_stopped(tracee_pid: int) -> bool:
    """若 tracee 仍被调试器暂停，发 SIGCONT 使其继续运行，避免停在断点上崩溃。

    返回 True 表示确认进程处于运行态（或已不存在），False 表示仍无法恢复。
    """
    for _ in range(20):
        if not _pid_alive(tracee_pid):
            return True
        try:
            # macOS 无 /proc，用 ps 取 STAT 列判断是否处于 stopped / traced-stopped
            r = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(tracee_pid)],
                capture_output=True, text=True, timeout=3,
            )
            stat = (r.stdout or "").strip()
        except Exception:
            return True
        if not stat:
            return True
        # T = stopped，X = traced；组合 "SX"/"TX"/"RX" 表示被调试器跟踪但未必停住，
        # 关键看是否有 T（真正 stopped）。
        if "T" not in stat:
            return True
        try:
            os.kill(tracee_pid, signal.SIGCONT)
        except OSError:
            return True
        time.sleep(0.2)
    return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _wait_lldb_exit(proc: "subprocess.Popen | None", timeout: float = 8.0) -> bool:
    """等待 lldb 进程（含其管道包装）彻底退出，确保 detach 完成后再做清理。

    返回 True 表示已退出。
    """
    if proc is None:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.1)
    return proc.poll() is not None


def _parse_lldb_passphrase(out: str) -> str | None:
    """从 lldb `memory read --format x` 的 hexdump 中解析 32 字节 passphrase。"""
    by: list[str] = []
    for line in out.splitlines():
        m = re.match(r"\s*0x[0-9a-f]+:\s+((?:0x[0-9a-f]{2}\s*)+)$", line)
        if m:
            by += re.findall(r"0x([0-9a-f]{2})", m.group(1))
    return "".join(by[:32]) if len(by) >= 32 else None


def capture_passphrase_lldb(
    timeout: int = DEFAULT_LLDB_TIMEOUT,
    pid: int | None = None,
    verify_callback: typing.Callable[[str], bool] | None = None,
) -> str:
    """attach 微信进程，在 CCKeyDerivationPBKDF 上断点，等待用户重新登录触发命中。

    支持对命中候选通过 verify_callback 进行实时 HMAC/PBKDF2 校验，
    直到验证通过才 detach 退出；若未提供 verify_callback 则首个有效命中即返回。
    """
    import platform

    issues = check_lldb_prerequisites()
    if issues:
        raise LLDBPassphraseCaptureError("; ".join(issues))

    target_pid = pid or _find_wechat_pid_macos()
    if not target_pid:
        raise LLDBPassphraseCaptureError("未找到微信进程。请确保微信已启动（停留在扫码登录界面或已登录）")

    is_arm = platform.machine() in ("arm64", "aarch64")
    # ABI 参数定义:
    # ARM64: x0=alg, x1=passphrase, x2=passphraseLen, x3=salt, x4=saltLen, x5=prf, sp=rounds, ...
    # x86_64: rdi=alg, rsi=passphrase, rdx=passphraseLen, rcx=salt, r8=saltLen, r9=prf
    # 收紧断点过滤：alg==2 (kCCPBKDF2), len==32, saltLen==16
    if is_arm:
        bp_cond = "$x2 == 32 && $x0 == 2 && $x4 == 16"
        pw_reg = "x1"
    else:
        bp_cond = "$rdx == 32 && $rdi == 2 && $r8 == 16"
        pw_reg = "rsi"

    # 关键修复（防卡死）：旧实现把读取逻辑写成 lldb 断点命令块：
    #   memory read ... / process continue
    # 但 lldb 默认 stop-command-source-on-error=true，一旦 memory read 失败
    # （微信在独立线程调用 PBKDF2，$rsi 指向栈上临时缓冲区，命中瞬间可能读取失败），
    # lldb 会中止命令块剩余命令，导致末尾的 `process continue` 永远不执行 ——
    # 微信进程被永久冻结在断点上。
    # 因此改为 Python 回调：所有读取都包在 try/except 中，并在 finally 中无条件
    # 调用 process.Continue()，保证无论读取成功与否都会放行目标进程。
    hook_src = f'''import lldb

_PW_REG = "{pw_reg}"


def _hexify(data):
    try:
        return " ".join("0x%02x" % b for b in bytes(data))
    except Exception:
        pass
    try:
        err = lldb.SBError()
        return " ".join("0x%02x" % data.GetUnsignedInt8(err, i) for i in range(32))
    except Exception:
        return ""


def pbkdf_hook(frame, bp_loc, internal_dict):
    proc = frame.GetThread().GetProcess()
    try:
        addr = 0
        try:
            addr = frame.FindRegister(_PW_REG).GetValueAsUnsigned()
        except Exception as e:
            print("=== HOOK_REG_ERR %s ===" % e)
        data = None
        if addr:
            err = lldb.SBError()
            data = proc.ReadMemory(addr, 32, err)
            if not err.Success():
                data = None
        if data is not None:
            hexs = _hexify(data)
            if hexs:
                print("=== CANDIDATE_START ===")
                print("0x%016x: %s" % (addr, hexs))
                print("=== CANDIDATE_END ===")
            else:
                print("=== HOOK_READ_EMPTY ===")
        else:
            print("=== HOOK_READ_FAILED addr=0x%x ===" % addr)
    except Exception as e:
        print("=== HOOK_ERR %s ===" % e)
    finally:
        # 无条件放行，杜绝把微信冻在断点上
        try:
            proc.Continue()
        except Exception:
            pass
    return False
'''
    fd_h, hook_path = tempfile.mkstemp(suffix=".py", prefix="wxhook_")
    with os.fdopen(fd_h, "w") as f:
        f.write(hook_src)

    script = (
        "settings set target.preload-symbols false\n"
        f"command script import {hook_path}\n"
        f"process attach -p {target_pid}\n"
        f"breakpoint set -n CCKeyDerivationPBKDF -c '{bp_cond}'\n"
        "breakpoint command add 1 -F " + os.path.basename(hook_path)[:-3] + ".pbkdf_hook\n"
        "process continue\n"
    )
    fd, sp = tempfile.mkstemp(suffix=".lldb", prefix="wxcap_")
    with os.fdopen(fd, "w") as f:
        f.write(script)

    # 关键修复：当后台启动（如 nohup ... </dev/null）时，若 stdin 关闭或为 /dev/null，
    # cat 读 stdin 会立刻得到 EOF 退出，导致管道写端全部关闭，lldb 收到 EOF 退出并连带关闭。
    # 因此超时采用精确 sleep；timeout=0 时采用超长 sleep（2147483647 秒，约 68 年）保活管道。
    sleep_sec = timeout if timeout > 0 else 2147483647
    proc = subprocess.Popen(
        ["bash", "-c", f"( cat {sp}; sleep {sleep_sec} ) | TERM=dumb lldb"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    buf = ""
    deadline = (time.time() + timeout) if timeout > 0 else None
    last_check_pid_time = time.time()
    seen_candidates: set[str] = set()
    verified_passphrase: str | None = None
    # 冻结看门狗：若断点命中后 12 秒内既没有 resuming 也没有候选输出，
    # 说明回调未生效（如 import 失败），立即强制 detach 解冻，避免微信卡死。
    bp_stop_seen_at: float | None = None
    FREEZE_GUARD_SECONDS = 12.0

    try:
        while deadline is None or time.time() < deadline:
            r, _, _ = select.select([proc.stdout], [], [], 0.5)
            if r:
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    continue
                buf += line

                if "stop reason = breakpoint" in line:
                    bp_stop_seen_at = time.time()
                elif "resuming" in line or "CANDIDATE_START" in line:
                    bp_stop_seen_at = None

                # 流式解析 CANDIDATE
                while "=== CANDIDATE_START ===" in buf and "=== CANDIDATE_END ===" in buf:
                    start_idx = buf.index("=== CANDIDATE_START ===")
                    end_idx = buf.index("=== CANDIDATE_END ===") + len("=== CANDIDATE_END ===")
                    segment = buf[start_idx:end_idx]
                    buf = buf[end_idx:]

                    cand = _parse_lldb_passphrase(segment)
                    if cand and cand not in seen_candidates:
                        seen_candidates.add(cand)
                        logger.info(f"LLDB 捕获候选 passphrase: {cand[:8]}...")
                        if verify_callback is not None:
                            if verify_callback(cand):
                                verified_passphrase = cand
                                break
                            else:
                                logger.info(f"候选 {cand[:8]}... 校验未通过，继续监听后续断点...")
                        else:
                            verified_passphrase = cand
                            break

                if verified_passphrase:
                    break

            # 冻结看门狗
            if bp_stop_seen_at is not None and (time.time() - bp_stop_seen_at) > FREEZE_GUARD_SECONDS:
                logger.error("断点命中后进程未自动恢复，正在强制 detach 解冻...")
                raise LLDBPassphraseCaptureError(
                    "断点命中后微信进程未能自动恢复，已强制 detach 解冻（微信应已恢复正常使用）。\n"
                    "这通常意味着断点回调未生效。请重试，或改用 --passphrase 手动传入口令。"
                )

            # 进程漂移容错：每隔 2 秒检查目标微信进程是否仍然存活
            now = time.time()
            if now - last_check_pid_time >= 2.0:
                last_check_pid_time = now
                current_pid = _find_wechat_pid_macos()
                if not current_pid:
                    logger.warning(f"微信进程 (PID={target_pid}) 已完全退出")
                elif current_pid != target_pid:
                    logger.warning(
                        f"检测到微信进程已重启 (原PID: {target_pid} -> 新PID: {current_pid})！"
                        "原监听断点已失效，请重新启动 wechat-init 进行监听"
                    )
                    raise LLDBPassphraseCaptureError(
                        f"微信进程已重启（PID 由 {target_pid} 变为 {current_pid}），监听断点失效！\n"
                        "【正确操作顺序】：\n"
                        "1. 在微信上点击「设置」->「退出登录」，使微信窗口停留在扫码登录界面；\n"
                        "2. 重新启动 wechat-init（绑定扫码界面的新进程）；\n"
                        "3. 掏出手机扫码登录微信。"
                    )
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        # 关键顺序：先等 lldb 真正退出（完成 detach），再动 tracee 与残留进程。
        # 旧实现直接 pkill，若与 detach 收尾竞争，会让 tracee 停在断点指令上
        # 并触发 EXC_BREAKPOINT/SIGTRAP 崩溃。
        _wait_lldb_exit(proc, timeout=8.0)
        _resume_tracee_if_stopped(target_pid)
        for _tmp in (sp, hook_path):
            try:
                os.unlink(_tmp)
            except OSError:
                pass
        _cleanup_lldb_orphans_macos(tracee_pid=target_pid)

    if verified_passphrase:
        logger.info("LLDB passphrase 捕获并确认成功")
        return verified_passphrase

    # 兜底：如果没通过流式结构拿到，但整个 buf 里有单个候选
    fallback_cand = _parse_lldb_passphrase(buf)
    if fallback_cand:
        if verify_callback is None or verify_callback(fallback_cand):
            logger.info("LLDB passphrase 兜底解析成功")
            return fallback_cand

    raise LLDBPassphraseCaptureError(
        "未捕获到有效 passphrase。\n"
        "常见排查与原因：\n"
        "  1) 微信进程在监听期间重启（退出登录会导致微信重启，PID 发生变化）\n"
        "     → 正确顺序：先在微信内退出登录停在扫码界面，再启动 wechat-init 监听，最后扫码登录。\n"
        "  2) 断点命中了非数据库派生调用且未捕获到有效口令。\n"
        "  3) 权限受限（若为非 disabled SIP 机器且未重签名）\n"
        "     → 执行一次重签名：sudo codesign --force --deep --sign - /Applications/WeChat.app"
    )


# ============================================================
# macOS 4.0.x 内存明文扫描 (raw key)
# ============================================================

def _task_for_pid_macos(pid: int) -> int:
    task = ctypes.c_uint32(0)
    kr = _libSystem.task_for_pid(_libSystem.mach_task_self(), ctypes.c_int(pid), ctypes.byref(task))
    if kr != 0:
        raise PermissionError(
            f"task_for_pid failed for PID={pid} (kern_return={kr})。"
            "请以 sudo / 管理员权限运行，或确认微信已重签名去除 Hardened Runtime。"
        )
    return task.value


class _vm_region_basic_info_64(ctypes.Structure):
    _fields_ = [
        ("protection", ctypes.c_int32),
        ("max_protection", ctypes.c_int32),
        ("inheritance", ctypes.c_uint32),
        ("shared", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("offset", ctypes.c_uint64),
        ("behavior", ctypes.c_int32),
        ("user_wired_count", ctypes.c_uint16),
    ]


def _enum_readable_regions_macos(task: int) -> list[tuple[int, int]]:
    regions: list[tuple[int, int]] = []
    address = ctypes.c_uint64(0)
    size = ctypes.c_uint64(0)
    info = _vm_region_basic_info_64()
    info_count = ctypes.c_uint32(9)
    object_name = ctypes.c_uint32(0)

    while True:
        kr = _libSystem.mach_vm_region(
            ctypes.c_uint32(task),
            ctypes.byref(address),
            ctypes.byref(size),
            ctypes.c_int(9),
            ctypes.byref(info),
            ctypes.byref(info_count),
            ctypes.byref(object_name),
        )
        if kr != 0:
            break

        reg_size = size.value
        if (info.protection & 0x01) and 0 < reg_size < 500 * 1024 * 1024:
            regions.append((address.value, reg_size))

        next_addr = address.value + reg_size
        if next_addr <= address.value:
            break
        address.value = next_addr

    return regions


def _read_memory_macos(task: int, address: int, size: int) -> bytes | None:
    # 钳制单次读取大小上限，避免对大区段触发 SIGBUS
    if size <= 0 or size > 32 * 1024 * 1024:
        return None
    data_ptr = ctypes.c_uint64(0)
    data_size = ctypes.c_uint64(0)
    kr = _libSystem.mach_vm_read(
        ctypes.c_uint32(task),
        ctypes.c_uint64(address),
        ctypes.c_uint64(size),
        ctypes.byref(data_ptr),
        ctypes.byref(data_size),
    )
    if kr != 0 or data_size.value == 0:
        return None
    try:
        return ctypes.string_at(data_ptr.value, data_size.value)
    except Exception:
        return None
    finally:
        _libSystem.mach_vm_deallocate(_libSystem.mach_task_self(), data_ptr, data_size)


def _scan_memory_raw_key_macos(db_dir: str, keys_file: str) -> dict:
    """macOS 下扫描微信进程内存，匹配 x'<64hex_enc_key><32hex_salt>'（微信 4.0.x）。"""
    db_files, salt_to_dbs = collect_db_files(db_dir)
    if not db_files:
        raise RuntimeError(f"在 {db_dir} 未找到可解密的 .db 文件")

    r = subprocess.run(["pgrep", "-x", "WeChat"], capture_output=True, text=True)
    pids = [int(p) for p in r.stdout.split() if p.strip().isdigit()]
    if not pids:
        raise RuntimeError("未找到微信进程，请先启动微信")

    key_map: dict[str, str] = {}
    remaining_salts = set(salt_to_dbs.keys())
    task_ok_count = 0
    pattern = re.compile(rb"x'([0-9a-f]{96})'")

    for pid in pids:
        if not remaining_salts:
            break
        try:
            task = _task_for_pid_macos(pid)
            task_ok_count += 1
        except PermissionError as e:
            logger.warning(f"{e}")
            continue

        regions = _enum_readable_regions_macos(task)
        for base, size in regions:
            if not remaining_salts:
                break
            data = _read_memory_macos(task, base, size)
            if not data:
                continue
            for m in pattern.finditer(data):
                hex_str = m.group(1).decode()
                enc_key_hex, salt_hex = hex_str[:64], hex_str[64:]
                if salt_hex not in remaining_salts:
                    continue
                enc_key = bytes.fromhex(enc_key_hex)
                for rel, _path, _sz, s, page1 in db_files:
                    if s == salt_hex and verify_enc_key(enc_key, page1):
                        key_map[salt_hex] = enc_key_hex
                        remaining_salts.discard(salt_hex)
                        _print(f"  [FOUND] salt={salt_hex} enc_key={enc_key_hex}")
                        break

    if not key_map:
        if task_ok_count == 0:
            raise PermissionError("所有微信进程都无法读取内存（task_for_pid 失败），需以 sudo 运行或重签名去除 Hardened Runtime")
        raise RuntimeError("能读内存但未扫到明文密钥（微信版本可能是 4.1+）")

    _save_results(db_files, salt_to_dbs, key_map, db_dir, keys_file)
    return key_map


# ============================================================
# PBKDF2 Key Derivation（拿到 passphrase 后走这条路，三平台通用）
# ============================================================

def _derive_keys_from_passphrase(passphrase: bytes, db_files: list, salt_to_dbs: dict) -> dict:
    key_map: dict[str, str] = {}
    total = len(salt_to_dbs)
    for i, salt_hex in enumerate(salt_to_dbs):
        salt = bytes.fromhex(salt_hex)
        enc_key = hashlib.pbkdf2_hmac("sha512", passphrase, salt, 256000, dklen=KEY_SZ)
        for _rel, _path, _sz, s, page1 in db_files:
            if s == salt_hex and verify_enc_key(enc_key, page1):
                key_map[salt_hex] = enc_key.hex()
                break
        if (i + 1) % 5 == 0 or i == total - 1:
            _print(f"  PBKDF2 派生: {i + 1}/{total} ({len(key_map)} 验证通过)")
    return key_map


def load_passphrase() -> str | None:
    """读取已保存的 passphrase，格式非法时视为未保存（回退到内存扫描）。"""
    try:
        with open(PASSPHRASE_FILE, "r") as f:
            ph = json.load(f).get("passphrase")
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    if not ph:
        return None
    ph = str(ph).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", ph):
        logger.warning(
            f"已保存的 passphrase 不是 64 位十六进制字符串，已忽略该文件: {PASSPHRASE_FILE}"
        )
        return None
    return ph


def _restrict_file_permissions(path: str) -> str:
    """尽力收紧密钥文件的访问权限，返回实际生效的方式描述。

    POSIX 下 chmod 0600 就够了；Windows 下 os.chmod 只能切换只读位、不构成
    访问控制，必须用 icacls 断开继承并只授权当前用户，否则该文件对同机其他
    账户仍然可读。
    """
    if os.name == "nt":
        user = os.environ.get("USERNAME")
        if not user:
            logger.warning("未取到 USERNAME，无法用 icacls 收紧密钥文件权限")
            return "未收紧（取不到 USERNAME）"
        try:
            proc = subprocess.run(
                ["icacls", path, "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
                capture_output=True, timeout=10, check=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"icacls 收紧权限失败（文件仍可被同机其他账户读取）: {exc}")
            return "icacls 失败（权限未收紧）"
        if proc.returncode != 0:
            # 之前不看返回码，icacls 失败也会报「已限制」，等于对外谎报安全状态
            detail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()
            logger.warning(f"icacls 返回 {proc.returncode}，密钥文件权限未收紧: {detail}")
            return "icacls 失败（权限未收紧）"
        return f"icacls 已限制为 {user}"
    try:
        os.chmod(path, 0o600)
        return "chmod 0600"
    except OSError as exc:
        logger.warning(f"chmod 0600 失败: {exc}")
        return "chmod 失败（权限未收紧）"


def save_passphrase(passphrase: str) -> None:
    os.makedirs(os.path.dirname(PASSPHRASE_FILE), exist_ok=True)
    with open(PASSPHRASE_FILE, "w") as f:
        json.dump({"passphrase": passphrase}, f, indent=2)
    mode = _restrict_file_permissions(PASSPHRASE_FILE)
    logger.info(f"passphrase 已保存到 {PASSPHRASE_FILE}（权限: {mode}）")


# ============================================================
# Key helpers
# ============================================================

def _strip_key_metadata(keys: dict) -> dict:
    return {k: v for k, v in keys.items() if not k.startswith("_")}


def _key_path_variants(rel_path: str) -> list[str]:
    normalized = rel_path.replace("\\", "/")
    variants: list[str] = []
    for candidate in (rel_path, normalized, normalized.replace("/", "\\"), normalized.replace("/", os.sep)):
        if candidate not in variants:
            variants.append(candidate)
    return variants


def _get_key_info(keys: dict, rel_path: str) -> dict | None:
    if ".." in rel_path.replace("\\", "/").split("/"):
        return None
    for candidate in _key_path_variants(rel_path):
        if candidate in keys and not candidate.startswith("_"):
            return keys[candidate]
    return None


def _save_results(db_files: list, salt_to_dbs: dict, key_map: dict, db_dir: str, out_file: str) -> None:
    _print(f"\n{'=' * 60}")
    _print(f"结果: {len(key_map)}/{len(salt_to_dbs)} salts 找到密钥")

    result: dict = {}
    for rel, _path, sz, salt_hex, _page1 in db_files:
        if salt_hex in key_map:
            result[rel] = {"enc_key": key_map[salt_hex], "salt": salt_hex, "size_mb": round(sz / 1024 / 1024, 1)}
            _print(f"  OK: {rel} ({sz / 1024 / 1024:.1f}MB)")
        else:
            _print(f"  MISSING: {rel} (salt={salt_hex})")

    if not result:
        raise RuntimeError("未能提取到任何密钥")

    result["_db_dir"] = db_dir
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    _print(f"\n密钥保存到: {out_file}")


# ============================================================
# Config Detection（读绿泡泡本地 ini 配置定位数据目录）
# ============================================================

def auto_detect_db_dir() -> str | None:
    """自动检测绿泡泡 db_storage 目录。
    Windows: 读取 %APPDATA%\\Tencent\\xwechat\\config\\*.ini 找到数据根目录，再匹配 xwechat_files\\*\\db_storage。
    macOS: 扫描 ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/*/db_storage。
    """
    candidates: list[str] = []

    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        config_dir = os.path.join(appdata, "Tencent", "xwechat", "config")
        data_roots: list[str] = []
        if os.path.isdir(config_dir):
            for ini_file in glob.glob(os.path.join(config_dir, "*.ini")):
                content = None
                # utf-8-sig 优先：ini 可能带 BOM，BOM 会让 strip() 后的路径判空
                for enc in ("utf-8-sig", "utf-8", "gbk"):
                    try:
                        with open(ini_file, "r", encoding=enc) as f:
                            content = f.read(1024).strip()
                        break
                    except UnicodeDecodeError:
                        continue
                if content and os.path.isdir(content):
                    data_roots.append(content)

        # 回退：新版客户端（4.x）的 ini 可能只写「MyDocument:」这类标记而非绝对路径，
        # 单靠 ini 无法定位数据目录；此时改用系统默认「文档」目录（xwechat_files 所在处）。
        doc_root = os.path.join(os.path.expanduser("~"), "Documents")
        if os.path.isdir(doc_root) and doc_root not in data_roots:
            data_roots.append(doc_root)

        for root in data_roots:
            for match in glob.glob(os.path.join(root, "xwechat_files", "*", "db_storage")):
                if os.path.isdir(match) and match not in candidates:
                    candidates.append(match)

    elif sys.platform == "darwin":
        mac_files = os.path.expanduser("~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files")
        if os.path.isdir(mac_files):
            for match in glob.glob(os.path.join(mac_files, "*", "db_storage")):
                if os.path.isdir(match) and match not in candidates:
                    candidates.append(match)

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # 多账号（多开）时不能依赖 glob 顺序：取 db_storage 最近被写入的那个，
    # 通常即当前活跃账号，与 config.get_files_root() 保持同一套选择策略。
    def _mtime(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    candidates.sort(key=_mtime, reverse=True)
    logger.warning("检测到多个绿泡泡数据目录，已选用最近活跃的一个: %s（候选: %s）",
                   candidates[0], ", ".join(candidates))
    return candidates[0]


# ============================================================
# Database Decryption
# ============================================================

def _decrypt_page(enc_key: bytes, page_data: bytes, pgno: int) -> bytes:
    iv = page_data[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
    if pgno == 1:
        encrypted = page_data[SALT_SZ: PAGE_SZ - RESERVE_SZ]
        decrypted = aes_cbc_decrypt(enc_key, iv, encrypted)
        return bytes(SQLITE_HDR + decrypted + b"\x00" * RESERVE_SZ)
    else:
        encrypted = page_data[: PAGE_SZ - RESERVE_SZ]
        decrypted = aes_cbc_decrypt(enc_key, iv, encrypted)
        return decrypted + b"\x00" * RESERVE_SZ


def _decrypt_database(db_path: str, out_path: str, enc_key: bytes) -> bool:
    file_size = os.path.getsize(db_path)
    total_pages = file_size // PAGE_SZ
    if file_size % PAGE_SZ != 0:
        _print(f"  [WARN] 文件大小 {file_size} 不是 {PAGE_SZ} 的倍数")
        total_pages += 1

    with open(db_path, "rb") as fin:
        page1 = fin.read(PAGE_SZ)
    if len(page1) < PAGE_SZ:
        _print("  [ERROR] 文件太小")
        return False

    salt = page1[:SALT_SZ]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)
    p1_hmac_data = page1[SALT_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
    p1_stored_hmac = page1[PAGE_SZ - HMAC_SZ: PAGE_SZ]
    hm = hmac_mod.new(mac_key, p1_hmac_data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    if hm.digest() != p1_stored_hmac:
        _print(f"  [ERROR] Page 1 HMAC 验证失败! salt: {salt.hex()}")
        return False

    _print(f"  HMAC OK, {total_pages} pages")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    with open(db_path, "rb") as fin, open(out_path, "wb") as fout:
        for pgno in range(1, total_pages + 1):
            page = fin.read(PAGE_SZ)
            if len(page) < PAGE_SZ:
                if len(page) > 0:
                    page = page + b"\x00" * (PAGE_SZ - len(page))
                else:
                    break
            fout.write(_decrypt_page(enc_key, page, pgno))
            if pgno % 10000 == 0:
                _print(f"  进度: {pgno}/{total_pages} ({100 * pgno / total_pages:.1f}%)")

    return True


def _merge_encrypted_wal(db_path: str, wal_path: str, enc_key: bytes) -> int:
    """把加密 WAL 的已提交帧解密后合并进已解密的数据库文件（等价于手动 checkpoint）。

    WCDB / SQLCipher 的 WAL 帧头（24 字节）是明文，页数据与主库一样按 4096 字节页加密。
    只解密 .db、完全忽略 -wal，会在 WAL 真的还压着未 checkpoint 的数据时丢消息，所以这里
    显式合并；但 WeChat 的 WCDB 也会对主库做 checkpoint 而不重置 WAL，此时残留的 WAL
    记录的是更早的库状态（实测 message_0.db 的 WAL 记录 47724 页，而主库文件已有 50215
    页），直接套用会把库回退成旧状态。因此做三重保护：

    1. WAL 最后一个提交帧记录的库大小不得小于主库文件页数，否则判定为过期残留，跳过；
    2. 只应用最后一个「提交帧」及其之前的帧（未提交尾部丢弃），与 SQLite 恢复语义一致；
    3. 合并后做一次 ``quick_check`` 结构校验，不通过则由调用方回退为仅解密数据库本体。

    返回应用的帧数；0 表示没有可用帧或判定为过期；-1 表示合并后校验失败（需回退）。
    """
    if not os.path.isfile(wal_path):
        return 0
    try:
        if os.path.getsize(wal_path) <= 32:
            return 0
        with open(wal_path, "rb") as f:
            data = f.read()
    except OSError as exc:
        logger.warning(f"读取 WAL 失败，跳过合并: {exc}")
        return 0

    frame_size = 24 + PAGE_SZ
    pages: list[tuple[int, bytes]] = []
    last_commit = -1
    committed_pages = 0
    for offset in range(32, len(data), frame_size):
        frame = data[offset:offset + frame_size]
        if len(frame) < frame_size:
            break
        pgno = struct.unpack(">I", frame[0:4])[0]
        db_size = struct.unpack(">I", frame[4:8])[0]
        if pgno == 0:
            continue
        pages.append((pgno, _decrypt_page(enc_key, frame[24:], pgno)))
        if db_size:
            last_commit = len(pages) - 1
            committed_pages = db_size
    if last_commit < 0 or committed_pages <= 0:
        return 0

    try:
        db_pages = os.path.getsize(db_path) // PAGE_SZ
    except OSError:
        return 0
    if committed_pages < db_pages:
        logger.info(
            f"WAL 记录 {committed_pages} 页 < 数据库文件 {db_pages} 页，"
            f"判定为已 checkpoint 的过期残留，未合并: {wal_path}"
        )
        return 0

    applied = pages[: last_commit + 1]
    try:
        with open(db_path, "r+b") as f:
            for pgno, page in applied:
                f.seek((pgno - 1) * PAGE_SZ)
                f.write(page)
    except OSError as exc:
        logger.warning(f"合并 WAL 到数据库失败: {exc}")
        return 0
    if not _db_quick_check(db_path):
        # 合并结果不合法（帧被误读 / WAL 与主库不同代），交给调用方回退
        return -1
    return len(applied)


def _db_quick_check(path: str) -> bool:
    """结构校验：确认解密（可能已合并 WAL）后的文件确实是一个可读的 SQLite 库。"""
    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return conn.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return False


def decrypt_all(db_dir: str, out_dir: str, keys_file: str) -> dict:
    _print("=" * 60)
    _print(f"  WeChat 数据库解密器 ({sys.platform})")
    _print("=" * 60)

    if not os.path.exists(keys_file):
        _print(f"[ERROR] 密钥文件不存在: {keys_file}")
        _print("请先运行: wechat-init")
        sys.exit(1)

    with open(keys_file, encoding="utf-8") as f:
        keys = _strip_key_metadata(json.load(f))
    _print(f"\n加载 {len(keys)} 个数据库密钥")
    os.makedirs(out_dir, exist_ok=True)

    db_files: list[tuple[str, str, int]] = []
    for root, _dirs, files in os.walk(db_dir):
        for fname in files:
            if fname.endswith(".db") and not fname.endswith("-wal") and not fname.endswith("-shm"):
                path = os.path.join(root, fname)
                rel = os.path.relpath(path, db_dir)
                db_files.append((rel, path, os.path.getsize(path)))
    db_files.sort(key=lambda x: x[2])
    _print(f"找到 {len(db_files)} 个数据库文件\n")

    success = failed = 0
    total_bytes = 0
    for rel, path, sz in db_files:
        key_info = _get_key_info(keys, rel)
        if not key_info:
            _print(f"SKIP: {rel} (无密钥)")
            failed += 1
            continue
        enc_key = bytes.fromhex(key_info["enc_key"])
        out_path = os.path.join(out_dir, rel)
        _print(f"解密: {rel} ({sz / 1024 / 1024:.1f}MB) ...", end=" ")
        if _decrypt_database(path, out_path, enc_key):
            wal_frames = _merge_encrypted_wal(out_path, path + "-wal", enc_key)
            if wal_frames < 0:
                logger.warning(f"合并 WAL 后校验未通过，已回退为仅解密数据库本体: {rel}")
                _decrypt_database(path, out_path, enc_key)
                wal_frames = 0
            if wal_frames:
                _print(f"  [+] 已合并 WAL 已提交帧: {wal_frames}")
            try:
                import sqlite3
                conn = sqlite3.connect(out_path)
                tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                conn.close()
                _print(f"  OK! 表: {', '.join(t[0] for t in tables[:5])}")
                success += 1
                total_bytes += sz
            except Exception as e:
                _print(f"  [WARN] SQLite 验证失败: {e}")
                failed += 1
        else:
            failed += 1

    _print(f"\n{'=' * 60}")
    _print(f"结果: {success} 成功, {failed} 失败, 共 {len(db_files)} 个")
    _print(f"解密数据量: {total_bytes / 1024 / 1024 / 1024:.1f}GB")
    return {"success": success, "failed": failed, "total": len(db_files), "total_bytes": total_bytes}


# ============================================================
# CLI
# ============================================================

def cmd_extract(args: argparse.Namespace) -> None:
    db_dir = args.db_dir or auto_detect_db_dir()
    if not db_dir:
        _print("[ERROR] 未能自动检测绿泡泡数据库目录，请使用 --db-dir 手动指定")
        sys.exit(1)
    _print(f"[*] 数据库目录: {db_dir}")

    db_files, salt_to_dbs = collect_db_files(db_dir)
    if not db_files:
        _print(f"[ERROR] 在 {db_dir} 未找到可解密的 .db 文件")
        sys.exit(1)

    out_file = args.output

    # 第 1 级：已保存的 passphrase（若通过 set-passphrase 手动填入过）
    passphrase_hex = load_passphrase()
    if passphrase_hex:
        _print("[*] 使用已保存的 passphrase 派生密钥（PBKDF2，约需 30-60 秒）...")
        try:
            key_map = _derive_keys_from_passphrase(
                bytes.fromhex(passphrase_hex), db_files, salt_to_dbs
            )
        except (ValueError, TypeError) as exc:
            _print(f"[!] 已保存的 passphrase 无法解析（{exc}），改走内存扫描")
            key_map = {}
        if key_map:
            _print(f"[+] passphrase 派生成功: {len(key_map)}/{len(salt_to_dbs)} 密钥")
            _save_results(db_files, salt_to_dbs, key_map, db_dir, out_file)
            if args.decrypt:
                decrypted_dir = getattr(args, "decrypted_dir", "decrypted") or "decrypted"
                decrypt_all(db_dir, decrypted_dir, out_file)
            return
        _print("[!] 已保存的 passphrase 无效，改走内存扫描")

    # 第 2 级：内存扫描 raw key（仅对仍缓存明文密钥的绿泡泡老版本有效，Windows/macOS 4.0.x 支持）
    if sys.platform == "win32":
        _scan_memory_raw_key(db_dir, out_file)
    elif sys.platform == "darwin":
        # 优先检测微信版本：4.1+ 直接进入 LLDB 捕获，避免对 4.1+ 遍历不可读内存区触发 SIGBUS
        is_wechat_41_plus = True
        try:
            r = subprocess.run(
                ["defaults", "read", "/Applications/WeChat.app/Contents/Info.plist", "CFBundleShortVersionString"],
                capture_output=True, text=True, timeout=5,
            )
            ver = r.stdout.strip()
            if ver and ver.startswith("4.0."):
                is_wechat_41_plus = False
        except Exception:
            pass

        key_map = None
        if not is_wechat_41_plus:
            try:
                key_map = _scan_memory_raw_key_macos(db_dir, out_file)
            except Exception as e:
                logger.info(f"4.0.x 内存扫描未获取到明文密钥（{e}），尝试 LLDB 断点捕获...")

        if not key_map:
            _print()
            _print("=" * 60)
            _print("  检测到微信 4.1+，正在通过 LLDB 断点捕获口令原料")
            _print("=" * 60)
            target_pid = getattr(args, "pid", None) or _find_wechat_pid_macos()
            if target_pid:
                _print(f"[*] 目标微信进程 PID: {target_pid}")
            _print("💡 操作提示：")
            _print("   请在 Mac 微信主界面操作：点击左下角「设置」->「账号与存储」->「退出登录」（切勿退出整个微信程序）")
            _print("   然后使用手机微信重新扫码/确认登录（触发密钥派生）")
            to_val = getattr(args, "timeout", DEFAULT_LLDB_TIMEOUT)
            if to_val and to_val > 0:
                _print(f"等待最多 {to_val} 秒...")
            else:
                _print("持续监听中，等待微信扫码登录...")
            cached_key_map: dict[str, str] = {}

            def _test_candidate_passphrase(cand_hex: str) -> bool:
                """对捕获到的候选 passphrase 立即在内存中做快速 PBKDF2 派生并抽样校验。"""
                try:
                    cand_bytes = bytes.fromhex(cand_hex)
                except Exception:
                    return False
                # 取前 3 个活跃库做快速校验，任一库校验通过即说明该口令为有效 DB passphrase
                test_salts = list(salt_to_dbs.keys())[:3]
                for s_hex in test_salts:
                    s_bytes = bytes.fromhex(s_hex)
                    test_key = hashlib.pbkdf2_hmac("sha512", cand_bytes, s_bytes, 256000, dklen=KEY_SZ)
                    for _rel, _path, _sz, s, page1 in db_files:
                        if s == s_hex and verify_enc_key(test_key, page1):
                            cached_key_map[s_hex] = test_key.hex()
                            return True
                return False

            try:
                passphrase_hex = capture_passphrase_lldb(
                    timeout=to_val,
                    pid=getattr(args, "pid", None),
                    verify_callback=_test_candidate_passphrase,
                )
                _print(f"[+] passphrase 捕获并自证通过: {passphrase_hex[:8]}...（已截断）")
                # 只有验证通过后才落盘保存，避免把误抓的口令持久化污染环境
                save_passphrase(passphrase_hex)
                _print("\n[*] 开始完整 PBKDF2 派生密钥（约需 30-60 秒）...")
                key_map = _derive_keys_from_passphrase(bytes.fromhex(passphrase_hex), db_files, salt_to_dbs)
                if not key_map:
                    _print("[ERROR] PBKDF2 派生后未能验证任何密钥，请检查数据库目录")
                    _print("💡 提示：本次断点命中可能未完全匹配数据库口令。请删除 ~/.wcdb-key-tool/wechat-passphrase.json 后重新运行重试。")
                    sys.exit(1)
                _save_results(db_files, salt_to_dbs, key_map, db_dir, out_file)
            except LLDBPassphraseCaptureError as lldb_err:
                _print(f"[ERROR] LLDB 捕获失败: {lldb_err}")
                _print("    提示: 可通过 `wechat-init --passphrase <64位hex>` 直接指定口令跳过断点捕获。")
                sys.exit(1)
    else:
        _print(f"[*] 当前平台 ({sys.platform}) 微信 4.1+ 需配置 passphrase。")
        _print("    请使用 `wechat-init set-passphrase <64位hex>` 或配置 wechat-passphrase.json")
        sys.exit(1)
    if args.decrypt:
        decrypted_dir = getattr(args, "decrypted_dir", "decrypted") or "decrypted"
        decrypt_all(db_dir, decrypted_dir, out_file)


def cmd_capture_experimental(args: argparse.Namespace) -> None:
    """[研究性备用] 尝试断点捕获 passphrase，见文件头说明。"""
    _print("!" * 60)
    _print("! 这是研究性备用方案，默认不影响 extract !")
    _print("!" * 60)
    _print()
    _print("请在捕获期间于绿泡泡中执行：设置 -> 退出登录 -> 重新登录")
    _print(f"工具将等待最多 {args.timeout} 秒...")
    try:
        passphrase_hex = capture_passphrase_experimental(pid=args.pid, timeout=args.timeout)
    except ExperimentalCaptureError as e:
        _print(f"[ERROR] {e}")
        sys.exit(1)
    _print(f"[?] 断点命中并解析出一段 32 字节数据: {passphrase_hex[:8]}...（已截断）")
    save_passphrase(passphrase_hex)
    _print("[*] 已保存。运行 extract 会尝试用它派生密钥并做 HMAC 校验——校验通过才说明真的抓对了。")


def cmd_set_passphrase(args: argparse.Namespace) -> None:
    """手动写入通过其他手段（x64dbg/WinDbg/Frida 等）拿到的 passphrase。"""
    ph = args.passphrase.strip().lower()
    if len(ph) != 64 or not re.fullmatch(r"[0-9a-f]{64}", ph):
        _print("[ERROR] passphrase 必须是 64 位十六进制字符串（32 字节）")
        sys.exit(1)
    save_passphrase(ph)
    _print("[+] 已保存，下次运行 extract 会优先尝试用它派生密钥")


def cmd_decrypt(args: argparse.Namespace) -> None:
    db_dir = args.db_dir
    if not db_dir and os.path.exists(args.keys):
        try:
            with open(args.keys, encoding="utf-8") as f:
                db_dir = json.load(f).get("_db_dir")
        except Exception:
            pass
    db_dir = db_dir or auto_detect_db_dir()
    if not db_dir:
        _print("[ERROR] 未能确定数据库目录，请使用 --db-dir 手动指定")
        sys.exit(1)
    decrypt_all(db_dir, args.output, args.keys)


def main() -> None:
    parser = argparse.ArgumentParser(description=f"wcdb-key-tool ({sys.platform}) — 绿泡泡数据库密钥提取与解密工具")
    parser.add_argument("--verbose", "-v", action="store_true")
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True

    extract_cmd = sub.add_parser("extract", help="提取数据库密钥（支持 passphrase PBKDF2 派生 / 老版本内存扫描）")
    extract_cmd.add_argument("--db-dir", help="绿泡泡 db_storage 目录（默认自动检测）")
    extract_cmd.add_argument("--output", default="all_keys.json")
    extract_cmd.add_argument("--decrypt", action="store_true")
    extract_cmd.add_argument(
        "--decrypted-dir", default="decrypted",
        help="配合 --decrypt 使用，解密后数据库的输出目录（默认 decrypted）",
    )

    if sys.platform == "win32":
        capture_cmd = sub.add_parser(
            "capture-experimental",
            help="[研究性备用] 断点捕获 passphrase，见文件头说明",
        )
        capture_cmd.add_argument("--pid", type=int, help="绿泡泡进程 PID（默认自动查找）")
        capture_cmd.add_argument("--timeout", type=int, default=DEFAULT_CDB_TIMEOUT)
    elif sys.platform == "darwin":
        capture_cmd = sub.add_parser(
            "capture-lldb",
            help="使用 LLDB 断点捕获 4.1+ passphrase",
        )
        capture_cmd.add_argument("--timeout", type=int, default=DEFAULT_LLDB_TIMEOUT)

    passphrase_cmd = sub.add_parser("set-passphrase", help="手动填入通过其他手段获取的 passphrase")
    passphrase_cmd.add_argument("passphrase", help="64 位十六进制字符串（32 字节）")

    decrypt_cmd = sub.add_parser("decrypt", help="解密数据库（需要已有密钥文件）")
    decrypt_cmd.add_argument("--db-dir")
    decrypt_cmd.add_argument("--keys", default="all_keys.json")
    decrypt_cmd.add_argument("--output", default="decrypted")

    args = parser.parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.command == "extract":
        cmd_extract(args)
    elif args.command == "capture-experimental":
        cmd_capture_experimental(args)
    elif args.command == "capture-lldb":
        _print("请在微信中执行：设置 -> 退出登录 -> 重新登录（触发密钥派生）")
        ph = capture_passphrase_lldb(timeout=args.timeout)
        save_passphrase(ph)
        _print(f"[+] 已保存 passphrase: {ph[:8]}...")
    elif args.command == "set-passphrase":
        cmd_set_passphrase(args)
    elif args.command == "decrypt":
        cmd_decrypt(args)


if __name__ == "__main__":
    main()
