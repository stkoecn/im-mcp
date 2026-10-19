"""定时同步调度器的单元测试。

全部用例都 mock 掉真实解密与 ETL：测试不得触发对微信数据目录的读写。
"""

import json
import os
import tempfile
import time
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

    def test_db_dir_from_keys(self):
        """密钥文件里的 _db_dir 只在目录真实存在时才算可用。"""
        with tempfile.TemporaryDirectory() as d:
            keys = Path(d) / "all_keys.json"
            dbdir = Path(d) / "db_storage"
            dbdir.mkdir()
            keys.write_text(json.dumps({"_db_dir": str(dbdir)}), encoding="utf-8")
            self.assertEqual(auto_sync._db_dir_from_keys(keys), str(dbdir))

            keys.write_text(json.dumps({"_db_dir": str(Path(d) / "nope")}), encoding="utf-8")
            self.assertIsNone(auto_sync._db_dir_from_keys(keys))
            keys.write_text("{}", encoding="utf-8")
            self.assertIsNone(auto_sync._db_dir_from_keys(keys))
            self.assertIsNone(auto_sync._db_dir_from_keys(Path(d) / "missing.json"))

    def test_falls_back_to_db_dir_recorded_in_keys(self):
        """自动探测失败时必须回退到密钥文件记录的 _db_dir。

        真实故障：wechat-mcp 由 Snow App 启动，其环境与终端不同，
        auto_detect_db_dir() 返回 None → 同步永远以「未找到 db_storage」失败，
        而 all_keys.json 里一直记着当初成功解密用过的目录。
        """
        calls = []

        class FakeProc:
            returncode = 0
            stdout = "ok\n"
            stderr = ""

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return FakeProc()

        with tempfile.TemporaryDirectory() as d:
            dbdir = Path(d) / "db_storage"
            dbdir.mkdir()
            keys = Path(d) / "all_keys.json"
            keys.write_text(json.dumps({"_db_dir": str(dbdir)}), encoding="utf-8")
            with patch("wechat_mcp.tools.key_tool.auto_detect_db_dir", return_value=None), \
                 patch.object(auto_sync.subprocess, "run", side_effect=fake_run), \
                 patch.object(auto_sync, "get_decrypted_dir", return_value=Path(d) / "decrypted"):
                res = auto_sync.refresh_decrypted(keys_file=keys)
        self.assertTrue(res["ok"])
        self.assertEqual(res["db_dir"], str(dbdir))
        self.assertIn(str(dbdir), calls[0])

    def test_env_var_overrides_detection(self):
        """WECHAT_DB_DIR 显式指定时优先于自动探测（排障出路）。"""
        calls = []

        class FakeProc:
            returncode = 0
            stdout = "ok\n"
            stderr = ""

        with tempfile.TemporaryDirectory() as d:
            dbdir = Path(d) / "db_storage"
            dbdir.mkdir()
            keys = Path(d) / "all_keys.json"
            keys.write_text("{}", encoding="utf-8")
            with patch.dict(auto_sync.os.environ, {"WECHAT_DB_DIR": str(dbdir)}), \
                 patch("wechat_mcp.tools.key_tool.auto_detect_db_dir",
                       side_effect=AssertionError("有环境变量时不应再探测")), \
                 patch.object(auto_sync.subprocess, "run",
                              side_effect=lambda cmd, **kw: calls.append(cmd) or FakeProc()), \
                 patch.object(auto_sync, "get_decrypted_dir", return_value=Path(d) / "decrypted"):
                res = auto_sync.refresh_decrypted(keys_file=keys)
        self.assertTrue(res["ok"])
        self.assertEqual(res["db_dir"], str(dbdir))
        self.assertIn(str(dbdir), calls[0])

    def test_all_fallbacks_fail_gives_actionable_error(self):
        """探测与密钥记录都不可用时，错误信息必须给出可操作出路。"""
        with tempfile.TemporaryDirectory() as d:
            keys = Path(d) / "all_keys.json"
            keys.write_text("{}", encoding="utf-8")
            with patch.dict(auto_sync.os.environ, {"WECHAT_DB_DIR": ""}), \
                 patch("wechat_mcp.tools.key_tool.auto_detect_db_dir", return_value=None):
                res = auto_sync.refresh_decrypted(keys_file=keys)
        self.assertFalse(res["ok"])
        self.assertIn("WECHAT_DB_DIR", res["error"])

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


