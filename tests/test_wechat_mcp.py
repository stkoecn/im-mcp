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
    parse_file_media,
)
from wechat_mcp.database import init_schema
from wechat_mcp.server import (
    _ts_to_str,
    _escape_like,
    _dedupe_phrases,
    _check_db_initialized,
)


class TestWeChatParser(unittest.TestCase):
    def test_etl_sources_tracks_progress(self):
        # 增量同步靠 (库文件, 表) 记录源指纹与最大 local_id；进度不能回退
        import sqlite3

        from wechat_mcp.adapters.v4 import (
            _ensure_etl_sources,
            _etl_source_state,
            _save_etl_source,
        )

        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        _ensure_etl_sources(cur)
        self.assertIsNone(_etl_source_state(cur, "message_0.db", "Msg_abc"))

        _save_etl_source(cur, "message_0.db", "Msg_abc", 100, 200, 5)
        self.assertEqual(tuple(_etl_source_state(cur, "message_0.db", "Msg_abc")), (100, 200, 5))

        # 指纹更新但 local_id 变小（例如换库/回滚）时，进度保持较大值
        _save_etl_source(cur, "message_0.db", "Msg_abc", 120, 300, 3)
        self.assertEqual(tuple(_etl_source_state(cur, "message_0.db", "Msg_abc")), (120, 300, 5))

        # 不同表互不影响
        _save_etl_source(cur, "message_0.db", "Msg_def", 100, 200, 99)
        self.assertEqual(_etl_source_state(cur, "message_0.db", "Msg_def")[2], 99)

    def test_parse_file_media(self):
        # [文件] 消息里的 <md5> 是关联 media_files 的唯一可靠键
        xml = (
            "<msg><appmsg><title>报告.pdf</title><type>6</type>"
            "<md5>ABC123DEF</md5><totallen>2048</totallen></appmsg></msg>"
        )
        meta = parse_file_media(xml)
        self.assertEqual(meta["md5"], "abc123def")
        self.assertEqual(meta["name"], "报告.pdf")
        self.assertEqual(meta["size"], 2048)
        self.assertIsNone(parse_file_media("<msg><appmsg><type>5</type></appmsg></msg>"))

    def test_media_payload(self):
        from wechat_mcp.server import _media_payload

        self.assertIsNone(_media_payload({}, None))
        # 索引里有 md5 但文件已被清理 → exists=False，结构仍然完整
        payload = _media_payload({"m1": {"path": r"C:\no\such\file.bin", "size": 5}}, "m1")
        self.assertEqual(payload["md5"], "m1")
        self.assertEqual(payload["size"], 5)
        self.assertFalse(payload["exists"])
        # md5 不在索引里（从未下载）也不抛异常
        self.assertEqual(_media_payload({}, "zzz")["path"], None)

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


if __name__ == "__main__":
    unittest.main()
