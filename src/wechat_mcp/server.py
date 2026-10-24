import datetime
import functools
import logging
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Optional, List, Dict, Any

from mcp.server.fastmcp import FastMCP

from .config import DB_PATH, BASE_DIR
from .database import get_connection, init_schema, release_connections, sync_data
from .images import get_image_for_message
from .emoticons import (
    get_emoticon_image as _get_emoticon_image,
    prepare_emoticons as _prepare_emoticons,
)
from .records import get_chat_record as _get_chat_record
from .parser import type_label
from .auto_sync import (
    get_auto_sync_status as _auto_sync_status,
    restore_on_startup,
    set_auto_sync as _set_auto_sync,
    start_sync as _start_sync,
    sync_now,
)

logger = logging.getLogger(__name__)

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
        # favorites / emoticons / avatars），或者 messages / sessions 缺少新增的列
        # （如 unread / emoticon_md5），会让对应工具抛裸 OperationalError。
        # 始终调用 init_schema() 执行幂等补表与 ALTER TABLE 迁移。
        conn.close()
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


def _snippet(content: Any, tokens: List[str], radius: int = 30) -> str:
    """命中上下文片段：取第一个命中词前后各 radius 个字符（不依赖 FTS 的 snippet）。"""
    text = str(content or "")
    if not text:
        return ""
    low = text.lower()
    pos = -1
    for token in tokens:
        i = low.find(str(token).lower())
        if i >= 0 and (pos < 0 or i < pos):
            pos = i
    if pos < 0:
        return text[: radius * 2]
    start = max(0, pos - radius)
    end = min(len(text), pos + radius)
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


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


def _has_column(cur: sqlite3.Cursor, table: str, column: str) -> bool:
    """判断本地索引表是否已有某列（老库未重建索引时新列可能缺失）。"""
    try:
        return any(row[1] == column for row in cur.execute(f"PRAGMA table_info({table})"))
    except sqlite3.Error:
        return False


# ---------------------------------------------------------------------------
# 分析类工具共用的聚合辅助
#
# 设计约束：这些函数只做「通用数据聚合」，不生成展示文案、不依赖任何前端，
# 返回值可直接被面板、Agent 或第三方脚本消费。新增维度一律以「只加字段、
# 不改既有字段语义」的方式扩展，避免破坏已有调用方。
# ---------------------------------------------------------------------------

# 引用消息的可读前缀形如：「[引用 昵称: 被引用的内容] 回复内容」
_QUOTE_PREFIX_RE = re.compile(r"^\[引用 (.+?): ")
_BASE_TYPE_MASK = 4294967295  # 低 32 位：基础消息类型
# 群自身（系统消息 / 群公告 / 撤回提示）的 sender_username 等于 session_username：
# 它不是一个「成员」，所有发言人维度都要排除；但这些消息本身仍计入消息总量与类型分布。
_SELF_SENDER_FILTER = "COALESCE(m.sender_username, '') <> COALESCE(m.session_username, '')"


def _range_where(
    cur: sqlite3.Cursor,
    session_id: Optional[str],
    start: Optional[str],
    end: Optional[str],
) -> tuple:
    """构建消息查询的 WHERE 子句与参数（时间范围必须同时提供）。"""
    where = ["1=1"]
    params: List[Any] = []
    if session_id:
        where.append("m.session_username = ?")
        params.append(session_id)
    if bool(start) != bool(end):
        raise ValueError("start 与 end 必须同时提供（只传其一会被静默忽略，故直接报错）")
    if start and end:
        start_ts, end_ts = _time_range_to_ts(cur, start, end)
        where.append("m.create_time >= ? AND m.create_time < ?")
        params.extend([start_ts, end_ts])
    return " AND ".join(where), params


def _gini(values: List[int]) -> float:
    """基尼系数：0 = 完全平均，越接近 1 表示消息越集中在少数人。"""
    vals = sorted(int(v) for v in values if v and v > 0)
    n = len(vals)
    if n <= 1:
        return 0.0
    total = float(sum(vals))
    if total <= 0:
        return 0.0
    cumulative = sum((i + 1) * v for i, v in enumerate(vals))
    return round((2.0 * cumulative) / (n * total) - (n + 1.0) / n, 4)


def _hhi(values: List[int]) -> float:
    """赫芬达尔指数：Σ(份额²)，越大表示越集中。"""
    total = float(sum(int(v) for v in values if v and v > 0))
    if total <= 0:
        return 0.0
    return round(sum((int(v) / total) ** 2 for v in values if v and v > 0), 4)


def _member_aggregate(
    cur: sqlite3.Cursor,
    where_sql: str,
    params: List[Any],
    divisor: int,
) -> List[sqlite3.Row]:
    """按发言人一次聚合全部指标，供排行、集中度与报告复用。"""
    return cur.execute(
        f"""
        SELECT m.sender_username AS u,
               COALESCE(NULLIF(c.display_name, ''), m.sender_username) AS name,
               COUNT(*) AS messages,
               COALESCE(SUM(LENGTH(COALESCE(m.content, ''))), 0) AS chars,
               COALESCE(MAX(LENGTH(COALESCE(m.content, ''))), 0) AS longest,
               SUM(CASE WHEN (m.local_type & {_BASE_TYPE_MASK}) = 3 OR (m.local_type >> 32) = 3 THEN 1 ELSE 0 END) AS images,
               SUM(CASE WHEN (m.local_type >> 32) = 6 THEN 1 ELSE 0 END) AS files,
               SUM(CASE WHEN (m.local_type & {_BASE_TYPE_MASK}) = 43 OR (m.local_type >> 32) = 4 THEN 1 ELSE 0 END) AS videos,
               SUM(CASE WHEN (m.local_type & {_BASE_TYPE_MASK}) = 47 OR (m.local_type >> 32) = 8 THEN 1 ELSE 0 END) AS emojis,
               SUM(CASE WHEN (m.local_type >> 32) = 57 THEN 1 ELSE 0 END) AS quotes_given,
               SUM(CASE WHEN m.content LIKE '%撤回%' THEN 1 ELSE 0 END) AS recall_like_events,
               SUM(CASE WHEN CAST(strftime('%H', datetime(m.create_time / {divisor}, 'unixepoch', 'localtime')) AS INTEGER) < 6 THEN 1 ELSE 0 END) AS night,
               SUM(CASE WHEN CAST(strftime('%H', datetime(m.create_time / {divisor}, 'unixepoch', 'localtime')) AS INTEGER) BETWEEN 6 AND 8 THEN 1 ELSE 0 END) AS early,
               MIN(m.create_time) AS first_time,
               MAX(m.create_time) AS last_time
        FROM messages m
        LEFT JOIN contacts c ON c.username = m.sender_username
        WHERE {where_sql} AND {_SELF_SENDER_FILTER}
        GROUP BY m.sender_username
        ORDER BY messages DESC
        """,
        params,
    ).fetchall()


def _member_bursts(
    cur: sqlite3.Cursor,
    where_sql: str,
    params: List[Any],
    divisor: int,
    threshold_seconds: int = 60,
) -> Dict[str, int]:
    """统计每人「连发」次数：与上一条自己消息间隔不超过阈值的次数。"""
    rows = cur.execute(
        f"""
        WITH seq AS (
            SELECT m.sender_username AS u,
                   m.create_time - LAG(m.create_time) OVER (
                       PARTITION BY m.sender_username ORDER BY m.create_time
                   ) AS gap
            FROM messages m WHERE {where_sql} AND {_SELF_SENDER_FILTER}
        )
        SELECT u, SUM(CASE WHEN gap <= ? THEN 1 ELSE 0 END) AS bursts
        FROM seq WHERE gap IS NOT NULL GROUP BY u
        """,
        list(params) + [threshold_seconds * divisor],
    ).fetchall()
    return {r["u"]: int(r["bursts"] or 0) for r in rows}


