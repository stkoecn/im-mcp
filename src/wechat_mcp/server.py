import datetime
import functools
import re
import sqlite3
from pathlib import Path
from typing import Optional, List, Dict, Any

from mcp.server.fastmcp import FastMCP

from .config import DB_PATH, BASE_DIR
from .database import get_connection, init_schema, release_connections, sync_data
from .images import get_image_for_message

# 初始化 FastMCP 服务器
mcp = FastMCP("wechat-mcp")


def _db_tool(func):
    """工具装饰器：无论正常返回还是抛异常，退出时都释放本线程的数据库连接"""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        finally:
            release_connections()

    return wrapper

def _check_db_initialized() -> Optional[str]:
    """检查绿泡泡本地数据库与索引是否已初始化完成"""
    if not DB_PATH.exists():
        return f"绿泡泡本地索引数据库未找到 ({DB_PATH})。请先以管理员身份在终端运行 `wechat-init` 完成解密与索引构建。"
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'")
        if not cur.fetchone():
            conn.close()
            return "绿泡泡数据库存在但数据表未初始化。请在终端运行 `wechat-init --etl-only` 重建索引。"
        # 老版本建的库可能缺少后续新增的表（chatroom_members / media_files /
        # favorites / emoticons / avatars），会让对应工具抛裸 OperationalError。init_schema
        # 是幂等的 CREATE TABLE IF NOT EXISTS，这里直接补齐（表为空，需跑 ETL 才有数据）。
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        have = {r[0] for r in cur.fetchall()}
        conn.close()
        if {"chatroom_members", "media_files", "favorites", "emoticons", "avatars"} - have:
            init_schema()
        return None
    except Exception as e:
        return f"绿泡泡数据库无法访问: {e}。请尝试重新运行 `wechat-init`。"

# 微信各版本 create_time 单位不一致：大于该阈值视为毫秒时间戳，否则视为秒
_MS_THRESHOLD = 10**11


def _ts_unit(ts: int) -> int:
    """判断时间戳单位：1000=毫秒，1=秒"""
    return 1000 if ts and ts > _MS_THRESHOLD else 1


def _normalize_ts(ts: int) -> float:
    """将秒级/毫秒级时间戳统一归一化为「秒」"""
    return ts / _ts_unit(ts) if ts else ts


