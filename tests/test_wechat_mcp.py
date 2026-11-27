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

    def test_system_sysmsgtemplate_renders_names(self):
        # 入群提示：可读文案在 <template>，人名在 <link_list> 里按占位符替换；
        # 旧实现只找 <content>/<plain>，会把整段 XML 当成正文
        xml = (
            'wxid_group@chatroom:\n<sysmsg type="sysmsgtemplate"><sysmsgtemplate>'
            '<content_template type="tmpl_type_profile"><plain><![CDATA[]]></plain>'
            '<template><![CDATA["$username$"邀请"$names$"加入了群聊]]></template>'
            '<link_list>'
            '<link name="username" type="link_profile"><memberlist><member>'
            "<username>wxid_a</username><nickname>ori</nickname></member></memberlist></link>"
            '<link name="names" type="link_profile"><memberlist><member>'
            "<username>wxid_b</username><nickname>李泽新</nickname></member></memberlist>"
            "<separator>、</separator></link>"
            "</link_list></content_template></sysmsgtemplate></sysmsg>"
        )
        res = build_content(10000, None, xml)
        self.assertEqual(res, '"ori"邀请"李泽新"加入了群聊')
        self.assertNotIn("<", res)
        self.assertNotIn("@chatroom", res)

    def test_system_html_markup_stripped(self):
        # 红包 / 群公告里夹 <img>/<a> 等排版标签，不能原样输出
        xml = '<img src="SystemMessages_HongbaoIcon.png"/>肖丕军领取了你的红包'
        self.assertEqual(build_content(10000, xml, None), "肖丕军领取了你的红包")

    def test_chat_record_inlined(self):
        # 合并转发的聊天记录：直接把子消息渲染进正文，而不是只留 [聊天记录] 标题
        local_type = (19 << 32) | 49
        xml = (
            "<msg><appmsg><title>群聊的聊天记录</title><type>19</type><recorditem><![CDATA["
            '<recordinfo><title>群聊的聊天记录</title><datalist count="2">'
            '<dataitem datatype="1" dataid="x1"><datadesc>hi</datadesc>'
            "<sourcename>A</sourcename></dataitem>"
            '<dataitem datatype="8" dataid="x2"><datadesc></datadesc>'
            "<datatitle>报告.pdf</datatitle><sourcename>B</sourcename></dataitem>"
            "</datalist></recordinfo>]]></recorditem></appmsg></msg>"
        )
        res = build_content(local_type, None, xml)
        self.assertIn("[聊天记录] 群聊的聊天记录（共 2 条）", res)
        self.assertIn("A: hi", res)
        self.assertIn("B: [文件] 报告.pdf", res)


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