def _quote_network(
    cur: sqlite3.Cursor,
    where_sql: str,
    params: List[Any],
    top_n: int,
) -> Dict[str, Any]:
    """解析引用消息，得到「谁引用了谁」的边与被引用榜。

    微信引用消息只保留被引用者的显示名，不保留稳定 ID；这里以显示名为节点，
    同名用户会被合并，属于已知近似（解析率会一并返回，便于调用方判断可信度）。
    """
    rows = cur.execute(
        f"""
        SELECT m.sender_username AS u,
               COALESCE(NULLIF(c.display_name, ''), m.sender_username) AS name,
               m.content AS content
        FROM messages m
        LEFT JOIN contacts c ON c.username = m.sender_username
        WHERE {where_sql} AND {_SELF_SENDER_FILTER} AND m.content LIKE '[引用%'
        """,
        params,
    ).fetchall()

    quoted: Counter = Counter()
    edges: Counter = Counter()
    parsed = 0
    for row in rows:
        match = _QUOTE_PREFIX_RE.match(row["content"] or "")
        if not match:
            continue
        parsed += 1
        target = match.group(1).strip()
        if not target:
            continue
        quoted[target] += 1
        edges[(row["name"], target)] += 1

    total = len(rows)
    payload = {
        "quote_messages": total,
        "parsed": parsed,
        "parse_rate": round(parsed / total, 4) if total else 0.0,
        "top_quoted": [
            {"name": name, "count": count}
            for name, count in quoted.most_common(top_n)
        ],
        "edges": [
            {"from": src, "to": dst, "count": count}
            for (src, dst), count in edges.most_common(top_n)
        ],
    }
    return payload, quoted


def _silent_members(
    cur: sqlite3.Cursor,
    where_sql: str,
    params: List[Any],
    session_id: Optional[str],
    top_n: int,
) -> Optional[Dict[str, Any]]:
    """群成员中在指定时间范围内没有发言的人（仅群聊有效）。"""
    if not session_id:
        return None
    member_total = cur.execute(
        "SELECT COUNT(*) FROM chatroom_members WHERE chatroom_username = ?",
        (session_id,),
    ).fetchone()[0]
    if not member_total:
        return {
            "member_total": 0,
            "active_members": 0,
            "silent_total": 0,
            "active_rate": 0.0,
            "silent_members": [],
        }
    silent_total = cur.execute(
        f"""
        SELECT COUNT(*) FROM chatroom_members cm
        WHERE cm.chatroom_username = ?
          AND cm.member_username NOT IN (
              SELECT DISTINCT m.sender_username FROM messages m WHERE {where_sql} AND {_SELF_SENDER_FILTER}
          )
        """,
        [session_id] + list(params),
    ).fetchone()[0]
    rows = cur.execute(
        f"""
        SELECT cm.member_username AS u,
               COALESCE(NULLIF(cm.display_name, ''), cm.member_username) AS name
        FROM chatroom_members cm
        WHERE cm.chatroom_username = ?
          AND cm.member_username NOT IN (
              SELECT DISTINCT m.sender_username FROM messages m WHERE {where_sql}
          )
        ORDER BY name
        LIMIT ?
        """,
        [session_id] + list(params) + [top_n],
    ).fetchall()
    active_members = max(0, member_total - silent_total)
    return {
        "member_total": member_total,
        "active_members": active_members,
        "silent_total": silent_total,
        "active_rate": round(active_members / member_total, 4) if member_total else 0.0,
        "silent_members": [{"username": r["u"], "display_name": r["name"]} for r in rows],
    }


