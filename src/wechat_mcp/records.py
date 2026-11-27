"""「聊天记录」合并转发消息（appmsg 子类型 19）的展开。

微信把「聊天记录」编码成 appmsg：`local_type` 高 32 位是子类型 19，正文里嵌一段

    <recorditem><![CDATA[<recordinfo>…<datalist><dataitem …>…</dataitem></datalist>
    </recordinfo>]]></recorditem>

被合并的各条消息就在 `<datalist>` 里。

关键点：**原始 XML 不在本地索引里**。ETL 只存 `build_content()` 之后的
`[聊天记录] 标题`，所以必须回解密快照（`~/.wechat_mcp/decrypted/message/*.db`）
读那一行的原始 `message_content`（zstd 压缩），解压后再解析。

消歧：`local_id` 只在单个 `Msg_<hash>` 表内唯一，不同会话的表可以重号，
因此必须同时用 `create_time` 匹配（只按 local_id 查会命中别的会话的同号记录）。
"""

import html
import re
import sqlite3
from typing import Any, Dict, List, Optional

from .config import get_decrypted_dir
from .parser import maybe_zstd_decompress

# recorditem 里的 datatype → 可读类型。未收录的值退化为 type_<n>。
_ITEM_KINDS: Dict[int, str] = {
    1: "text",
    2: "image",
    3: "audio",
    4: "video",
    5: "link",
    6: "location",
    7: "card",
    8: "file",
    17: "chatrecord",
    19: "chatrecord",
    21: "mini_program",
    22: "video_channel",
    23: "music",
    24: "sticker",
    26: "note",
    27: "voip",
}

_RECORDITEM_RE = re.compile(r"<recorditem\b[^>]*>(.*?)</recorditem>", re.DOTALL)

# 渲染正文时的中文类型标签（缺失 datadesc 时用，如 [文件] 报告.pdf）
_ITEM_KINDS_ZH = {
    1: "文本",
    2: "图片",
    3: "语音",
    4: "视频",
    5: "链接",
    6: "位置",
    7: "名片",
    8: "文件",
    17: "聊天记录",
    19: "聊天记录",
    21: "小程序",
    22: "视频号",
    23: "音乐",
    24: "表情",
    26: "笔记",
    27: "通话",
}

_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)
_ITEM_RE = re.compile(r"<dataitem\b([^>]*)>(.*?)</dataitem>", re.DOTALL)
_ATTR_RE = re.compile(r'([A-Za-z_][\w-]*)\s*=\s*"([^"]*)"')
_COUNT_RE = re.compile(r'<datalist\b[^>]*\bcount="(\d+)"')


def _tag_text(xml: str, tag: str) -> str:
    """取 <tag>…</tag> 的文本（首个匹配），并还原 XML 实体（&#x20; / &amp; 等）。"""
    m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", xml, re.DOTALL)
    if not m:
        return ""
    return html.unescape(m.group(1).strip())


def _opt(value: str) -> Optional[str]:
    value = (value or "").strip()
    return value or None


def parse_chat_record(raw: str) -> Dict[str, Any]:
    """把原始 message_content 里的 recorditem 解析成结构化条目。"""
    m = _RECORDITEM_RE.search(raw or "")
    if not m:
        return {"error": "该消息正文里没有 recorditem（不是聊天记录合并消息）"}

    body = m.group(1)
    cdata = _CDATA_RE.search(body)
    xml = cdata.group(1) if cdata else body
    # 部分客户端的 recorditem 是「转义一层」的（&lt;recordinfo&gt;…），
    # 直接按标签匹配会一个条目都取不到，这里还原一次再解析。
    if "<dataitem" not in xml and "&lt;" in xml:
        xml = html.unescape(xml)

    items: List[Dict[str, Any]] = []
    for attrs, inner in _ITEM_RE.findall(xml):
        a = dict(_ATTR_RE.findall(attrs))
        try:
            datatype = int(a.get("datatype") or 0)
        except ValueError:
            datatype = 0
        items.append(
            {
                "type": _ITEM_KINDS.get(datatype, f"type_{datatype}"),
                "datatype": datatype,
                "desc": _tag_text(inner, "datadesc"),
                "title": _tag_text(inner, "datatitle"),
                "sender": _tag_text(inner, "sourcename"),
                "time": _tag_text(inner, "sourcetime"),
                "format": _opt(_tag_text(inner, "datafmt")),
                "duration": _opt(_tag_text(inner, "duration")),
                "size": _opt(_tag_text(inner, "datasize")),
                # 图片/视频条目的内容 MD5，便于宿主进一步取原文件
                "md5": _opt(_tag_text(inner, "fullmd5")) or _opt(_tag_text(inner, "thumbfullmd5")),
                "src_create_time": _opt(_tag_text(inner, "srcMsgCreateTime")),
            }
        )

    declared = _COUNT_RE.search(xml)
    return {
        "title": _tag_text(xml, "title"),
        "desc": _tag_text(xml, "desc"),
        "count": len(items),
        "declared_count": int(declared.group(1)) if declared else None,
        "items": items,
    }


