import os
import sys
import tempfile
import unittest
import sqlite3
from pathlib import Path

# 将 src 加入路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wechat_mcp.parser import (
    build_content,
    extract_title_from_xml,
    maybe_zstd_decompress,
)
from wechat_mcp.database import init_schema
from wechat_mcp.server import (
    _ts_to_str,
    _escape_like,
    _dedupe_phrases,
    _check_db_initialized,
)


class TestWeChatParser(unittest.TestCase):
    def test_text_message_strip_wxid_prefix(self):
        # 测试群聊中多余的 wxid_xxx: 前缀被剥离
        raw = "wxid_abcdef123456:\n你好，今天开会吗？"
        res = build_content(1, raw, None)
        self.assertEqual(res, "你好，今天开会吗？")

    def test_text_plain_content(self):
        res = build_content(1, "普通文字消息", None)
        self.assertEqual(res, "普通文字消息")

    def test_quote_message_parsing(self):
        # 模拟 appmsg type=57 引用消息 XML (local_type 高32位编码 app_type 57: 57 << 32 = 244824367104)
        local_type = (57 << 32) | 49
        xml = """
        <appmsg appid="" sdkver="0">
            <title>我也赞成这个方案</title>
            <type>57</type>
            <refermsg>
                <type>1</type>
                <fromusr>wxid_other</fromusr>
                <displayname>张三</displayname>
                <content>大家觉得下周上线合适吗？</content>
            </refermsg>
        </appmsg>
        """
        res = build_content(local_type, None, xml)
        self.assertIn("[引用 张三: 大家觉得下周上线合适吗？]", res)
        self.assertIn("我也赞成这个方案", res)

    def test_system_message_parsing(self):
        # type=10000 系统消息不应被当作 [多媒体消息]
        res = build_content(10000, "你已添加了张三，现在可以开始聊天了。", None)
        self.assertEqual(res, "你已添加了张三，现在可以开始聊天了。")

    def test_system_xml_parsing(self):
        xml = '<sysmsg type="revokemsg"><revokemsg><content>对方撤回了一条消息</content></revokemsg></sysmsg>'
        res = build_content(10000, None, xml)
        self.assertEqual(res, "对方撤回了一条消息")


class TestUtilities(unittest.TestCase):
    def test_ts_to_str(self):
        # 秒级时间戳: 1600000000 -> 2020-09-13
        s = _ts_to_str(1600000000)
        self.assertTrue(s.startswith("2020-09-13"))

        # 毫秒级时间戳: 1600000000000 -> 同样解析
        ms_s = _ts_to_str(1600000000000)
        self.assertEqual(s, ms_s)

    def test_escape_like(self):
        self.assertEqual(_escape_like("100%"), "100\\%")
        self.assertEqual(_escape_like("user_name"), "user\\_name")
        self.assertEqual(_escape_like("a\\b"), "a\\\\b")

    def test_dedupe_phrases(self):
        from collections import Counter
        c = Counter({
            "猫老板": 10,
            "猫老": 10,  # 碎片，应被覆盖并剔除
            "老板": 10,  # 碎片，应被覆盖并剔除
            "独立词": 5,
        })
        res = _dedupe_phrases(c, min_count=2, scan=10, limit=5)
        phrases = [item["phrase"] for item in res]
        self.assertIn("猫老板", phrases)
        self.assertIn("独立词", phrases)
        self.assertNotIn("猫老", phrases)
        self.assertNotIn("老板", phrases)


class TestDatabaseSchema(unittest.TestCase):
    def test_schema_creation_in_memory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = Path(tmpdir) / "test.db"
            import wechat_mcp.config as conf
            orig_db_path = conf.DB_PATH
            conf.DB_PATH = test_db
            try:
                init_schema()
                self.assertTrue(test_db.exists())
                conn = sqlite3.connect(test_db)
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                expected = {"contacts", "sessions", "messages", "messages_fts", "media_files", "favorites", "emoticons"}
                for t in expected:
                    self.assertIn(t, tables)
                conn.close()
            finally:
                conf.DB_PATH = orig_db_path


class TestRecallAttribution(unittest.TestCase):
    """撤回次数必须算在「被撤回人」头上，而不是「发过含撤回字样消息的人」。

    撤回提示的 sender_username 是群自身（session_username）：旧口径按发言人做
    `content LIKE '%撤回%'` 统计，结果是真正的撤回一条都归不到人，反而把
    「聊天里提到撤回」的成员算成撤回冠军（阈值 >0，会在榜单上永久霸榜）。
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE messages (
                sender_username TEXT,
                session_username TEXT,
                content TEXT,
                local_type INTEGER,
                create_time INTEGER
            )
            """
        )
        self.session = "123@chatroom"
        self.conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?)",
            [
                # 真正的撤回提示：发送者是群自身
                (self.session, self.session, '"张三" 撤回了一条消息', 10000, 1),
                (self.session, self.session, '"张三" 撤回了一条消息', 10000, 2),
                (self.session, self.session, '"李四" 撤回了一条消息', 10002, 3),
                (self.session, self.session, "你撤回了一条消息", 10000, 4),
                # 干扰项：真人聊天里正常提到「撤回」
                ("wxid_a", self.session, "他撤回了对 bukkit 的代码授权", 1, 5),
                ("wxid_a", self.session, "[引用 张三: 撤回了] 对啊", 244813135921, 6),
            ],
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_counts_go_to_revoked_member_not_mentioner(self):
        from wechat_mcp.server import _recall_counts

        counts = _recall_counts(
            self.conn.cursor(), "m.session_username = ?", [self.session]
        )
        self.assertEqual(counts["张三"], 2)
        self.assertEqual(counts["李四"], 1)
        # 「你撤回了一条消息」拿不到昵称，保守跳过
        self.assertNotIn("你", counts)
        # 正常聊天里提到「撤回」不计入
        self.assertEqual(sum(counts.values()), 3)

    def test_member_payload_injects_recall_counts(self):
        from collections import Counter

        from wechat_mcp.server import _member_payload

        rows = [
            {"u": "wxid_zhang", "name": "张三", "messages": 10},
            {"u": "wxid_li", "name": "李四", "messages": 5},
        ]
        payload, leaders = _member_payload(
            rows, 10, quoted=Counter(), bursts={}, recalls=Counter({"张三": 2})
        )
        by_name = {m["name"]: m for m in payload}
        self.assertEqual(by_name["张三"]["recall_like_events"], 2)
        self.assertEqual(by_name["李四"]["recall_like_events"], 0)
        self.assertEqual(leaders["recall_like_events"][0]["display_name"], "张三")


if __name__ == "__main__":
    unittest.main()