class _FakeProc:
    """冒充 spawn 出来的独立同步进程。"""

    pid = 424242

    def poll(self):
        return None


class TestManualSyncWorker(unittest.TestCase):
    """手动同步（start_sync / _sync_worker / 独立进程心跳）的关键不变量。

    真实故障（2026-10-19 实测）：面板点「同步微信数据」永远停在「同步中」。两个根因 ——
    (1) 后台线程抛异常时只 logger.warning 不写 state，调用方按 run_count 增长判定完成的
    条件永远不成立；(2) 宿主（Snow App）对 stdio MCP 是**短连接**（每次工具调用 spawn
    进程、寿命 < 1 秒），进程内的后台线程会随进程被杀，而一次完整同步实测 53 秒 ——
    永远跑不完。因此 start_sync 必须把同步交给**脱离进程树**的独立进程，并用文件心跳
    报告进度；进程内线程只作为 spawn 受限环境的兜底。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / "auto_sync.json"
        self.sync_state = Path(self._tmp.name) / "sync_state.json"
        self._patch = patch.object(auto_sync, "STATE_FILE", self.state)
        self._patch.start()
        self._patch_sync = patch.object(auto_sync, "SYNC_STATE_FILE", self.sync_state)
        self._patch_sync.start()

    def tearDown(self):
        self._patch.stop()
        self._patch_sync.stop()
        self._tmp.cleanup()
        with auto_sync._lock:
            auto_sync._sync_thread = None
            auto_sync._sync_started_at = None

    def _write_sync_state(self, **overrides):
        payload = {
            "mode": "detached",
            "pid": os.getpid(),  # 用当前进程冒充「还活着」
            "started_at": time.time() - 10,
            "heartbeat_at": time.time(),
        }
        payload.update(overrides)
        self.sync_state.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    # ---- 进程内线程兜底路径 ----

    def test_worker_records_failure_when_sync_raises(self):
        """异常必须落盘：否则调用方永远等不到「跑完」的信号。"""
        with patch.object(auto_sync, "sync_now", side_effect=RuntimeError("解密快照被写坏")):
            auto_sync._sync_worker(False, True)

        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["run_count"], 1)
        self.assertFalse(saved["last_run"]["ok"])
        self.assertIn("解密快照被写坏", saved["last_run"]["result"]["error"])
        # 线程引用与启动时间都要清干净，否则下一次 start_sync 会误判 already_running
        self.assertIsNone(auto_sync._sync_thread)
        self.assertIsNone(auto_sync._sync_started_at)

    # ---- 独立进程路径 ----

    def test_start_sync_spawns_detached_worker(self):
        """默认走独立进程（脱离进程树），并如实报告 mode / pid。"""
        with patch.object(auto_sync, "_spawn_detached_worker", return_value=_FakeProc()), \
             patch.object(auto_sync, "_load_sync_state", return_value={"pid": _FakeProc.pid}), \
             patch.object(auto_sync, "_pid_alive", return_value=True):
            res = auto_sync.start_sync()
        self.assertTrue(res["started"])
        self.assertEqual(res["mode"], "detached")
        self.assertEqual(res["pid"], _FakeProc.pid)

    def test_start_sync_reports_failure_when_worker_dies(self):
        """Popen 成功但 worker 未写出心跳 → 必须返回 started=false 并清掉残留。

        真实故障（2026-10-19 实测）：短连接宿主会在 MCP 进程退出时终止 worker，
        状态文件永不更新；旧实现只看到 Popen 成功就返回 started=true，调用方
        只能干等超时。这里固定新的失败语义。
        """
        with patch.object(auto_sync, "_spawn_detached_worker", return_value=_FakeProc()), \
             patch.object(auto_sync, "_load_sync_state", return_value=None), \
             patch.object(auto_sync, "WORKER_STARTUP_GRACE_SECONDS", 0.05):
            res = auto_sync.start_sync()
        self.assertFalse(res["started"])
        self.assertEqual(res["mode"], "detached")
        self.assertIn("未能存活", res["error"])
        self.assertFalse(self.sync_state.exists())

    def test_start_sync_falls_back_to_thread_when_spawn_fails(self):
        """spawn 受限（沙箱 / 受限客户端）时必须回退线程，而不是直接失败。"""
        with patch.object(auto_sync, "_spawn_detached_worker", return_value=None), \
             patch.object(auto_sync, "_sync_worker"):
            res = auto_sync.start_sync()
        self.assertTrue(res["started"])
        self.assertEqual(res["mode"], "thread")
        self.assertIn("短连接", res["note"])

    # ---- 文件心跳判定 ----

    def test_heartbeat_fresh_means_running(self):
        """心跳新鲜且进程存活 → syncing true，并给出模式与已跑时长。"""
        self._write_sync_state()
        st = auto_sync.get_auto_sync_status()
        self.assertTrue(st["syncing"])
        self.assertEqual(st["sync_mode"], "detached")
        self.assertGreaterEqual(st["sync_elapsed_seconds"], 10)

    def test_stale_heartbeat_is_cleared_and_not_running(self):
        """心跳过期（进程可能已被强杀）→ 不算在跑，并清掉残留状态。"""
        self._write_sync_state(
            started_at=time.time() - 600,
            heartbeat_at=time.time() - (auto_sync.SYNC_HEARTBEAT_STALE_SECONDS + 5),
        )
        self.assertFalse(auto_sync.get_auto_sync_status()["syncing"])
        self.assertFalse(self.sync_state.exists())

    def test_dead_pid_is_cleared_and_not_running(self):
        """心跳还新鲜但进程已不存在 → 立即判死（不必等心跳过期）。"""
        self._write_sync_state(pid=999999)  # 基本不可能存在的 pid
        self.assertFalse(auto_sync.get_auto_sync_status()["syncing"])
        self.assertFalse(self.sync_state.exists())

    def test_already_running_uses_file_heartbeat(self):
        """已有独立进程在跑时，start_sync 必须返回 already_running 且不重复拉起。"""
        self._write_sync_state(started_at=time.time() - 5, heartbeat_at=time.time())
        with patch.object(auto_sync, "_spawn_detached_worker") as spawn:
            res = auto_sync.start_sync()
        self.assertFalse(res["started"])
        self.assertTrue(res["already_running"])
        self.assertGreaterEqual(res["elapsed_seconds"], 5)
        spawn.assert_not_called()

    def test_stale_file_heartbeat_allows_restart(self):
        """心跳过期 → 允许重新发起（僵尸自愈），否则一次卡死会永久堵住后续调用。"""
        self._write_sync_state(
            started_at=time.time() - (auto_sync.SYNC_STALE_SECONDS + 10),
            heartbeat_at=time.time() - (auto_sync.SYNC_HEARTBEAT_STALE_SECONDS + 10),
        )
        with patch.object(auto_sync, "_spawn_detached_worker", return_value=_FakeProc()), \
             patch.object(auto_sync, "_load_sync_state", return_value={"pid": _FakeProc.pid}), \
             patch.object(auto_sync, "_pid_alive", return_value=True):
            res = auto_sync.start_sync()
        self.assertTrue(res["started"])
        self.assertNotIn("already_running", res)

    # ---- 独立进程本体 ----

    def test_worker_run_records_result_and_clears_state(self):
        """sync_worker.run 正常路径：写 last_run、run_count+1、清掉心跳、退出码 0。"""
        from wechat_mcp import sync_worker

        with patch.object(auto_sync, "refresh_and_sync", return_value={"ok": True}):
            code = sync_worker.run(force=False, redecrypt=True)

        self.assertEqual(code, 0)
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["run_count"], 1)
        self.assertTrue(saved["last_run"]["ok"])
        self.assertFalse(self.sync_state.exists())

    def test_worker_run_records_exception_and_returns_nonzero(self):
        """sync_worker.run 异常路径：异常也要落盘（调用方靠它判定「本轮结束」）。"""
        from wechat_mcp import sync_worker

        with patch.object(auto_sync, "refresh_and_sync", side_effect=RuntimeError("解密失败")):
            code = sync_worker.run()

        self.assertEqual(code, 1)
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertFalse(saved["last_run"]["ok"])
        self.assertIn("解密失败", saved["last_run"]["result"]["error"])
        self.assertFalse(self.sync_state.exists())


if __name__ == "__main__":
    unittest.main()