def render_record_text(raw: str, max_items: int = 60, max_chars: int = 2000) -> str:
    """把「聊天记录」合并消息渲染成多行纯文本，供 ETL 直接写入索引正文。

    返回空串表示正文里没有 recorditem（不是聊天记录），调用方自行兜底。
    子条目数量与总字数都做上限，避免几十页的转发记录把正文撑爆。
    """
    parsed = parse_chat_record(raw)
    if parsed.get("error"):
        return ""

    items: List[Dict[str, Any]] = parsed.get("items") or []
    if not items:
        return ""

    total = parsed.get("declared_count") or len(items)
    title = (parsed.get("title") or "").strip() or "聊天记录"
    lines = [f"[聊天记录] {title}（共 {total} 条）"]
    for item in items[:max_items]:
        desc = (item.get("desc") or "").strip()
        if not desc:
            kind = _ITEM_KINDS_ZH.get(item.get("datatype"), "")
            label = (item.get("title") or "").strip()
            if kind and label:
                desc = f"[{kind}] {label}"
            elif kind:
                desc = f"[{kind}]"
            else:
                desc = f"[{item.get('type') or '消息'}]"
        desc = re.sub(r"\s*\n\s*", " ⏎ ", desc)
        sender = (item.get("sender") or "").strip()
        lines.append(f"{sender}: {desc}" if sender else desc)
    omitted = max(0, (total or 0) - min(len(items), max_items))
    if omitted:
        lines.append(f"…（已省略 {omitted} 条）")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def find_raw_content(source_file: str, local_id: int, create_time: int) -> Optional[str]:
    """回解密快照的源库取该消息的原始 message_content（zstd 解压后）。

    local_id 只在单个 Msg_<hash> 表内唯一，故必须配合 create_time 消歧。
    源库缺失 / 该行已被清理时返回 None。
    """
    if not source_file:
        return None
    db_path = get_decrypted_dir() / "message" / source_file
    if not db_path.exists():
        return None

    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if r[0].startswith("Msg_")
        ]
        for tbl in tables:
            try:
                row = conn.execute(
                    f"SELECT message_content FROM '{tbl}' WHERE local_id = ? AND create_time = ?",
                    (local_id, create_time),
                ).fetchone()
            except sqlite3.Error:
                continue
            if row is None or row[0] is None:
                continue
            raw = maybe_zstd_decompress(row[0])
            text = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw)
            if text.strip():
                return text
    finally:
        conn.close()
    return None


def get_chat_record(message_id: int, include_raw: bool = False) -> Dict[str, Any]:
    """展开指定消息的聊天记录内容（只读）。"""
    from .database import get_connection

    conn = get_connection()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, source_file, local_id, create_time, local_type, content "
            "FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return {"error": f"Message id {message_id} not found"}

    local_type = row["local_type"] or 0
    app_type = (local_type >> 32) if local_type > 0xFFFFFFFF else 0
    if app_type != 19 and "[聊天记录]" not in (row["content"] or ""):
        return {
            "error": f"Message id {message_id} is not a chat-record message "
            f"(local_type={local_type})"
        }

    raw = find_raw_content(row["source_file"], row["local_id"], row["create_time"])
    if not raw:
        return {"error": "无法从解密快照读取该消息原文（源库缺失或该行已被清理）"}

    parsed = parse_chat_record(raw)
    if "error" in parsed:
        return parsed

    result: Dict[str, Any] = {"success": True, "message_id": message_id}
    result.update(parsed)
    if include_raw:
        result["raw_xml"] = raw[:20000]
    return result