def _sessionization(
    cur: sqlite3.Cursor,
    where_sql: str,
    params: List[Any],
    divisor: int,
    total: int,
    gap_minutes: int,
) -> Optional[Dict[str, Any]]:
    """把连续消息按静默间隔切成「会话片段」，衡量讨论的节奏与断档。

    仅在指定单个会话时计算：跨会话的相邻消息没有「同一段对话」语义。
    """
    gap_threshold = max(1, int(gap_minutes)) * 60 * divisor
    row = cur.execute(
        f"""
        WITH gaps AS (
            SELECT m.create_time - LAG(m.create_time) OVER (ORDER BY m.create_time) AS gap
            FROM messages m WHERE {where_sql}
        )
        SELECT COUNT(gap) AS n,
               SUM(CASE WHEN gap > ? THEN 1 ELSE 0 END) AS breaks,
               MAX(gap) AS max_gap
        FROM gaps
        """,
        list(params) + [gap_threshold],
    ).fetchone()
    n = int(row["n"] or 0)
    breaks = int(row["breaks"] or 0)
    segment_count = (breaks + 1) if total else 0
    median_gap = None
    if n:
        median_row = cur.execute(
            f"""
            WITH gaps AS (
                SELECT m.create_time - LAG(m.create_time) OVER (ORDER BY m.create_time) AS gap
                FROM messages m WHERE {where_sql}
            )
            SELECT gap FROM gaps WHERE gap IS NOT NULL ORDER BY gap LIMIT 1 OFFSET ?
            """,
            list(params) + [max(0, (n - 1) // 2)],
        ).fetchone()
        median_gap = median_row["gap"] if median_row else None
    return {
        "gap_minutes": int(gap_minutes),
        "segment_count": segment_count,
        "avg_segment_messages": round(total / segment_count, 1) if segment_count else 0.0,
        "break_count": breaks,
        "median_gap_seconds": round(median_gap / divisor, 1) if median_gap is not None else None,
        "longest_silence_seconds": round((row["max_gap"] or 0) / divisor, 1),
    }


def _system_events(
    cur: sqlite3.Cursor,
    where_sql: str,
    params: List[Any],
    total: int,
) -> Dict[str, Any]:
    """系统事件：撤回类消息与系统提示消息的数量/占比。"""
    row = cur.execute(
        f"""
        SELECT SUM(CASE WHEN m.content LIKE '%撤回%' THEN 1 ELSE 0 END) AS recalls,
               SUM(CASE WHEN (m.local_type & {_BASE_TYPE_MASK}) IN (10000, 10002) THEN 1 ELSE 0 END) AS system_messages,
               SUM(CASE WHEN COALESCE(m.sender_username, '') = COALESCE(m.session_username, '') THEN 1 ELSE 0 END) AS group_self_messages
        FROM messages m WHERE {where_sql}
        """,
        params,
    ).fetchone()
    recalls = int(row["recalls"] or 0)
    return {
        "recall_like_count": recalls,
        "system_message_count": int(row["system_messages"] or 0),
        # 会话自身（群 / 系统）发出的消息数：不计入任何发言人维度，只在这里透明暴露
        "group_self_messages": int(row["group_self_messages"] or 0),
        "recall_rate": round(recalls / total, 4) if total else 0.0,
    }


def _concentration(members: List[Dict[str, Any]], top_n: int) -> Dict[str, Any]:
    """消息集中度：基尼系数、HHI 与 Top-N 份额。"""
    counts = [int(m.get("messages") or 0) for m in members]
    total = sum(counts)
    if total <= 0:
        return {"gini": 0.0, "hhi": 0.0, "top_n": top_n, "top_n_share": 0.0}
    ordered = sorted(counts, reverse=True)
    return {
        "gini": _gini(counts),
        "hhi": _hhi(counts),
        "top_n": top_n,
        "top_n_share": round(sum(ordered[:top_n]) / total, 4),
    }


def _member_payload(
    rows: List[sqlite3.Row],
    top_n: int,
    quoted: Optional[Counter] = None,
    bursts: Optional[Dict[str, int]] = None,
) -> tuple:
    """把聚合行转成可消费的成员列表，并给出各维度领跑者。

    被引用次数只能按显示名匹配（引用消息不含稳定 ID），同名用户会合并计数。
    """
    quoted = quoted or Counter()
    bursts = bursts or {}
    members: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["quoted"] = int(quoted.get(item.get("name") or "", 0))
        item["bursts"] = int(bursts.get(item.get("u"), 0))
        members.append(item)

    def leaders(field: str, size: int = 3) -> List[Dict[str, Any]]:
        ordered = sorted(members, key=lambda m: m.get(field) or 0, reverse=True)
        return [
            {"username": m["u"], "display_name": m["name"], "value": m[field]}
            for m in ordered[:size]
            if (m.get(field) or 0) > 0
        ]

    leader_fields = (
        "messages", "chars", "images", "videos", "emojis",
        "quotes_given", "quoted", "recall_like_events", "night", "early", "bursts",
    )
    return members[:top_n], {field: leaders(field) for field in leader_fields}


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
def search_wechat_messages(
    query: str,
    session_id: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    sender: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    order: str = "desc",
) -> List[Dict[str, Any]]:
    """全文搜索绿泡泡聊天记录（多词 AND，可限定会话 / 发言人 / 时间窗）。

    Args:
        query: 检索关键词（空格分隔 = 多词 AND）
        session_id: 可选，限定某个群聊或联系人
        limit: 返回数量，默认 20（上限 500）
        offset: 分页偏移，默认 0
        sender: 可选，只看某个成员发的消息（username / 备注 / 显示名，按会话消歧）
        start: 可选，起始时间（需与 end 同时提供）
        end: 可选，结束时间
        order: 'desc'（默认，最新优先）或 'asc'

    每条结果附带 ``snippet``（命中上下文），便于调用方直接展示或喂给模型。
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

    # 会话 / 时间窗条件复用统一的区间构造（时间必须成对提供，只传其一会报错）
    try:
        where_sql, params = _range_where(cur, session_id, start, end)
    except ValueError as exc:
        conn.close()
        return [{"error": str(exc)}]

    sql = f"""
        SELECT m.id, m.session_username, s.display_name AS session_name,
               m.sender_username, c.display_name AS sender_name,
               m.create_time, m.content, m.emoticon_md5
       FROM messages m
       LEFT JOIN sessions s ON s.username = m.session_username
       LEFT JOIN contacts c ON c.username = m.sender_username
       WHERE {where_sql}
    """
    for token in tokens:
        sql += " AND m.content LIKE ? ESCAPE '\\'"
        params.append(f"%{_escape_like(token)}%")
    if sender:
        # 显示名不唯一（同名不同 wxid），必须按会话消歧，统一交给 _resolve_member
        target = _resolve_member(cur, sender, session_id)
        if not target:
            conn.close()
            return [{"error": f"未找到成员：{sender}（可先用 search_contacts 或 list_chatroom_members 查询）"}]
        sql += " AND m.sender_username = ?"
        params.append(target["username"])

    direction = "ASC" if str(order or "").strip().lower() == "asc" else "DESC"
    sql += f" ORDER BY m.create_time {direction} LIMIT ? OFFSET ?"
    params.extend([_clamp(limit, 20, 500), _safe_offset(offset)])

    rows = cur.execute(sql, params).fetchall()
    conn.close()

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
            # 表情消息的 md5：面板据此调 get_emoticon_image 取表情图（缺省为 None）
            "emoticon_md5": r["emoticon_md5"],
            # 命中上下文：调用方无需自己截断长消息（多词命中取最靠前的一个）
            "snippet": _snippet(r["content"], tokens),
        })
    return results

@mcp.tool()
@_db_tool
def list_wechat_sessions(keyword: Optional[str] = None, session_type: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
    """查询绿泡泡会话列表（用于查找特定联系人或群聊的唯一 ID）。

    Args:
        keyword: 会话名称搜索关键字（群名/人名/备注）
        session_type: 会话类型筛选，可选：'room'（群聊）、'c2c'（单聊）、'gh'（公众号）
        limit: 返回记录数，默认 30

    ``unread`` 是 ETL 时从源库 Session 表读取的未读数快照（旧库可能为 0，
    需重建索引后才有值）。
    """
    err = _check_db_initialized()
    if err:
        return [{"error": err}]
    conn = get_connection()
    cur = conn.cursor()

    # 旧库可能还没迁移出 unread 列：按列存在性选表达式，避免整条查询报错
    unread_expr = "unread" if _has_column(cur, "sessions", "unread") else "0"

    sql = (
        "SELECT username, type, display_name, message_count, last_time, last_msg_preview, "
        f"{unread_expr} AS unread FROM sessions WHERE 1=1"
    )
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
            "unread": int(r["unread"] or 0),
        })
    conn.close()
    return results

@mcp.tool()
@_db_tool
def get_unread_sessions(min_unread: int = 1, session_type: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
    """列出有未读消息的会话（按最近活跃倒序），用于「还有哪些群没看」这类问题。

    未读数是 ETL 时从源库 Session 表读取的快照（``sessions.unread``），
    反映最后一次同步时刻的状态；旧库尚未重建索引时该列可能全为 0。

    Args:
        min_unread: 未读数下限，默认 1（只看真的有未读的会话）
        session_type: 可选，会话类型筛选：'room' / 'c2c' / 'gh'
        limit: 返回记录数，默认 30（上限 500）
    """
    err = _check_db_initialized()
    if err:
        return [{"error": err}]
    conn = get_connection()
    cur = conn.cursor()
    if not _has_column(cur, "sessions", "unread"):
        conn.close()
        return [{"error": "当前数据库缺少 sessions.unread 列，请重新运行 wechat-init 重建索引"}]

    sql = """
        SELECT username, type, display_name, message_count, last_time, last_msg_preview, unread
        FROM sessions WHERE unread >= ?
    """
    params: List[Any] = [_clamp(min_unread, 1, 100000)]
    if session_type:
        sql += " AND type = ?"
        params.append(session_type)
    sql += " ORDER BY last_time DESC LIMIT ?"
    params.append(_clamp(limit, 30, 500))

    rows = cur.execute(sql, params).fetchall()
    conn.close()
    return [
        {
            "session_id": r["username"],
            "display_name": r["display_name"],
            "type": r["type"],
            "message_count": r["message_count"],
            "last_time": _ts_to_str(r["last_time"]),
            "last_message": r["last_msg_preview"],
            "unread": int(r["unread"] or 0),
        }
        for r in rows
    ]

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
               m.create_time, m.content, m.emoticon_md5
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
            "emoticon_md5": r["emoticon_md5"],
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


@mcp.tool()
@_db_tool
def get_emoticon_image(md5: str) -> Dict[str, Any]:
    """按表情 md5 取本地明文表情图（自定义表情 / 表情包）。

    表情消息的 `content` 只有 `[表情]` 三个字，但返回里额外带 `emoticon_md5`
    字段 —— 把它传进本工具即可拿到可直接渲染的图片文件路径（gif / png / jpg /
    webp / APNG），供面板等宿主读盘展示。

    前置：需要先用 `prepare_emoticons` 生成明文表情库（微信的表情文件是账号级
    加密的，解密要做一次 GPU 搜索，不适合放进每次调用）。表情库未准备、或该
    表情未被本地缓存时，返回 `error` 与可操作提示。

    Args:
        md5: 表情标识，来自消息返回的 `emoticon_md5`
    """
    return _get_emoticon_image(md5=md5)


@mcp.tool()
def prepare_emoticons() -> Dict[str, Any]:
    """生成 / 刷新本地明文表情库（一次性操作，供 `get_emoticon_image` 使用）。

    微信 4.x 的自定义表情文件（`business/emoticon/Persist/<两位>/<md5>:<ext>`）
    使用账号级密钥加密：

        key = MD5(f"{seed}{wxid}EMOTICON").digest()
        plaintext = AES-128-CBC(key, iv=key).decrypt(ciphertext)

    其中 `seed` 落在 `0..2^32` 空间、需要 GPU/SIMD 暴力搜索，因此这一步委托外部
    工具 wxemo（https://github.com/azazo1/wxemo）完成；本工具负责调用它，并把
    产物摊平成 `~/.wechat_mcp/emoticons/<md5>.<ext>` 供快速查询。

    需要：wxemo 在 PATH 中（或用环境变量 `WXEMO_BIN` 指定完整路径），且已跑过
    一次 `wxemo seed search`（seed 缓存在 wxemo 自己的配置目录里）。wxgf 私有
    格式的转码还需要微信自带的 VoipEngine.dll（会自动从运行中的微信进程定位，
    找不到时只跳过 wxgf 那一部分，原生 gif/png/jpg 照常可用）。
    """
    return _prepare_emoticons()


@mcp.tool()
@_db_tool
def get_chat_record(message_id: int, include_raw: bool = False) -> Dict[str, Any]:
    """展开「聊天记录」合并转发消息（appmsg 子类型 19）。

    合并转发消息在本地索引里只剩 `[聊天记录] 标题` 一行，被合并的正文只存在于解密快照
    的源库中。本工具回源库取出原始 XML，把 recorditem 解析成结构化条目（文本 / 图片 /
    视频 / 文件…），供宿主渲染成可展开的卡片。

    Args:
        message_id: 消息 ID（来自各查询工具返回项的 id 字段）
        include_raw: 是否附带原始 XML（调试用，默认 False）

    Returns:
        {"success": True, "title": 标题, "desc": 摘要, "count": 条目数,
         "items": [{"type", "datatype", "desc", "sender", "time", "format", "md5", ...}]}
    """
    err = _check_db_initialized()
    if err:
        return {"error": err}
    return _get_chat_record(message_id=message_id, include_raw=include_raw)


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
               m.create_time, m.content, m.emoticon_md5
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
            "emoticon_md5": r["emoticon_md5"],
        })
    conn.close()
    return results

@mcp.tool()
@_db_tool
def sync_wechat_database(force: bool = False, redecrypt: bool = True) -> Dict[str, Any]:
    """触发数据导入同步，使本地 MCP 数据库保持最新状态（**阻塞**直到完成）。

    注意：绿泡泡数据库是加密的，本地索引读的是**解密快照**。只重建 ETL 读到的仍是旧
    快照（永远拿不到新消息），因此本工具默认会**先重新解密源库**再重建索引。

    ⚠️ 耗时不固定：数据量小时几秒；解密快照接近 1GB 时「重新解密 + 重建索引」可能超过
    100 秒。**调用方若对单次工具调用有硬超时（例如 120s），请改用 `start_sync`**
    （后台执行，用 `get_auto_sync_status` 查结果），不要靠延长超时。

    Args:
        force: 是否强制全量重建索引（忽略指纹，默认 False）
        redecrypt: 是否先重新解密源库（默认 True；设为 False 则只重建索引）
    """
    return sync_now(force=force, redecrypt=redecrypt)


@mcp.tool()
@_db_tool
def start_sync(force: bool = False, redecrypt: bool = True) -> Dict[str, Any]:
    """在**后台**执行一次完整同步并立即返回（推荐给有调用超时的客户端）。

    与 `sync_wechat_database` 的唯一区别是不阻塞等待结果：适合宿主/客户端对单次工具调用
    有硬超时的场景（同步本身可能要 1–3 分钟，超时会被误报成失败）。

    进度与结果查询：`get_auto_sync_status` 的 `syncing`（是否仍在跑）与 `last_run`
    （上次结果、耗时、`run_count` 递增）。

    Args:
        force: 是否强制全量重建索引（忽略指纹，默认 False）
        redecrypt: 是否先重新解密源库（默认 True；设为 False 则只重建索引）
    """
    return _start_sync(force=force, redecrypt=redecrypt)


@mcp.tool()
@_db_tool
def set_auto_sync(enabled: bool, interval_minutes: Optional[int] = None) -> Dict[str, Any]:
    """开启 / 关闭**定时自动同步**（重新解密源库 + 重建索引）。

    定时器在本 MCP 进程内以后台线程运行，因此只在 MCP 服务存活期间生效（随 Snow App
    启停）；开关、间隔与运行记录持久化到 ~/.wechat_mcp/auto_sync.json，服务重启后自动恢复。

    每个周期会执行一次完整同步（等价于调用 sync_wechat_database），因此间隔不宜过短。

    Args:
        enabled: True 开启，False 关闭
        interval_minutes: 同步间隔（分钟），钳制到 5–1440。
                          省略则沿用上次设置（从未设置过时为 30）。
    """
    return _set_auto_sync(enabled=enabled, interval_minutes=interval_minutes)


@mcp.tool()
@_db_tool
def get_auto_sync_status() -> Dict[str, Any]:
    """查看定时自动同步的状态：开关、间隔、运行次数、上次运行结果。

    用于确认「定时同步到底有没有在跑」以及上一轮同步是否成功。
    """
    return _auto_sync_status()


@mcp.tool()
def extract_image_key() -> Dict[str, Any]:
    """从运行中的绿泡泡进程内存提取**图片解密密钥**（微信 4.x V2 加密图片）。

    背景：微信 4.x 的聊天图片（.dat）有三代格式 —— 旧 XOR 格式可离线反推密钥、
    V1 用固定常量；**V2 的 AES 密钥是账号级的，只在该账号查看图片时临时缓存于
    Weixin.exe 进程内存**，磁盘上没有任何副本。缺它时 `get_message_image` 对 V2
    图片一律返回 `WeChat 4.x V2 image encryption requires image_aes_key`。

    使用流程（顺序很重要）：先在绿泡泡客户端点开 1-2 张图片（看大图，让密钥进入
    内存），然后**立刻**调用本工具。成功后密钥会写入密钥文件（`image_aes_key`
    字段），之后 `get_message_image` 即可直接解密，无需重复提取。

    返回 `{"ok": true, "key_masked": "7396...f5a6", "format": "jpeg", ...}`；
    未命中时 `ok=false` 并说明原因（通常是还没在客户端看图，或需要管理员权限）。
    """
    from .tools.key_tool import extract_image_key as _extract_image_key
    return _extract_image_key()

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
def get_messages_by_range(start: str, end: str, session_id: Optional[str] = None, limit: int = 100, offset: int = 0, order: str = "asc", sender: Optional[str] = None) -> List[Dict[str, Any]]:
    """按自定义时间范围查询聊天记录（支持跨天与任意时段，比按单日查询更灵活）。

    默认按时间正序返回；需要「最新 N 条」这类场景时传 ``order="desc"``，
    配合 ``offset`` 即可从最新往更早翻页（desc 下 offset 跳过的是最新的记录）。
    范围内消息数超过 limit 时请用 offset 翻页。

    Args:
        start: 起始时间，'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM[:SS]'
        end: 结束时间，同上；只给日期时自动覆盖当天全天
        session_id: 可选，限定某个群聊或联系人
        limit: 返回数量，默认 100（上限 500）
        offset: 跳过前 N 条（默认 0），用于分页
        order: 'asc'（默认，时间正序）或 'desc'（时间倒序，取最新优先）
        sender: 可选，只看某个成员发的消息（username / 备注 / 显示名）；
            与 session_id 组合即可取「某人在某群某时段的全部发言」
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
               m.create_time, m.content, m.emoticon_md5
        FROM messages m
        LEFT JOIN sessions s ON s.username = m.session_username
        LEFT JOIN contacts c ON c.username = m.sender_username
        WHERE m.create_time >= ? AND m.create_time < ?
    """
    params: List[Any] = [start_ts, end_ts]
    if session_id:
        sql += " AND m.session_username = ?"
        params.append(session_id)
    if sender:
        # 按发言人过滤：与 session_id 组合即可取「某人在某群某时段的全部发言」
        target = _resolve_member(cur, sender, session_id)
        if not target:
            conn.close()
            return [{"error": f"未找到成员：{sender}（可先用 search_contacts 或 list_chatroom_members 查询）"}]
        sql += " AND m.sender_username = ?"
        params.append(target["username"])
    direction = "DESC" if str(order or "").strip().lower() == "desc" else "ASC"
    sql += f" ORDER BY m.create_time {direction} LIMIT ? OFFSET ?"
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
            "emoticon_md5": r["emoticon_md5"],
        }
        for r in rows
    ]

@mcp.tool()
@_db_tool
def get_messages_since(
    after_id: Optional[int] = None,
    after_time: Optional[str] = None,
    session_id: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """增量拉取游标之后的新消息（供面板 / 脚本做增量刷新，避免重复拉全量）。

    游标给 ``after_id``（推荐：messages.id 自增序，绝对无重复）或 ``after_time``
    （'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM[:SS]'），两者都未提供则报错。

    ⚠️ **时间游标是「含」语义（>=），且只能精确到秒**：同一秒内可能有任意多条消息，
    只用 ``after_time`` 续拉会重复返回该秒已读到的消息（但不会再漏），调用方应按
    ``next_cursor`` 去重。要「不漏也不重」必须同时传 ``after_time + after_id``，
    此时按 ``(create_time, id)`` 复合游标推进 —— 这是推荐用法。

    只向「更新」方向推进：给 ``after_time`` 时按 ``(create_time, id)`` 升序返回，
    只给 ``after_id`` 时按 id 升序返回；``next_cursor`` 是本批最后一条的 id，
    下次原样回传即可续拉；``next_time`` 是同一位置的可读时间（配合 next_cursor 使用）。
    ``has_more`` 为真表示还有更多未取完（内部多取一条探测，不会漏批）。

    Args:
        after_id: 只取 id 大于该值的消息（增量游标）
        after_time: 时间下界（**含**该时刻；建议与 after_id 一起传）
        session_id: 可选，限定某个群聊或联系人
        limit: 单批条数，默认 200（上限 500）
    """
    err = _check_db_initialized()
    if err:
        return {"error": err}
    if after_id is None and not (after_time or "").strip():
        return {"error": "after_id 与 after_time 至少提供一个（增量游标）"}

    conn = get_connection()
    cur = conn.cursor()

    where = ["1=1"]
    params: List[Any] = []
    if session_id:
        where.append("m.session_username = ?")
        params.append(session_id)

    cursor_id: Optional[int] = None
    if after_id is not None:
        try:
            cursor_id = int(after_id)
        except (TypeError, ValueError):
            conn.close()
            return {"error": "after_id 必须是整数"}

    time_bound: Optional[int] = None
    if (after_time or "").strip():
        try:
            bound = _parse_time_bound(after_time)
        except ValueError as exc:
            conn.close()
            return {"error": str(exc)}
        max_ts = cur.execute("SELECT max(create_time) FROM messages").fetchone()[0] or 0
        time_bound = int(bound.timestamp() * _ts_unit(max_ts))

    if time_bound is not None and cursor_id is not None:
        # 复合游标 (create_time, id)：同一秒内继续按 id 推进，既不漏也不重。
        where.append("(m.create_time > ? OR (m.create_time = ? AND m.id > ?))")
        params.extend([time_bound, time_bound, cursor_id])
    elif time_bound is not None:
        # 只有时间时必须用 >=：旧的严格 > 会让「同一秒内超过一页」的消息永久丢失。
        where.append("m.create_time >= ?")
        params.append(time_bound)
    elif cursor_id is not None:
        where.append("m.id > ?")
        params.append(cursor_id)

    size = _clamp(limit, 200, 500)
    # 时间游标必须按时间排序：messages.id 是 ETL 写入序，不保证与 create_time 同序；
    # 按 id 排序会让「同一秒/更早时间但 id 更大」的消息错位，续拉时被跳过。
    order_sql = "m.create_time ASC, m.id ASC" if time_bound is not None else "m.id ASC"
    sql = f"""
        SELECT m.id, m.session_username, s.display_name AS session_name,
               m.sender_username, c.display_name AS sender_name,
               m.create_time, m.content, m.emoticon_md5
        FROM messages m
        LEFT JOIN sessions s ON s.username = m.session_username
        LEFT JOIN contacts c ON c.username = m.sender_username
        WHERE {' AND '.join(where)}
        ORDER BY {order_sql} LIMIT ?
    """
    rows = cur.execute(sql, params + [size + 1]).fetchall()
    conn.close()

    has_more = len(rows) > size
    rows = rows[:size]
    messages = [
        {
            "id": r["id"],
            "session_id": r["session_username"],
            "session_name": r["session_name"] or r["session_username"],
            "sender_name": r["sender_name"] or r["sender_username"],
            "sender_username": r["sender_username"],
            "time": _ts_to_str(r["create_time"]),
            "content": r["content"],
            "emoticon_md5": r["emoticon_md5"],
        }
        for r in rows
    ]
    return {
        "messages": messages,
        "count": len(messages),
        "has_more": has_more,
        # 游标停在「本批最后一条」；一批都没取到时回退成调用方给的 after_id
        "next_cursor": rows[-1]["id"] if rows else (int(after_id) if after_id is not None else None),
        "next_time": _ts_to_str(rows[-1]["create_time"]) if rows else None,
    }


@mcp.tool()
@_db_tool
def get_session_overview(
    session_id: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    top_types: int = 5,
) -> Dict[str, Any]:
    """单个会话的轻量概览（切群首屏卡片用），只做少量聚合、返回很快。

    返回会话元信息（名称 / 类型 / 累计条数 / 未读快照）、范围内的消息总量、
    发言人数、峰值时段、类型 TopN 与范围内首末消息时间。

    ⚠️ ``peak_hour`` 是 0-23 合并口径（跨天窗口会把不同日期的同一小时合并），
    需要跨天逐小时序列时请改用 ``get_chat_activity_stats`` 的 ``hourly_series``；
    需要成员榜 / 互动网络等完整画像时改用 ``get_group_digest``。

    Args:
        session_id: 会话唯一 ID
        start: 可选，起始时间（需与 end 同时提供）
        end: 可选，结束时间
        top_types: 返回的消息类型条数，默认 5（上限 20）
    """
    err = _check_db_initialized()
    if err:
        return {"error": err}
    conn = get_connection()
    cur = conn.cursor()

    unread_expr = "unread" if _has_column(cur, "sessions", "unread") else "0"
    meta = cur.execute(
        f"SELECT username, type, display_name, message_count, last_time, {unread_expr} AS unread "
        "FROM sessions WHERE username = ?",
        (session_id,),
    ).fetchone()

    try:
        where_sql, params = _range_where(cur, session_id, start, end)
    except ValueError as exc:
        conn.close()
        return {"error": str(exc)}

    max_ts = cur.execute("SELECT max(create_time) FROM messages").fetchone()[0] or 0
    divisor = _ts_unit(max_ts)

    row = cur.execute(f"""
        SELECT COUNT(*) AS total,
               COUNT(DISTINCT CASE WHEN {_SELF_SENDER_FILTER} THEN m.sender_username END) AS senders,
               MIN(m.create_time) AS first_ts,
               MAX(m.create_time) AS last_ts
        FROM messages m WHERE {where_sql}
    """, params).fetchone()

    # 峰值时段：0-23 合并口径（轻量概览够用），并列时取更早的小时保证结果稳定
    peak = cur.execute(f"""
        SELECT strftime('%H', datetime(create_time / {divisor}, 'unixepoch', 'localtime')) AS h,
               COUNT(*) AS n
        FROM messages m WHERE {where_sql}
        GROUP BY h ORDER BY n DESC, h ASC LIMIT 1
    """, params).fetchone()

    type_rows = cur.execute(f"""
        SELECT m.local_type AS t, COUNT(*) AS n
        FROM messages m WHERE {where_sql}
        GROUP BY m.local_type ORDER BY n DESC LIMIT ?
    """, params + [_clamp(top_types, 5, 20)]).fetchall()
    conn.close()

    return {
        "session_id": session_id,
        "session": {
            "display_name": (meta["display_name"] if meta else None) or session_id,
            "type": meta["type"] if meta else None,
            "message_count": int(meta["message_count"] or 0) if meta else 0,
            "last_time": _ts_to_str(meta["last_time"]) if meta else "",
            "unread": int(meta["unread"] or 0) if meta else 0,
        },
        "time_range": {"start": start, "end": end} if (start and end) else None,
        "total_messages": row["total"] or 0,
        "unique_senders": row["senders"] or 0,
        "peak_hour": {"hour": peak["h"], "count": peak["n"]} if peak else None,
        "type_distribution": [
            {"local_type": r["t"], "label": type_label(r["t"]), "count": r["n"]} for r in type_rows
        ],
        "first_message": _ts_to_str(row["first_ts"] or 0),
        "last_message": _ts_to_str(row["last_ts"] or 0),
    }


def _resolve_member(
    cur: sqlite3.Cursor, target: str, session_id: Optional[str] = None
) -> Optional[sqlite3.Row]:
    """把 username / 备注 / 显示名解析成成员行。

    ⚠️ 显示名不唯一：同一个「12」可能对应多个 wxid（一个是好友、一个是群成员）。
    因此给了 ``session_id`` 时，先用「该群内这个名字的真实发言量」消歧 —— 这正是
    成员画像 / 按人检索消息想要的答案；否则才回退 contacts 精确 / 群成员表 / 模糊匹配。
    """
    target = (target or "").strip()
    if not target:
        return None

    # 1) 明显是 wxid / 群号：按 username 精确匹配（最可靠，无需消歧）
    row = cur.execute(
        "SELECT username, display_name FROM contacts WHERE username = ? LIMIT 1", (target,)
    ).fetchone()
    if row:
        return row
    row = cur.execute(
        "SELECT member_username AS username, display_name FROM chatroom_members "
        "WHERE member_username = ? LIMIT 1",
        (target,),
    ).fetchone()
    if row:
        return row

    # 2) 指定了群：按「该群内这个名字的发言量」消歧（解决同名不同 wxid）
    if session_id:
        row = cur.execute(
            """
            SELECT m.sender_username AS username,
                   COALESCE(NULLIF(c.display_name, ''), m.sender_username) AS display_name,
                   COUNT(*) AS n
            FROM messages m
            LEFT JOIN contacts c ON c.username = m.sender_username
            WHERE m.session_username = ?
              AND (m.sender_username = ? OR c.display_name = ? OR c.alias = ?)
            GROUP BY m.sender_username
            ORDER BY n DESC LIMIT 1
            """,
            (session_id, target, target, target),
        ).fetchone()
        if row:
            return row
        # 该群成员表里按显示名找（没发过言的人也能解析出来）
        row = cur.execute(
            "SELECT member_username AS username, display_name FROM chatroom_members "
            "WHERE chatroom_username = ? AND display_name = ? LIMIT 1",
            (session_id, target),
        ).fetchone()
        if row:
            return row

    # 3) contacts 精确（备注 / 显示名）
    row = cur.execute(
        "SELECT username, display_name FROM contacts WHERE alias = ? OR display_name = ? LIMIT 1",
        (target, target),
    ).fetchone()
    if row:
        return row

    # 4) 任意群的群成员表精确
    row = cur.execute(
        "SELECT member_username AS username, display_name FROM chatroom_members "
        "WHERE display_name = ? LIMIT 1",
        (target,),
    ).fetchone()
    if row:
        return row

    # 5) 模糊匹配（最后手段）
    row = cur.execute(
        "SELECT username, display_name FROM contacts "
        "WHERE display_name LIKE ? ESCAPE '\\' LIMIT 1",
        (f"%{_escape_like(target)}%",),
    ).fetchone()
    if row:
        return row
    return cur.execute(
        "SELECT member_username AS username, display_name FROM chatroom_members "
        "WHERE display_name LIKE ? ESCAPE '\\' LIMIT 1",
        (f"%{_escape_like(target)}%",),
    ).fetchone()


def _activity_stats_payload(
    session_id: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    gap_minutes: int = 30,
    top_n: int = 10,
) -> Dict[str, Any]:
    """统计聊天活跃度：消息总量、按小时/按天分布、发言最多的人，以及参与度指标。

    除消息条数外，还返回**去重发言人数**（``unique_senders`` /
    ``hourly_unique_senders`` / ``daily_unique_senders``）、参与度分桶
    （``sender_buckets``）、星期×小时热力（``weekday_hourly``）与消息类型分布
    （``type_distribution``，结构为 ``[{local_type, label, count}]``）。这些指标
    全部在 SQL 侧对全量数据聚合，调用方无需分页拉取消息。

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

    try:
        where_sql, params = _range_where(cur, session_id, start, end)
    except ValueError as exc:
        conn.close()
        return {"error": str(exc)}

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
        WHERE {where_sql} AND {_SELF_SENDER_FILTER}
        GROUP BY m.sender_username
        ORDER BY n DESC LIMIT 10
    """, params).fetchall()

    # 发言人维度指标：全部在 SQL 侧对全量数据聚合，避免调用方分页拉取。
    unique_senders = cur.execute(
        f"SELECT COUNT(DISTINCT m.sender_username) FROM messages m WHERE {where_sql} AND {_SELF_SENDER_FILTER}",
        params,
    ).fetchone()[0]
    hourly_unique = cur.execute(f"""
        SELECT strftime('%H', datetime(create_time / {divisor}, 'unixepoch', 'localtime')) AS h,
               COUNT(DISTINCT m.sender_username) AS n
        FROM messages m WHERE {where_sql} AND {_SELF_SENDER_FILTER}
        GROUP BY h ORDER BY h
    """, params).fetchall()
    daily_unique = cur.execute(f"""
        SELECT strftime('%Y-%m-%d', datetime(create_time / {divisor}, 'unixepoch', 'localtime')) AS d,
               COUNT(DISTINCT m.sender_username) AS n
        FROM messages m WHERE {where_sql} AND {_SELF_SENDER_FILTER}
        GROUP BY d ORDER BY d
    """, params).fetchall()
    sender_buckets = cur.execute(f"""
        WITH per_sender AS (
            SELECT m.sender_username AS u, COUNT(*) AS c
            FROM messages m WHERE {where_sql} AND {_SELF_SENDER_FILTER}
            GROUP BY m.sender_username
        )
        SELECT CASE
                 WHEN c = 1 THEN '1'
                 WHEN c <= 9 THEN '2-9'
                 WHEN c <= 49 THEN '10-49'
                 ELSE '50+'
               END AS bucket,
               COUNT(*) AS n
        FROM per_sender GROUP BY bucket
    """, params).fetchall()
    weekday_hourly = cur.execute(f"""
        SELECT strftime('%w', datetime(create_time / {divisor}, 'unixepoch', 'localtime')) AS w,
               strftime('%H', datetime(create_time / {divisor}, 'unixepoch', 'localtime')) AS h,
               COUNT(*) AS n
        FROM messages m WHERE {where_sql}
        GROUP BY w, h
    """, params).fetchall()
    type_rows = cur.execute(f"""
        SELECT m.local_type AS t, COUNT(*) AS n
        FROM messages m WHERE {where_sql}
        GROUP BY m.local_type ORDER BY n DESC
    """, params).fetchall()
    # 绝对时间的小时序列：跨天滚动窗口（如「近 24 小时」）必须按「年-月-日 时」分桶，
    # 否则 hourly_distribution 会把「昨天 20 点」与「今天 20 点」合并成一根柱。
    # 仅在给出 start/end 时计算（无范围时全量按小时会返回海量桶）。
    hourly_series = []
    hourly_series_senders = []
    if start and end:
        hourly_series = cur.execute(f"""
            SELECT strftime('%Y-%m-%d %H', datetime(create_time / {divisor}, 'unixepoch', 'localtime')) AS k,
                   COUNT(*) AS n
            FROM messages m WHERE {where_sql}
            GROUP BY k ORDER BY k
        """, params).fetchall()
        hourly_series_senders = cur.execute(f"""
            SELECT strftime('%Y-%m-%d %H', datetime(create_time / {divisor}, 'unixepoch', 'localtime')) AS k,
                   COUNT(DISTINCT m.sender_username) AS n
            FROM messages m WHERE {where_sql} AND {_SELF_SENDER_FILTER}
            GROUP BY k ORDER BY k
        """, params).fetchall()

    # ---- 扩展维度：同一 where 范围内的全量聚合，只加字段、不改既有语义 ----
    gap_minutes = _clamp(gap_minutes, 30, 1440)
    top_n = _clamp(top_n, 10, 100)

    member_rows = _member_aggregate(cur, where_sql, params, divisor)
    bursts = _member_bursts(cur, where_sql, params, divisor)
    quote_payload, quoted_counter = _quote_network(cur, where_sql, params, top_n)
    member_rank, member_leaders = _member_payload(
        member_rows, top_n, quoted_counter, bursts
    )
    concentration = _concentration([dict(row) for row in member_rows], top_n)
    silent = _silent_members(cur, where_sql, params, session_id, top_n)
    sessionization = (
        _sessionization(cur, where_sql, params, divisor, total, gap_minutes)
        if session_id
        else None
    )
    system_events = _system_events(cur, where_sql, params, total)
    conn.close()

    # 结构化返回（local_type + label + count），而不是把展示文案当 key：
    # 面板、Agent、脚本都能按需消费，MCP 不耦合任何单一前端。
    type_distribution = [
        {
            "local_type": row["t"],
            "label": type_label(row["t"]),
            "count": row["n"],
        }
        for row in type_rows
    ]

    # 日趋势：在原始按天计数上补 7 日移动平均与日环比（纯派生数据）。
    daily_trend = []
    for i, row in enumerate(daily):
        window = [r["n"] for r in daily[max(0, i - 6): i + 1]]
        count = row["n"]
        prev = daily[i - 1]["n"] if i > 0 else None
        daily_trend.append({
            "date": row["d"],
            "messages": count,
            "ma7": round(sum(window) / len(window), 1),
            "dod_pct": round((count - prev) / prev * 100, 1) if prev else None,
        })

    return {
        "session_id": session_id or "(全部会话)",
        "time_range": {"start": start, "end": end} if (start and end) else None,
        "total_messages": total,
        "unique_senders": unique_senders,
        "hourly_distribution": {r["h"]: r["n"] for r in hourly},
        "hourly_series": [{"hour": r["k"], "count": r["n"]} for r in hourly_series],
        "hourly_series_senders": [{"hour": r["k"], "count": r["n"]} for r in hourly_series_senders],
        "hourly_unique_senders": {r["h"]: r["n"] for r in hourly_unique},
        "daily_distribution": {r["d"]: r["n"] for r in daily},
        "daily_unique_senders": {r["d"]: r["n"] for r in daily_unique},
        "daily_trend": daily_trend,
        "sender_buckets": {r["bucket"]: r["n"] for r in sender_buckets},
        "weekday_hourly": [
            {"weekday": int(r["w"]), "hour": r["h"], "count": r["n"]} for r in weekday_hourly
        ],
        "type_distribution": type_distribution,
        "top_senders": [
            {"display_name": r["name"], "username": r["username"], "message_count": r["n"]}
            for r in top_senders
        ],
        "sessionization": sessionization,
        "concentration": concentration,
        "member_rank": member_rank,
        "member_leaders": member_leaders,
        "silent_members": silent,
        "quote_network": quote_payload,
        "system_events": system_events,
    }

@mcp.tool()
@_db_tool
def get_chat_activity_stats(
    session_id: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    gap_minutes: int = 30,
    top_n: int = 10,
) -> Dict[str, Any]:
    """统计聊天活跃度：消息量、时间分布、发言人与互动结构。

    既有指标：``total_messages``、``unique_senders``、``hourly_distribution``、
    ``hourly_unique_senders``、``daily_distribution``、``daily_unique_senders``、
    ``sender_buckets``、``weekday_hourly``、``type_distribution``、``top_senders``。

    新增通用指标（只加字段，不改既有语义）：
        - ``daily_trend``：按天消息量 + 7 日移动平均 + 日环比
        - ``sessionization``：按静默间隔切分的会话片段（仅指定 session_id 时）
        - ``concentration``：基尼系数 / HHI / Top-N 份额
        - ``member_rank``：成员多维聚合（发言、字数、图片、视频、表情、引用、
          被引用、撤回类系统事件、深夜、早起、连发、首末发言时间）
        - ``member_leaders``：各维度领跑者（Top 3）
        - ``silent_members``：群成员活跃率与沉默成员（仅群聊）
        - ``quote_network``：引用关系边与被引用榜（按显示名，含解析率）
        - ``system_events``：撤回类消息与系统提示消息数量/占比

    Args:
        session_id: 可选，限定某个群聊或联系人（不传则统计全部会话）
        start: 可选，起始时间 'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM[:SS]'
        end: 可选，结束时间，同上（需与 start 同时提供）
        gap_minutes: 会话片段切分阈值（分钟），默认 30，范围 1–1440
        top_n: 榜单与被引用榜长度，默认 10，范围 1–100
    """
    return _activity_stats_payload(session_id, start, end, gap_minutes, top_n)


@mcp.tool()
@_db_tool
def get_group_digest(
    session_id: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    gap_minutes: int = 30,
    top_n: int = 10,
    highlights: int = 0,
) -> Dict[str, Any]:
    """一次取齐群聊日报 / 周报 / 年度报告所需的聚合数据。

    返回 ``get_chat_activity_stats`` 的全部字段，另加 ``highlights``：
    最长的若干条文本消息（供 Agent 提炼金句与摘要），条数由 ``highlights`` 指定
    （0 = 不返回，上限 50）。所有字段都是结构化数据，不含展示文案。

    Args:
        session_id: 群聊或联系人 ID（可用 list_wechat_sessions 查询）
        start: 可选，起始时间 'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM[:SS]'
        end: 可选，结束时间，同上（需与 start 同时提供）
        gap_minutes: 会话片段切分阈值（分钟），默认 30
        top_n: 榜单长度，默认 10
        highlights: 返回的最长文本消息条数，默认 0（不返回），上限 50
    """
    payload = _activity_stats_payload(session_id, start, end, gap_minutes, top_n)
    if not payload or "error" in payload:
        return payload

    limit = _clamp(highlights, 0, 50)
    if limit <= 0:
        payload["highlights"] = []
        return payload

    conn = get_connection()
    cur = conn.cursor()
    try:
        where_sql, params = _range_where(cur, session_id, start, end)
    except ValueError as exc:
        conn.close()
        return {"error": str(exc)}

    rows = cur.execute(
        f"""
        SELECT m.sender_username AS u,
               COALESCE(NULLIF(c.display_name, ''), m.sender_username) AS name,
               m.content AS content,
               m.create_time AS create_time
        FROM messages m
        LEFT JOIN contacts c ON c.username = m.sender_username
        WHERE {where_sql} AND LENGTH(COALESCE(m.content, '')) >= 80
        ORDER BY LENGTH(m.content) DESC
        LIMIT ?
        """,
        list(params) + [limit],
    ).fetchall()
    conn.close()

    payload["highlights"] = [
        {
            "username": r["u"],
            "display_name": r["name"],
            "time": _ts_to_str(r["create_time"]),
            "chars": len(r["content"] or ""),
            "content": r["content"],
        }
        for r in rows
    ]
    return payload


@mcp.tool()
@_db_tool
def get_member_profile(
    member: str,
    session_id: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    top_n: int = 10,
    include_samples: int = 0,
) -> Dict[str, Any]:
    """单个成员的群聊画像：活跃节奏、内容结构、互动对象与关键指标。

    ``member`` 支持 username / 备注 / 显示名（先精确匹配，再模糊匹配）。
    返回的是统计事实，不含「称号」等展示判断；称号可由 Agent 或面板派生。

    Args:
        member: 成员 username、备注或显示名
        session_id: 可选，限定某个群聊（不传则统计全部会话）
        start: 可选，起始时间 'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM[:SS]'
        end: 可选，结束时间，同上（需与 start 同时提供）
        top_n: 互动对象等榜单长度，默认 10，范围 1–100
        include_samples: 可选，附带该成员最近 N 条原始消息（0 = 不返回，上限 200）；
            传 20–50 即可让 Agent 直接基于原文写画像，不必再翻整个群
    """
    err = _check_db_initialized()
    if err:
        return {"error": err}
    if not (member or "").strip():
        return {"error": "member 不能为空"}
    target = member.strip()
    top_n = _clamp(top_n, 10, 100)

    conn = get_connection()
    cur = conn.cursor()

    # 先查联系人（好友），再回退群成员表（群成员未必是好友）
    row = _resolve_member(cur, target, session_id)
    if not row:
        conn.close()
        return {
            "error": f"未找到成员：{member}（可先用 search_contacts 或 list_chatroom_members 查询）"
        }

    username = row["username"]
    display_name = row["display_name"] or username
    try:
        where_sql, params = _range_where(cur, session_id, start, end)
    except ValueError as exc:
        conn.close()
        return {"error": str(exc)}

    max_ts = cur.execute("SELECT max(create_time) FROM messages").fetchone()[0] or 0
    divisor = _ts_unit(max_ts)
    scope_where = f"{where_sql} AND m.sender_username = ?"
    scope_params = list(params) + [username]

    summary = cur.execute(
        f"""
        SELECT COUNT(*) AS messages,
               COALESCE(SUM(LENGTH(COALESCE(m.content, ''))), 0) AS chars,
               COALESCE(MAX(LENGTH(COALESCE(m.content, ''))), 0) AS longest,
               MIN(m.create_time) AS first_time,
               MAX(m.create_time) AS last_time,
               COUNT(DISTINCT strftime('%Y-%m-%d', datetime(m.create_time / {divisor}, 'unixepoch', 'localtime'))) AS active_days,
               SUM(CASE WHEN (m.local_type & {_BASE_TYPE_MASK}) = 3 OR (m.local_type >> 32) = 3 THEN 1 ELSE 0 END) AS images,
               SUM(CASE WHEN (m.local_type >> 32) = 6 THEN 1 ELSE 0 END) AS files,
               SUM(CASE WHEN (m.local_type & {_BASE_TYPE_MASK}) = 43 OR (m.local_type >> 32) = 4 THEN 1 ELSE 0 END) AS videos,
               SUM(CASE WHEN (m.local_type & {_BASE_TYPE_MASK}) = 47 OR (m.local_type >> 32) = 8 THEN 1 ELSE 0 END) AS emojis,
               SUM(CASE WHEN (m.local_type >> 32) = 57 THEN 1 ELSE 0 END) AS quotes_given,
               SUM(CASE WHEN m.content LIKE '%撤回%' THEN 1 ELSE 0 END) AS recall_like_events,
               SUM(CASE WHEN CAST(strftime('%H', datetime(m.create_time / {divisor}, 'unixepoch', 'localtime')) AS INTEGER) < 6 THEN 1 ELSE 0 END) AS night,
               SUM(CASE WHEN CAST(strftime('%H', datetime(m.create_time / {divisor}, 'unixepoch', 'localtime')) AS INTEGER) BETWEEN 6 AND 8 THEN 1 ELSE 0 END) AS early
        FROM messages m WHERE {scope_where}
        """,
        scope_params,
    ).fetchone()

    if not summary or not summary["messages"]:
        conn.close()
        return {"error": f"{display_name} 在指定范围内没有消息记录"}

    hourly = cur.execute(
        f"""
        SELECT strftime('%H', datetime(m.create_time / {divisor}, 'unixepoch', 'localtime')) AS h,
               COUNT(*) AS n
        FROM messages m WHERE {scope_where} GROUP BY h ORDER BY h
        """,
        scope_params,
    ).fetchall()
    weekday = cur.execute(
        f"""
        SELECT strftime('%w', datetime(m.create_time / {divisor}, 'unixepoch', 'localtime')) AS w,
               COUNT(*) AS n
        FROM messages m WHERE {scope_where} GROUP BY w ORDER BY w
        """,
        scope_params,
    ).fetchall()
    types = cur.execute(
        f"""
        SELECT m.local_type AS t, COUNT(*) AS n
        FROM messages m WHERE {scope_where} GROUP BY m.local_type ORDER BY n DESC
        """,
        scope_params,
    ).fetchall()

    # 该成员引用过谁
    own_quotes = cur.execute(
        f"SELECT m.content AS content FROM messages m WHERE {scope_where} AND m.content LIKE '[引用%'",
        scope_params,
    ).fetchall()
    quoted_targets: Counter = Counter()
    for item in own_quotes:
        match = _QUOTE_PREFIX_RE.match(item["content"] or "")
        if match:
            quoted_targets[match.group(1).strip()] += 1

    # 谁引用过该成员（仅能按显示名匹配，同名用户会合并）
    all_quotes = cur.execute(
        f"""
        SELECT COALESCE(NULLIF(c.display_name, ''), m.sender_username) AS name,
               m.content AS content
        FROM messages m
        LEFT JOIN contacts c ON c.username = m.sender_username
        WHERE {where_sql} AND {_SELF_SENDER_FILTER} AND m.content LIKE '[引用%'
        """,
        params,
    ).fetchall()
    quoted_by: Counter = Counter()
    for item in all_quotes:
        match = _QUOTE_PREFIX_RE.match(item["content"] or "")
        if match and match.group(1).strip() == display_name:
            quoted_by[item["name"]] += 1

    # 连发次数：与上一条自己消息间隔不超过 60 秒
    burst_row = cur.execute(
        f"""
        WITH seq AS (
            SELECT m.create_time,
                   m.create_time - LAG(m.create_time) OVER (ORDER BY m.create_time) AS gap
            FROM messages m WHERE {scope_where}
        )
        SELECT COUNT(*) AS n FROM seq WHERE gap IS NOT NULL AND gap <= ?
        """,
        scope_params + [60 * divisor],
    ).fetchone()
    total_in_scope = cur.execute(
        f"SELECT COUNT(*) FROM messages m WHERE {where_sql}", params
    ).fetchone()[0]
    # 原文样本（可选）：让 Agent 一次拿到「画像 + 该成员近期发言」，不必再翻整个群
    sample_limit = _clamp(include_samples, 0, 200)
    samples = []
    if sample_limit:
        sample_rows = cur.execute(
            f"""
            SELECT m.create_time AS t, m.content AS content, m.local_type AS lt
            FROM messages m WHERE {scope_where}
            ORDER BY m.create_time DESC LIMIT ?
            """,
            scope_params + [sample_limit],
        ).fetchall()
        samples = [
            {
                "time": _ts_to_str(r["t"]),
                "content": r["content"],
                "local_type": r["lt"],
                "label": type_label(r["lt"]),
            }
            for r in sample_rows
        ]
    conn.close()

    messages = int(summary["messages"] or 0)
    chars = int(summary["chars"] or 0)
    return {
        "member": {"username": username, "display_name": display_name},
        "scope_session": session_id or "(全部会话)",
        "time_range": {"start": start, "end": end} if (start and end) else None,
        "summary": {
            "messages": messages,
            "chars": chars,
            "avg_length": round(chars / messages, 1) if messages else 0.0,
            "longest": int(summary["longest"] or 0),
            "first_time": _ts_to_str(summary["first_time"]),
            "last_time": _ts_to_str(summary["last_time"]),
            "active_days": int(summary["active_days"] or 0),
            "share_of_scope": round(messages / total_in_scope, 4) if total_in_scope else 0.0,
        },
        "media": {
            "images": int(summary["images"] or 0),
            "files": int(summary["files"] or 0),
            "videos": int(summary["videos"] or 0),
            "emojis": int(summary["emojis"] or 0),
            "quotes_given": int(summary["quotes_given"] or 0),
        },
        "rhythm": {
            "night_messages": int(summary["night"] or 0),
            "early_messages": int(summary["early"] or 0),
            "recall_like_events": int(summary["recall_like_events"] or 0),
            "bursts": int(burst_row["n"] or 0) if burst_row else 0,
        },
        "hourly_distribution": {r["h"]: r["n"] for r in hourly},
        "weekday_distribution": {r["w"]: r["n"] for r in weekday},
        "type_distribution": [
            {"local_type": r["t"], "label": type_label(r["t"]), "count": r["n"]}
            for r in types
        ],
        "top_quoted": [
            {"name": name, "count": count} for name, count in quoted_targets.most_common(top_n)
        ],
        "quoted_by": [
            {"name": name, "count": count} for name, count in quoted_by.most_common(top_n)
        ],
        "samples": samples,
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
    # 恢复持久化的定时同步（未开启则什么都不做）
    try:
        restore_on_startup()
    except Exception as exc:  # noqa: BLE001 - 恢复失败不应阻止服务启动
        logger.warning(f"恢复定时同步失败：{exc}")
    mcp.run()

if __name__ == "__main__":
    main()
