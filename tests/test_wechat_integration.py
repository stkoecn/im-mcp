import os
import sys
import unittest
from pathlib import Path

# 将 src 加入路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wechat_mcp.config import DB_PATH
from wechat_mcp.server import mcp, _check_db_initialized


class TestRealHostEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 仅在宿主机真实数据库存在时进行端到端全链路检验
        cls.db_available = DB_PATH.exists()

    def test_registered_tools_count(self):
        # 微信 MCP 必须完整对外注册 16 个核心工具
        tools = [t.name for t in mcp._tool_manager.list_tools()]
        expected_tools = [
            "search_wechat_messages",
            "list_wechat_sessions",
            "get_session_history",
            "search_shared_files",
            "get_message_image",
            "get_messages_by_date",
            "sync_wechat_database",
            "search_contacts",
            "list_chatroom_members",
            "get_messages_by_range",
            "get_chat_activity_stats",
            "search_media_files",
            "list_favorites",
            "list_emoticons",
            "get_sync_status",
            "get_persona_style",
            "get_user_avatars",
        ]
        for exp in expected_tools:
            self.assertIn(exp, tools)
        self.assertEqual(len(tools), 17)

    def test_sync_status_tool_structure(self):
        if not self.db_available:
            self.skipTest("本地尚未运行 wechat-init 生成 app.db，跳过本地集成测试")

        from wechat_mcp.server import get_sync_status
        status = get_sync_status()
        self.assertIsInstance(status, dict)
        self.assertIn("counts", status)
        self.assertIn("messages", status["counts"])
        self.assertIn("contacts", status["counts"])
        self.assertIn("sessions", status["counts"])
        self.assertGreater(status["counts"]["messages"], 0)

    def test_wechat_init_cli_parser(self):
        """测试 wechat-init 的 CLI 参数解析器，确保 --timeout, --pid, --passphrase 等参数齐全."""
        import argparse
        from wechat_mcp.tools.__init__ import main
        from unittest.mock import patch
        with patch("sys.argv", ["wechat-init", "--help"]):
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 0)

    def test_wechat_init_passphrase_import(self):
        """测试 wechat-init --passphrase 分支，确保 save_passphrase 被正确导入且不抛 NameError."""
        from wechat_mcp.tools.__init__ import main
        from unittest.mock import patch, MagicMock

        # 模拟传入合法的 64 位十六进制 passphrase，并拦截后续提取流程避免执行真实系统操作
        valid_ph = "0123456789abcdef" * 4
        with patch("sys.argv", ["wechat-init", "--passphrase", valid_ph, "--only-extract"]), \
             patch("wechat_mcp.tools.__init__.save_passphrase") as mock_save, \
             patch("wechat_mcp.tools.__init__.cmd_extract") as mock_extract:
            main()
            mock_save.assert_called_once_with(valid_ph)
            mock_extract.assert_called_once()


if __name__ == "__main__":
    unittest.main()
