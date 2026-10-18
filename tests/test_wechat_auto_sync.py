"""定时同步调度器的单元测试。

全部用例都 mock 掉真实解密与 ETL：测试不得触发对微信数据目录的读写。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_mcp import auto_sync


class TestIntervalClamp(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(auto_sync._clamp_interval(1), auto_sync.MIN_INTERVAL_MINUTES)
        self.assertEqual(auto_sync._clamp_interval(99999), auto_sync.MAX_INTERVAL_MINUTES)
        self.assertEqual(auto_sync._clamp_interval(45), 45)
        self.assertEqual(auto_sync._clamp_interval("bad"), auto_sync.DEFAULT_INTERVAL_MINUTES)
        self.assertEqual(auto_sync._clamp_interval(None), auto_sync.DEFAULT_INTERVAL_MINUTES)


class TestRefreshDecrypted(unittest.TestCase):
    def test_missing_keys_file(self):
        with tempfile.TemporaryDirectory() as d:
            res = auto_sync.refresh_decrypted(keys_file=Path(d) / "nope.json")
        self.assertFalse(res["ok"])
        self.assertIn("密钥文件不存在", res["error"])

    def test_bad_db_dir(self):
        with tempfile.TemporaryDirectory() as d:
            keys = Path(d) / "all_keys.json"
            keys.write_text("{}", encoding="utf-8")
            res = auto_sync.refresh_decrypted(db_dir=str(Path(d) / "nope"), keys_file=keys)
        self.assertFalse(res["ok"])

    def test_uses_subprocess_and_stays_quiet(self):
        """刷新必须走子进程并捕获输出 —— 直接进程内调用会把进度打到 stdout，
        MCP 走 stdio 传 JSON-RPC，任何 stdout 输出都会破坏协议。"""
        calls = []

        class FakeProc:
            returncode = 0
            stdout = "结果: 20 成功, 0 失败\n"
            stderr = ""

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return FakeProc()

        with tempfile.TemporaryDirectory() as d:
            keys = Path(d) / "all_keys.json"
            keys.write_text("{}", encoding="utf-8")
            dbdir = Path(d) / "db_storage"
            dbdir.mkdir()
            with patch.object(auto_sync.subprocess, "run", side_effect=fake_run), \
                 patch.object(auto_sync, "get_decrypted_dir", return_value=Path(d) / "decrypted"):
                res = auto_sync.refresh_decrypted(db_dir=str(dbdir), keys_file=keys)

        self.assertTrue(res["ok"])
        self.assertEqual(len(calls), 1)
        cmd, kwargs = calls[0]
        self.assertIn("key_tool", " ".join(cmd))
        self.assertIn("decrypt", cmd)
        self.assertTrue(kwargs.get("capture_output"))


class TestAutoSyncState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / "auto_sync.json"
        self._patch = patch.object(auto_sync, "STATE_FILE", self.state)
        self._patch.start()
        auto_sync._stop_worker()

    def tearDown(self):
        auto_sync._stop_worker()
        self._patch.stop()
        self._tmp.cleanup()

    def test_default_status_disabled(self):
        st = auto_sync.get_auto_sync_status()
        self.assertFalse(st["enabled"])
        self.assertFalse(st["running"])
        self.assertEqual(st["interval_minutes"], auto_sync.DEFAULT_INTERVAL_MINUTES)

    def test_enable_then_disable(self):
        st = auto_sync.set_auto_sync(True, 15)
        self.assertTrue(st["enabled"])
        self.assertTrue(st["running"])
        self.assertEqual(st["interval_minutes"], 15)
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["interval_minutes"], 15)

        st2 = auto_sync.set_auto_sync(False)
        self.assertFalse(st2["enabled"])
        self.assertFalse(st2["running"])

    def test_restore_on_startup(self):
        self.state.write_text(json.dumps({"enabled": True, "interval_minutes": 7}), encoding="utf-8")
        auto_sync.restore_on_startup()
        st = auto_sync.get_auto_sync_status()
        self.assertTrue(st["running"])
        self.assertEqual(st["interval_minutes"], 7)

    def test_restore_skips_when_disabled(self):
        self.state.write_text(json.dumps({"enabled": False}), encoding="utf-8")
        auto_sync.restore_on_startup()
        self.assertFalse(auto_sync.get_auto_sync_status()["running"])

    def test_sync_now_skips_etl_when_decrypt_fails(self):
        """解密失败就不该重建索引：在旧快照上重建只会「看起来成功」但内容没变"""
        with patch.object(auto_sync, "refresh_decrypted", return_value={"ok": False, "error": "x"}), \
             patch.object(auto_sync, "sync_data") as mock_etl:
            res = auto_sync.sync_now()
        self.assertFalse(res["ok"])
        mock_etl.assert_not_called()
        self.assertEqual(res["etl_step"]["status"], "skipped")

    def test_sync_now_runs_etl_when_decrypt_ok(self):
        with patch.object(auto_sync, "refresh_decrypted", return_value={"ok": True}), \
             patch.object(auto_sync, "sync_data", return_value={"status": "success"}) as mock_etl:
            res = auto_sync.sync_now(force=True)
        self.assertTrue(res["ok"])
        mock_etl.assert_called_once_with(force=True)
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["run_count"], 1)
        self.assertIn("last_run", saved)


if __name__ == "__main__":
    unittest.main()
