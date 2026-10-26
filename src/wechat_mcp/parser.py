import re
import logging

logger = logging.getLogger(__name__)

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

try:
    import zstandard as _zstd
    _ZSTD_CTX = _zstd.ZstdDecompressor()
    HAS_ZSTD = True
except ImportError:
    _ZSTD_CTX = None
    HAS_ZSTD = False

def maybe_zstd_decompress(data) -> bytes:
    """若是 zstd 压缩包则解压，否则原样返回"""
    if not data:
        return b""
    if isinstance(data, str):
        data = data.encode("utf-8", errors="ignore")
    if data[:4] != ZSTD_MAGIC:
        return bytes(data)
    if not HAS_ZSTD:
        return b""
    try:
        return _ZSTD_CTX.decompress(data)
    except Exception as exc:
        logger.warning(f"zstd 解压失败（输入 {len(data)} 字节）: {exc}")
        return b""

TYPE_LABELS = {
    1: "文本",
    3: "图片",
    34: "语音",
    37: "好友请求",
    42: "名片",
    43: "视频",
    47: "表情",
    48: "位置",
    49: "应用消息",
    50: "通话",
    51: "视频号",
    52: "视频号",
    10000: "系统",
    10002: "系统",
}

APPMSG_LABELS = {
    1: "链接",
    2: "相册",
    3: "图片",
    4: "视频",
    5: "链接",
    6: "文件",
    7: "位置",
    8: "表情",
    9: "图文",
    10: "音乐",
    11: "音乐",
    12: "名片",
    14: "打招呼",
    15: "频道",
    16: "群接龙",
    17: "实时语音",
    18: "企业绿泡泡",
    19: "聊天记录",
    20: "卡券",
    24: "群体预览",
    25: "群公告",
    33: "小程序",
    34: "小程序",
    35: "小程序",
    36: "视频号",
    57: "引用",
    62: "拍一拍",
}

def type_label(local_type) -> str:
    """把微信的复合 local_type 映射成人类可读标签。

    微信 4.x 的 ``local_type`` 低 32 位是基础类型，高 32 位是 appmsg 子类型；
    例如 ``6 << 32 | 49`` 是文件、``57 << 32 | 49`` 是引用。面板、Agent 与脚本
    共用这一份判定，避免各自维护映射表。
    """
    try:
        t = int(local_type or 0)
    except (TypeError, ValueError):
        return "其他"
    base = t & 0xFFFFFFFF
    app_type = (t >> 32) if t > 0xFFFFFFFF else 0
    if base == 49 and app_type:
        return APPMSG_LABELS.get(app_type, "应用消息")
    return TYPE_LABELS.get(base, "其他")


def _clean_xml_text(value: str) -> str:
    """去掉 CDATA 包裹并还原常见 XML 实体"""
    text = (value or "").strip()
    text = re.sub(r"^<!\[CDATA\[(.*?)\]\]>$", r"\1", text, flags=re.DOTALL)
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .strip()
    )


# 标签属性段：允许属性值内部出现 '>'（如 <a href="x>y">）。用引号感知的
# 交替式匹配，避免 [^>]* 在属性值里的第一个 '>' 处提前截断，把 "x>y" 之后
# 的垃圾当成正文。
_ATTRS = r"""(?:"[^"]*"|'[^']*'|[^>"'])*"""


def extract_tag_from_xml(xml_text: str, tag: str) -> str:
    """提取指定标签内的首个非空文本（跳过自闭合与空标签）"""
    if not xml_text:
        return ""
    # 先剔除自闭合标签（<tag/> 与 <tag />）。它们没有闭合标签，正则的非重叠
    # 扫描会从它一路吞到后面同名的真标签，导致紧随其后的真标签被整体跳过。
    # \b 保证 <tagattr/> 这类同名前缀不会被误当成 <tag/>。
    cleaned = re.sub(rf"<{tag}\b(?:{_ATTRS})?/>", "", xml_text)
    # 正文遇到同名开标签即停止：脏数据里缺闭合标签的 <title>a<title>b</title>
    # 否则会把两个标签之间的内容整段吞进来。
    pattern = rf"<{tag}\b({_ATTRS})>((?:(?!<{tag}\b).)*?)</{tag}>"
    for match in re.finditer(pattern, cleaned, re.DOTALL):
        value = _clean_xml_text(match.group(2))
        if value:
            return value
    return ""


def extract_title_from_xml(xml_text: str) -> str:
    """从 appmsg XML 中提取 <title> 内容"""
    return extract_tag_from_xml(xml_text, "title")