def _ts_to_str(ts: int) -> str:
    """时间戳转人类可读时间（越界/非法值返回空串，避免单条脏数据中断整次查询）"""
    if not ts:
        return ""
    try:
        return datetime.datetime.fromtimestamp(_normalize_ts(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


def _parse_time_bound(value: str) -> datetime.datetime:
    """解析时间参数：'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM[:SS]'"""
    text = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间 '{value}'，支持 YYYY-MM-DD 或 YYYY-MM-DD HH:MM[:SS]")


def _time_range_to_ts(cur: sqlite3.Cursor, start: str, end: str) -> tuple:
    """将人类可读时间范围转换为与库内 create_time 单位一致的查询区间"""
    max_ts = cur.execute("SELECT max(create_time) FROM messages").fetchone()[0] or 0
    unit = _ts_unit(max_ts)
    start_dt = _parse_time_bound(start)
    end_dt = _parse_time_bound(end)
    # 结束时间只给到日期时，自动覆盖当天全天
    if len((end or "").strip()) <= 10:
        end_dt += datetime.timedelta(days=1)
    if end_dt <= start_dt:
        raise ValueError("结束时间必须晚于起始时间")
    return int(start_dt.timestamp() * unit), int(end_dt.timestamp() * unit)


def _escape_like(value: str) -> str:
    """转义 LIKE 通配符（% _ \\），使用户输入按字面量匹配"""
    return (value or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clamp(limit: Any, default: int, maximum: int) -> int:
    """把外部传入的 limit 钳制到合法区间。

    SQLite 中 ``LIMIT -1`` 等价于「无限制」，负数或 0 会让单次调用全表返回，
    因此统一收敛为 [1, maximum]，非法值回退到 default。
    """
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return min(value, maximum)


def _safe_offset(offset: Any) -> int:
    """把外部传入的 offset 收敛为非负整数（SQLite 负 OFFSET 等价于 0）"""
    try:
        return max(0, int(offset))
    except (TypeError, ValueError):
        return 0


def _dedupe_phrases(phrases, min_count: int = 3, scan: int = 200, limit: int = 20) -> List[Dict[str, Any]]:
    """把 n-gram 计数整理成短语列表，并过滤被更长短语完全覆盖的碎片。

    只要存在一个更长的短语完整包含当前短语、且其计数不低于当前计数，
    当前短语就视为 n-gram 碎片而丢弃（如「猫老」被「猫老板」覆盖、
    「算力和」被「算力和数」覆盖）。计数更高的短词说明有独立用法，予以保留。
    """
    ranked = [(p, c) for p, c in phrases.most_common(scan) if c >= min_count]
    result: List[Dict[str, Any]] = []
    for phrase, count in ranked:
        if any(
            other != phrase and phrase in other and other_count >= count
            for other, other_count in ranked
        ):
            continue
        result.append({"phrase": phrase, "count": count})
        if len(result) >= limit:
            break
    return result


@mcp.tool()
@_db_tool
def search_wechat_messages(query: str, session_id: Optional[str] = None, limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
    """全文搜索绿泡泡聊天记录。

    Args:
        query: 检索关键词（支持多词空格搜索）
        session_id: 可选，限定搜索特定的群聊（@chatroom）或联系人（绿泡泡号）
        limit: 返回数量，默认 20 条
        offset: 分页偏移，默认 0；需要翻页查看更早的命中时递增
    """
    err = _check_db_initialized()
    if err:
        return [{"error": err}]
    conn = get_connection()
    cur = conn.cursor()
    
    # 使用 LIKE 子串匹配而非 FTS5 MATCH：内置 unicode61 分词器会把连续中文当成
    # 单个 token（搜「缓存」匹配不到「我缓存命中都降到98%」），中文召回率极低；
    # 个人数据量（十万级）下 LIKE 全表扫描为百毫秒级，此处以正确性优先。
    tokens = [t for t in (query or "").split() if t]
    if not tokens:
        conn.close()
        # 与 search_contacts / list_chatroom_members 等保持一致：空参数明确报错，
        # 而不是静默返回空列表让人误以为「确实没有匹配」
        return [{"error": "query 不能为空"}]

    sql = """
        SELECT m.id, m.session_username, s.display_name AS session_name,
               m.sender_username, c.display_name AS sender_name,
               m.create_time, m.content
        FROM messages m
        LEFT JOIN sessions s ON s.username = m.session_username
        LEFT JOIN contacts c ON c.username = m.sender_username
        WHERE 1=1
    """
    params: List[Any] = []
    for token in tokens:
        sql += " AND m.content LIKE ? ESCAPE '\\'"
        params.append(f"%{_escape_like(token)}%")
    if session_id:
        sql += " AND m.session_username = ?"
        params.append(session_id)

    sql += " ORDER BY m.create_time DESC LIMIT ? OFFSET ?"
    params.extend([_clamp(limit, 20, 500), max(0, offset)])

    rows = cur.execute(sql, params).fetchall()

    results = []
    for r in rows:
        results.append({
            "id": r["id"],
            "session_id": r["session_username"],
            "session_name": r["session_name"] or r["session_username"],
            "sender_name": r["sender_name"] or r["sender_username"],
            "sender_username": r["sender_username"],
            "time": _ts_to_str(r["create_time"]),
            "content": r["content"],
        })
    conn.close()
    return results

@mcp.tool()
@_db_tool
def list_wechat_sessions(keyword: Optional[str] = None, session_type: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
    """查询绿泡泡会话列表（用于查找特定联系人或群聊的唯一 ID）。
    
    Args:
        keyword: 会话名称搜索关键字（群名/人名/备注）
        session_type: 会话类型筛选，可选：'room'（群聊）、'c2c'（单聊）、'gh'（公众号）
        limit: 返回记录数，默认 30
    """
    err = _check_db_initialized()
    if err:
        return [{"error": err}]
    conn = get_connection()
    cur = conn.cursor()
    
    sql = "SELECT username, type, display_name, message_count, last_time, last_msg_preview FROM sessions WHERE 1=1"
    params = []
    
    if keyword and keyword.strip():
        kw = f"%{_escape_like(keyword.strip())}%"
        sql += " AND (display_name LIKE ? ESCAPE '\\' OR username LIKE ? ESCAPE '\\')"
        params.extend([kw, kw])
    if session_type:
        sql += " AND type = ?"
        params.append(session_type)
        
    sql += " ORDER BY last_time DESC LIMIT ?"
    params.append(_clamp(limit, 30, 500))
    
    rows = cur.execute(sql, params).fetchall()
    results = []
    for r in rows:
        results.append({
            "session_id": r["username"],
            "display_name": r["display_name"],
            "type": r["type"],
            "message_count": r["message_count"],
            "last_time": _ts_to_str(r["last_time"]),
            "last_message": r["last_msg_preview"],
        })
    conn.close()
    return results

@mcp.tool()
@_db_tool
def get_session_history(session_id: str, limit: int = 50, days: Optional[int] = None) -> List[Dict[str, Any]]:
    """获取指定群聊或联系人的聊天记录（按时间正序排列，适合上下文理解与总结）。
    
    Args:
        session_id: 会话的唯一 ID（如群聊 57684484208@chatroom 或个人绿泡泡号）
        limit: 消息条数，默认 50，最大 200
        days: 可选，限定最近 N 天内的消息
    """
    err = _check_db_initialized()
    if err:
        return [{"error": err}]
    conn = get_connection()
    cur = conn.cursor()
    
    limit = _clamp(limit, 50, 200)
    sql = """
        SELECT m.id, m.sender_username, c.display_name AS sender_name,
               m.create_time, m.content
        FROM messages m
        LEFT JOIN contacts c ON c.username = m.sender_username
        WHERE m.session_username = ?
    """
    params = [session_id]
    
    if days:
        cur.execute("SELECT max(create_time) FROM messages WHERE session_username = ?", (session_id,))
        max_ts = cur.fetchone()[0] or 0
        start_ts = max_ts - int(days * 24 * 3600 * _ts_unit(max_ts))
        sql += " AND m.create_time >= ?"
        params.append(start_ts)
        
    sql += " ORDER BY m.create_time DESC LIMIT ?"
    params.append(_clamp(limit, 30, 500))
    
    rows = cur.execute(sql, params).fetchall()
    
    # 转换为正序输出，便于大模型按时间线阅读
    results = []
    for r in reversed(rows):
        results.append({
            "id": r["id"],
            "sender_name": r["sender_name"] or r["sender_username"],
            "sender_username": r["sender_username"],
            "time": _ts_to_str(r["create_time"]),
            "content": r["content"],
        })
    conn.close()
    return results

@mcp.tool()
@_db_tool
def search_shared_files(keyword: Optional[str] = None, session_id: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
    """查找绿泡泡聊天中传输的文件（如 PDF、Excel、Word 等文档记录）。
    
    Args:
        keyword: 文件名关键字搜索（如 '理财', '报告', 'xlsx'）
        session_id: 可选，限定特定群聊或联系人
        limit: 返回数量，默认 30
    """
    err = _check_db_initialized()
    if err:
        return [{"error": err}]
    conn = get_connection()
    cur = conn.cursor()
    
    sql = """
        SELECT m.session_username, s.display_name AS session_name,
               m.sender_username, c.display_name AS sender_name,
               m.create_time, m.content
        FROM messages m
        LEFT JOIN sessions s ON s.username = m.session_username
        LEFT JOIN contacts c ON c.username = m.sender_username
        WHERE m.content LIKE '[文件]%'
    """
    params = []
    
    if keyword and keyword.strip():
        sql += " AND m.content LIKE ? ESCAPE '\\'"
        params.append(f"%{_escape_like(keyword.strip())}%")
    if session_id:
        sql += " AND m.session_username = ?"
        params.append(session_id)
        
    sql += " ORDER BY m.create_time DESC LIMIT ?"
    params.append(_clamp(limit, 30, 500))
    
    rows = cur.execute(sql, params).fetchall()
    results = []
    for r in rows:
        results.append({
            "session_name": r["session_name"] or r["session_username"],
            "session_id": r["session_username"],
            "sender_name": r["sender_name"] or r["sender_username"],
            "sender_username": r["sender_username"],
            "time": _ts_to_str(r["create_time"]),
            "file_name": r["content"].replace("[文件] ", ""),
        })
    conn.close()
    return results

@mcp.tool()
@_db_tool
def get_message_image(message_id: int, include_base64: bool = False) -> Dict[str, Any]:
    """获取并解密指定消息关联的聊天图片。
    
    优先返回已解密图片的本地物理绝对路径（避免直接将大图 base64 注入大模型导致上下文窗口被撑爆）。
    如需将图片传递给多模态大模型进行看图分析，可设置 include_base64=True。
    
    Args:
        message_id: 消息 ID（来自 search_wechat_messages 或 get_messages_by_date 返回的 id 字段）
        include_base64: 是否在返回结果中包含图片的 base64 字符串（默认 False）
    """
    err = _check_db_initialized()
    if err:
        return {"error": err}
    return get_image_for_message(message_id=message_id, include_base64=include_base64)


# 单次头像查询的 username 上限。面板一屏消息通常只涉及十几个发言者，
# 200 足够覆盖"整群成员头像"这类批量场景，同时挡住超长入参。
_AVATAR_LOOKUP_LIMIT = 200


@mcp.tool()
@_db_tool
def get_user_avatars(usernames: List[str]) -> Dict[str, Any]:
    """批量获取用户 / 群 / 公众号头像的本地图片文件路径。

    头像来自绿泡泡 4.x 的 head_image.db（已由 ETL 落盘为真实图片文件）。
    只返回**本地绝对路径**，不返回图片内容 —— 供面板等宿主直接读盘渲染，
    避免把图片塞进大模型上下文。

    一次性批量查询：渲染一屏消息通常涉及十几个发言者，逐个调用开销过大。

    Args:
        usernames: 用户名列表（wxid / 群号 *@chatroom / 公众号 *@openim），
                   自动去空去重，最多 200 个

    Returns:
        {"success": True, "count": 命中数, "avatars": {username: {md5, format,
        size_bytes, file_path}}, "missing": [未命中或文件已丢失的 username]}
    """
    err = _check_db_initialized()
    if err:
        return {"error": err}

    # 归一化：去空、去重、限量（保留原顺序，便于调用方对齐）
    wanted: List[str] = []
    seen = set()
    for u in usernames or []:
        if not isinstance(u, str):
            continue
        u = u.strip()
        if not u or u in seen:
            continue
        seen.add(u)
        wanted.append(u)
    if not wanted:
        return {"success": True, "count": 0, "avatars": {}, "missing": []}
    truncated = len(wanted) > _AVATAR_LOOKUP_LIMIT
    wanted = wanted[:_AVATAR_LOOKUP_LIMIT]

    conn = get_connection()
    cur = conn.cursor()
    avatars: Dict[str, Any] = {}
    try:
        placeholders = ",".join("?" for _ in wanted)
        rows = cur.execute(
            f"SELECT username, md5, format, size_bytes, file_path "
            f"FROM avatars WHERE username IN ({placeholders})",
            wanted,
        ).fetchall()
        for r in rows:
            fp = r["file_path"] or ""
            # 索引里的文件可能已被用户清理掉，命中但读不到的不算命中
            if not fp or not Path(fp).exists():
                continue
            avatars[r["username"]] = {
                "md5": r["md5"] or "",
                "format": r["format"] or "",
                "size_bytes": r["size_bytes"] or 0,
                "file_path": fp,
            }
    except sqlite3.Error:
        # avatars 表尚未由 ETL 建立：视为"全部无头像"，由调用方降级到占位色块
        avatars = {}
    conn.close()

    res: Dict[str, Any] = {
        "success": True,
        "count": len(avatars),
        "avatars": avatars,
        "missing": [u for u in wanted if u not in avatars],
    }
    if truncated:
        res["truncated"] = True
    return res


@mcp.tool()
@_db_tool
def get_messages_by_date(date: str, session_id: Optional[str] = None, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    """按指定单日（YYYY-MM-DD）查询聊天记录（用于日报分析、事件回溯等）。

    结果按时间正序返回。单日消息数超过 limit 时请用 offset 翻页取后续，
    否则只能拿到当天最早的一段。

    Args:
        date: 日期，格式如 '2026-10-16'
        session_id: 可选，限定特定群聊或联系人
        limit: 返回消息数量，默认 100
        offset: 跳过前 N 条（默认 0），用于分页
    """
    err = _check_db_initialized()
    if err:
        return [{"error": err}]
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        dt_start = datetime.datetime.strptime(date, "%Y-%m-%d")
        dt_end = dt_start + datetime.timedelta(days=1)
    except Exception as e:
        return [{"error": f"Invalid date format: {e}"}]

    # create_time 单位随微信版本而异（秒 / 毫秒），必须按实际单位换算查询区间
    max_ts = cur.execute("SELECT max(create_time) FROM messages").fetchone()[0] or 0
    unit = _ts_unit(max_ts)
    start_ts = int(dt_start.timestamp() * unit)
    end_ts = int(dt_end.timestamp() * unit)
        
    sql = """
        SELECT m.id, m.session_username, s.display_name AS session_name,
               m.sender_username, c.display_name AS sender_name,
               m.create_time, m.content
        FROM messages m
        LEFT JOIN sessions s ON s.username = m.session_username
        LEFT JOIN contacts c ON c.username = m.sender_username
        WHERE m.create_time >= ? AND m.create_time < ?
    """
    params = [start_ts, end_ts]
    
    if session_id:
        sql += " AND m.session_username = ?"
        params.append(session_id)
        
    sql += " ORDER BY m.create_time ASC LIMIT ? OFFSET ?"
    params.append(_clamp(limit, 30, 500))
    params.append(_safe_offset(offset))

    rows = cur.execute(sql, params).fetchall()
    results = []
    for r in rows:
        results.append({
            "session_name": r["session_name"] or r["session_username"],
            "sender_name": r["sender_name"] or r["sender_username"],
            "sender_username": r["sender_username"],
            "time": _ts_to_str(r["create_time"]),
            "content": r["content"],
        })
    conn.close()
    return results

@mcp.tool()
@_db_tool
def sync_wechat_database(force: bool = False) -> Dict[str, Any]:
    """触发数据导入同步，使本地 MCP 数据库保持最新状态。
    
    Args:
        force: 是否强制全量同步
    """
    etl_result = sync_data(force=force)
    return {
        "etl_step": etl_result
    }

@mcp.tool()
@_db_tool
def search_contacts(keyword: str, limit: int = 30) -> List[Dict[str, Any]]:
    """搜索绿泡泡联系人（按昵称 / 备注 / 微信号匹配）。

    Args:
        keyword: 搜索关键字（昵称、备注或微信号片段）
        limit: 返回数量，默认 30
    """
    err = _check_db_initialized()
    if err:
        return [{"error": err}]
    if not (keyword or "").strip():
        return [{"error": "keyword 不能为空"}]
    conn = get_connection()
    cur = conn.cursor()
    kw = f"%{_escape_like(keyword.strip())}%"
    rows = cur.execute("""
        SELECT username, alias, display_name, type
        FROM contacts
        WHERE display_name LIKE ? ESCAPE '\\' OR alias LIKE ? ESCAPE '\\' OR username LIKE ? ESCAPE '\\'
        ORDER BY CASE WHEN alias IS NULL OR alias = '' THEN 1 ELSE 0 END, display_name
        LIMIT ?
    """, (kw, kw, kw, _clamp(limit, 30, 500))).fetchall()
    conn.close()
    return [
        {
            "username": r["username"],
            "display_name": r["display_name"],
            "alias": r["alias"] or "",
            "type": r["type"],
        }
        for r in rows
    ]

@mcp.tool()
@_db_tool
def list_chatroom_members(chatroom_id: str, keyword: Optional[str] = None, limit: int = 200) -> Dict[str, Any]:
    """列出指定群聊的成员名单（自动关联联系人昵称与微信号）。

    Args:
        chatroom_id: 群聊唯一 ID（形如 12345678@chatroom，可用 list_wechat_sessions 查询）
        keyword: 可选，按成员昵称 / 微信号 / username 过滤
        limit: 返回数量，默认 200
    """
    err = _check_db_initialized()
    if err:
        return {"error": err}
    if not (chatroom_id or "").strip():
        return {"error": "chatroom_id 不能为空"}
    conn = get_connection()
    cur = conn.cursor()

    total = cur.execute(
        "SELECT COUNT(*) FROM chatroom_members WHERE chatroom_username = ?", (chatroom_id,)
    ).fetchone()[0]
    if total == 0:
        conn.close()
        return {"error": f"未找到群 {chatroom_id} 的成员记录，请确认群 ID（可用 list_wechat_sessions 查询）"}

    sql = """
        SELECT cm.member_username,
               COALESCE(NULLIF(c.display_name, ''), cm.member_username) AS display_name,
               COALESCE(c.alias, '') AS alias
        FROM chatroom_members cm
        LEFT JOIN contacts c ON c.username = cm.member_username
        WHERE cm.chatroom_username = ?
    """
    params: List[Any] = [chatroom_id]
    if keyword and keyword.strip():
        sql += " AND (c.display_name LIKE ? ESCAPE '\\' OR c.alias LIKE ? ESCAPE '\\' OR cm.member_username LIKE ? ESCAPE '\\')"
        kw = f"%{_escape_like(keyword.strip())}%"
        params.extend([kw, kw, kw])
    sql += " ORDER BY display_name LIMIT ?"
    params.append(_clamp(limit, 30, 500))

    rows = cur.execute(sql, params).fetchall()
    session_row = cur.execute(
        "SELECT display_name FROM sessions WHERE username = ?", (chatroom_id,)
    ).fetchone()
    conn.close()

    return {
        "chatroom_id": chatroom_id,
        "chatroom_name": session_row["display_name"] if session_row else chatroom_id,
        "member_count": total,
        "returned": len(rows),
        "members": [
            {
                "username": r["member_username"],
                "display_name": r["display_name"],
                "alias": r["alias"],
            }
            for r in rows
        ],
    }

@mcp.tool()
@_db_tool
def get_messages_by_range(start: str, end: str, session_id: Optional[str] = None, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    """按自定义时间范围查询聊天记录（支持跨天与任意时段，比按单日查询更灵活）。

    结果按时间正序返回。范围内消息数超过 limit 时请用 offset 翻页取后续。

    Args:
        start: 起始时间，'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM[:SS]'
        end: 结束时间，同上；只给日期时自动覆盖当天全天
        session_id: 可选，限定某个群聊或联系人
        limit: 返回数量，默认 100
        offset: 跳过前 N 条（默认 0），用于分页
    """
    err = _check_db_initialized()
    if err:
        return [{"error": err}]
    conn = get_connection()
    cur = conn.cursor()
    try:
        start_ts, end_ts = _time_range_to_ts(cur, start, end)
    except ValueError as exc:
        conn.close()
        return [{"error": str(exc)}]

    sql = """
        SELECT m.id, m.session_username, s.display_name AS session_name,
               m.sender_username, c.display_name AS sender_name,
               m.create_time, m.content
        FROM messages m
        LEFT JOIN sessions s ON s.username = m.session_username
        LEFT JOIN contacts c ON c.username = m.sender_username
        WHERE m.create_time >= ? AND m.create_time < ?
    """
    params: List[Any] = [start_ts, end_ts]
    if session_id:
        sql += " AND m.session_username = ?"
        params.append(session_id)
    sql += " ORDER BY m.create_time ASC LIMIT ? OFFSET ?"
    params.append(_clamp(limit, 30, 500))
    params.append(_safe_offset(offset))

    rows = cur.execute(sql, params).fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "session_id": r["session_username"],
            "session_name": r["session_name"] or r["session_username"],
            "sender_name": r["sender_name"] or r["sender_username"],
            "sender_username": r["sender_username"],
            "time": _ts_to_str(r["create_time"]),
            "content": r["content"],
        }
        for r in rows
    ]

@mcp.tool()
@_db_tool
def get_chat_activity_stats(session_id: Optional[str] = None, start: Optional[str] = None, end: Optional[str] = None) -> Dict[str, Any]:
    """统计聊天活跃度：消息总量、按小时与按天分布、发言最多的人。

    Args:
        session_id: 可选，限定某个群聊或联系人（不传则统计全部会话）
        start: 可选，起始时间 'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM[:SS]'
        end: 可选，结束时间，同上（需与 start 同时提供）
    """
    err = _check_db_initialized()
    if err:
        return {"error": err}
    conn = get_connection()
    cur = conn.cursor()

    where = ["1=1"]
    params: List[Any] = []
    if session_id:
        where.append("m.session_username = ?")
        params.append(session_id)
    if bool(start) != bool(end):
        conn.close()
        return {"error": "start 与 end 必须同时提供（只传其一会被静默忽略，故直接报错）"}
    if start and end:
        try:
            start_ts, end_ts = _time_range_to_ts(cur, start, end)
        except ValueError as exc:
            conn.close()
            return {"error": str(exc)}
        where.append("m.create_time >= ? AND m.create_time < ?")
        params.extend([start_ts, end_ts])
    where_sql = " AND ".join(where)

    max_ts = cur.execute("SELECT max(create_time) FROM messages").fetchone()[0] or 0
    divisor = _ts_unit(max_ts)

    total = cur.execute(f"SELECT COUNT(*) FROM messages m WHERE {where_sql}", params).fetchone()[0]
    hourly = cur.execute(f"""
        SELECT strftime('%H', datetime(create_time / {divisor}, 'unixepoch', 'localtime')) AS h,
               COUNT(*) AS n
        FROM messages m WHERE {where_sql}
        GROUP BY h ORDER BY h
    """, params).fetchall()
    daily = cur.execute(f"""
        SELECT strftime('%Y-%m-%d', datetime(create_time / {divisor}, 'unixepoch', 'localtime')) AS d,
               COUNT(*) AS n
        FROM messages m WHERE {where_sql}
        GROUP BY d ORDER BY d
    """, params).fetchall()
    top_senders = cur.execute(f"""
        SELECT COALESCE(NULLIF(c.display_name, ''), m.sender_username) AS name,
               m.sender_username AS username, COUNT(*) AS n
        FROM messages m
        LEFT JOIN contacts c ON c.username = m.sender_username
        WHERE {where_sql}
        GROUP BY m.sender_username
        ORDER BY n DESC LIMIT 10
    """, params).fetchall()
    conn.close()

    return {
        "session_id": session_id or "(全部会话)",
        "time_range": {"start": start, "end": end} if (start and end) else None,
        "total_messages": total,
        "hourly_distribution": {r["h"]: r["n"] for r in hourly},
        "daily_distribution": {r["d"]: r["n"] for r in daily},
        "top_senders": [
            {"display_name": r["name"], "username": r["username"], "message_count": r["n"]}
            for r in top_senders
        ],
    }

@mcp.tool()
@_db_tool
def search_media_files(keyword: Optional[str] = None, kind: Optional[str] = None, month: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
    """查找聊天中传输过的图片 / 文件 / 视频，并返回其磁盘路径。

    Args:
        keyword: 可选，按文件名关键字搜索
        kind: 可选，类型筛选：image（图片）、file（文件）、video（视频）
        month: 可选，按月份筛选，格式 'YYYY-MM'
        limit: 返回数量，默认 30
    """
    err = _check_db_initialized()
    if err:
        return [{"error": err}]
    conn = get_connection()
    cur = conn.cursor()

    sql = """
        SELECT md5, kind, file_name, file_size, modify_time, month, rel_path, abs_path
        FROM media_files WHERE 1=1
    """
    params: List[Any] = []
    if keyword and keyword.strip():
        sql += " AND file_name LIKE ? ESCAPE '\\'"
        params.append(f"%{_escape_like(keyword.strip())}%")
    if kind and kind.strip():
        sql += " AND kind = ?"
        params.append(kind.strip().lower())
    if month and month.strip():
        sql += " AND month = ?"
        params.append(month.strip())
    sql += " ORDER BY modify_time DESC LIMIT ?"
    params.append(_clamp(limit, 30, 500))

    rows = cur.execute(sql, params).fetchall()
    conn.close()
    return [
        {
            "md5": r["md5"],
            "kind": r["kind"],
            "file_name": r["file_name"],
            "file_size": r["file_size"],
            "time": _ts_to_str(r["modify_time"]),
            "month": r["month"],
            "rel_path": r["rel_path"],
            "abs_path": r["abs_path"],
            "exists": bool(r["abs_path"]) and Path(r["abs_path"]).is_file(),
        }
        for r in rows
    ]

@mcp.tool()
@_db_tool
def list_favorites(keyword: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    """查询绿泡泡收藏条目（标题 / 描述 / 来源）。

    Args:
        keyword: 可选，按标题或描述搜索
        limit: 返回数量，默认 20
    """
    err = _check_db_initialized()
    if err:
        return [{"error": err}]
    conn = get_connection()
    cur = conn.cursor()

    sql = "SELECT local_id, type, update_time, title, description, from_user FROM favorites WHERE 1=1"
    params: List[Any] = []
    if keyword and keyword.strip():
        kw = f"%{_escape_like(keyword.strip())}%"
        sql += " AND (title LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')"
        params.extend([kw, kw])
    sql += " ORDER BY update_time DESC LIMIT ?"
    params.append(_clamp(limit, 30, 500))

    rows = cur.execute(sql, params).fetchall()
    conn.close()
    return [
        {
            "local_id": r["local_id"],
            "type": r["type"],
            "time": _ts_to_str(r["update_time"]),
            "title": r["title"],
            "description": r["description"],
            "from_user": r["from_user"],
        }
        for r in rows
    ]

@mcp.tool()
@_db_tool
def list_emoticons(keyword: Optional[str] = None, package: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """列出绿泡泡表情包与表情（含官方文字说明）。

    Args:
        keyword: 可选，按文字说明或表情包名搜索
        package: 可选，按表情包名称筛选
        limit: 返回数量，默认 50
    """
    err = _check_db_initialized()
    if err:
        return [{"error": err}]
    conn = get_connection()
    cur = conn.cursor()
    sql = (
        "SELECT md5, caption, package_id, package_name, kind, thumb_url, cdn_url, designer_id "
        "FROM emoticons WHERE 1=1"
    )
    params: List[Any] = []
    if keyword and keyword.strip():
        kw = f"%{_escape_like(keyword.strip())}%"
        sql += " AND (caption LIKE ? ESCAPE '\\' OR package_name LIKE ? ESCAPE '\\')"
        params.extend([kw, kw])
    if package and package.strip():
        sql += " AND package_name LIKE ? ESCAPE '\\'"
        params.append(f"%{_escape_like(package.strip())}%")
    sql += " ORDER BY CASE WHEN caption = '' THEN 1 ELSE 0 END, package_name, caption LIMIT ?"
    params.append(_clamp(limit, 30, 500))
    rows = cur.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@mcp.tool()
@_db_tool
def get_sync_status() -> Dict[str, Any]:
    """查看本地索引的数据新鲜度与规模，判断是否需要重新运行 wechat-init 同步新消息。"""
    err = _check_db_initialized()
    if err:
        return {"error": err}
    conn = get_connection()
    cur = conn.cursor()

    def _one(query: str) -> int:
        try:
            row = cur.execute(query).fetchone()
            return row[0] if row and row[0] is not None else 0
        except sqlite3.Error:
            # 旧版本数据库可能缺少后续版本新增的表，此处容错为 0
            return 0

    counts = {
        "messages": _one("SELECT COUNT(*) FROM messages"),
        "contacts": _one("SELECT COUNT(*) FROM contacts"),
        "sessions": _one("SELECT COUNT(*) FROM sessions"),
        "chatroom_members": _one("SELECT COUNT(*) FROM chatroom_members"),
        "media_files": _one("SELECT COUNT(*) FROM media_files"),
        "favorites": _one("SELECT COUNT(*) FROM favorites"),
        "emoticons": _one("SELECT COUNT(*) FROM emoticons"),
    }
    oldest = _one("SELECT min(create_time) FROM messages")
    latest = _one("SELECT max(create_time) FROM messages")
    conn.close()
    return {
        "database": str(DB_PATH),
        "oldest_message": _ts_to_str(oldest),
        "latest_message": _ts_to_str(latest),
        "counts": counts,
        "note": "解密库是初始化时的快照。微信有新消息时，需以管理员身份重新运行 wechat-init，再执行 wechat-init --etl-only 重建索引。",
    }

@mcp.tool()
@_db_tool
def get_persona_style(sender: str, session_id: Optional[str] = None, sample_limit: int = 300) -> Dict[str, Any]:
    """分析某位联系人的沟通风格（统计画像 + 高频用语 + 典型语句），可用于风格蒸馏。

    Args:
        sender: 联系人昵称 / 微信号 / username
        session_id: 可选，限定在某个群聊中分析
        sample_limit: 参与分析的消息上限，默认 300
    """
    err = _check_db_initialized()
    if err:
        return {"error": err}
    if not (sender or "").strip():
        return {"error": "sender 不能为空"}

    conn = get_connection()
    cur = conn.cursor()

    # 解析目标联系人：先精确匹配，再模糊匹配
    row = cur.execute(
        "SELECT username, display_name FROM contacts "
        "WHERE username = ? OR alias = ? OR display_name = ? LIMIT 1",
        (sender.strip(), sender.strip(), sender.strip()),
    ).fetchone()
    if not row:
        row = cur.execute(
            "SELECT username, display_name FROM contacts "
            "WHERE display_name LIKE ? ESCAPE '\\' LIMIT 1",
            (f"%{_escape_like(sender.strip())}%",),
        ).fetchone()
    if not row:
        conn.close()
        return {"error": f"未找到联系人：{sender}（可先用 search_contacts 查询）"}

    target_username = row["username"]
    target_name = row["display_name"]

    sql = "SELECT content, create_time, local_type FROM messages WHERE sender_username = ?"
    params: List[Any] = [target_username]
    if session_id:
        sql += " AND session_username = ?"
        params.append(session_id)
    sql += " ORDER BY create_time DESC LIMIT ?"
    params.append(_clamp(sample_limit, 300, 2000))

    rows = cur.execute(sql, params).fetchall()
    conn.close()
    if not rows:
        return {"error": f"{target_name} 在指定范围内没有消息记录"}

    from collections import Counter

    texts = []
    for r in rows:
        if r["local_type"] != 1 or not r["content"]:
            continue
        # 去掉引用消息前缀的 wxid，避免污染高频短语统计与样本展示
        cleaned_text = re.sub(r"^wxid_[A-Za-z0-9]+:\s*", "", r["content"]).strip()
        if cleaned_text:
            texts.append(cleaned_text)
    type_counter = Counter(r["local_type"] for r in rows)
    hour_counter = Counter(
        datetime.datetime.fromtimestamp(_normalize_ts(r["create_time"])).hour
        for r in rows
        if r["create_time"]
    )

    lengths = [len(t) for t in texts] or [0]

    # 高频用语：中文按 2-4 字 n-gram，英文/数字按完整词统计（避免无意义碎片）
    phrases: Counter = Counter()
    for text in texts:
        cleaned = re.sub(r"\[[^\]]{1,12}\]", " ", text)
        for chunk in re.findall(r"[\u4e00-\u9fff]+", cleaned):
            for n in (2, 3, 4):
                for i in range(len(chunk) - n + 1):
                    phrases[chunk[i : i + n]] += 1
        for word in re.findall(r"[A-Za-z0-9]{2,}", cleaned):
            phrases[word.lower()] += 1

    tone_candidates = [
        "啊", "吧", "呢", "嘛", "哦", "呀", "哈", "嘿", "嗯", "唉", "哎", "诶",
        "哇", "草", "靠", "笑死", "哈哈", "hhh", "emm",
    ]
    tone = {w: sum(t.count(w) for t in texts) for w in tone_candidates}
    tone = {k: v for k, v in sorted(tone.items(), key=lambda kv: -kv[1]) if v > 0}

    samples: List[str] = []
    for text in texts:
        if 5 <= len(text) <= 80 and text not in samples:
            samples.append(text)
        if len(samples) >= 8:
            break

    return {
        "sender": target_name,
        "username": target_username,
        "scope_session": session_id or "(全部会话)",
        "analyzed_messages": len(rows),
        "text_messages": len(texts),
        "avg_length": round(sum(lengths) / len(lengths), 1),
        "max_length": max(lengths),
        "message_type_distribution": {str(k): v for k, v in type_counter.most_common(10)},
        "active_hours": {f"{h:02d}": n for h, n in sorted(hour_counter.items())},
        "tone_words": tone,
        "top_phrases": _dedupe_phrases(phrases),
        "samples": samples,
    }

def main():
    mcp.run()

if __name__ == "__main__":
    main()