def _summarize_quoted(quoted: str) -> str:
    """被引用的消息本身若是 appmsg，其 <content> 里嵌的是整段原始 XML。

    此时再解析一层，取出可读的 <title>/<des>，避免把整段 XML 当成引用原文
    直接塞进正文。解析不出可读字段时按嵌套根标签退化为类型占位。
    """
    raw = quoted or ""
    text = raw.lstrip()
    # 群聊中被引用内容可能带 "wxid_xxx: " 发送者前缀；仅当剥掉前缀后确实以
    # XML 开头时才剥离，避免误伤形如 "abc123: 你好" 的正常引用文本。
    stripped = re.sub(r"^[A-Za-z0-9_.\-@]{4,}:\s*", "", text, count=1)
    if stripped.startswith("<?xml") or stripped.startswith("<msg"):
        text = stripped
    elif not (text.startswith("<?xml") or text.startswith("<msg")):
        return raw
    for tag in ("title", "des", "content"):
        value = extract_tag_from_xml(text, tag)
        value = (value or "").strip()
        if value and not value.startswith("<"):
            return value
    for token, label in (("<img", "[图片]"), ("<video", "[视频]"), ("<emoji", "[表情]")):
        if token in text:
            return label
    return "[多媒体消息]"


_EMOJI_MD5_RE = re.compile(rb'<emoji\b[^>]*?\bmd5\s*=\s*"([0-9a-fA-F]{32})"', re.IGNORECASE)


def extract_emoticon_md5(raw_content, raw_source=None) -> str | None:
    """从表情消息（local_type=47）的原始 XML 里取出表情 md5。

    微信只在 XML 的 ``<emoji ... md5="...">`` 里给出这个标识（图片本体按 md5
    命名存在账号目录的 business/emoticon/Persist 下），所以要让面板展示表情，
    必须先把 md5 存进中间库 —— 只存 "[表情]" 标签是拿不到图的。
    """
    raw = maybe_zstd_decompress(raw_content if raw_content else raw_source)
    if not raw:
        return None
    if isinstance(raw, str):
        raw = raw.encode("utf-8", errors="ignore")
    match = _EMOJI_MD5_RE.search(raw)
    return match.group(1).decode("ascii").lower() if match else None


def build_content(local_type: int, raw_content, raw_source) -> str:
    """将 raw_content / raw_source 根据 local_type 构建成可读字符串"""
    if local_type == 1:
        if isinstance(raw_content, bytes):
            text = maybe_zstd_decompress(raw_content).decode("utf-8", errors="ignore")
        else:
            text = str(raw_content or "")
        # 群聊消息会把发送者编码成 `<sender_id>: ` 前缀（实测与 sender_username 一致、
        # 私聊从不出现），属冗余标记，清理后正文更可读。除 wxid_ 外，公众号/服务号
        # 会话还会出现 gh_ 与 <数字>@openim 形式，一并清理。
        return re.sub(
            r"^(?:wxid_[A-Za-z0-9_-]{4,}|gh_[A-Za-z0-9_-]{4,}|[0-9]{6,}@openim):\s*", "", text
        )

    # 系统消息（撤回 / 入群提示等）：正文多为可读文本，部分 type 为 sysmsg XML。
    # v4 的 local_type 可能是「高位子类型 + 低位基础类型」的复合值，
    # 与统计侧 (_BASE_TYPE_MASK) 同口径取低 32 位判断；纯值行为不变。
    if (int(local_type or 0) & 0xFFFFFFFF) in (10000, 10002):
        raw = maybe_zstd_decompress(raw_content or raw_source)
        text = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw or "")
        text = text.strip()
        if "<sysmsg" in text:
            for tag in ("content", "text", "replacemsg", "plain"):
                value = extract_tag_from_xml(text, tag)
                if value:
                    return value
        return text or "[系统]"

    label = TYPE_LABELS.get(local_type)
    if label:
        return f"[{label}]"

    # appmsg 处理
    decompressed = maybe_zstd_decompress(raw_content or raw_source)
    xml_text = decompressed.decode("utf-8", errors="ignore") if decompressed else ""
    app_type = (local_type >> 32) if local_type > 0xFFFFFFFF else 0
    sub_label = APPMSG_LABELS.get(app_type, "消息")

    if xml_text:
        # 引用消息：<title> 是本次回复内容，<refermsg> 里才是被引用原文与作者
        if app_type == 57:
            reply = extract_tag_from_xml(xml_text, "title")
            quoted = _summarize_quoted(extract_tag_from_xml(xml_text, "content"))
            who = extract_tag_from_xml(xml_text, "displayname")
            if quoted:
                head = f"[引用 {who}: {quoted}]" if who else f"[引用: {quoted}]"
                return f"{head} {reply}" if reply else head
            if reply:
                return f"[引用] {reply}"

        title = extract_tag_from_xml(xml_text, "title")
        if title:
            return f"[{sub_label}] {title}"
        if "<appmsg" in xml_text:
            return f"[{sub_label}]"

    return f"[{sub_label}]" if app_type else "[多媒体消息]"
