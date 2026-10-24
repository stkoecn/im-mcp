"""Date/range query regressions using only synthetic, temporary SQLite data."""

import datetime
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wechat_mcp import config, database, server


class TestWeChatTimeQueries(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        db_path = Path(temp_dir.name) / "synthetic.db"

        # These modules import DB_PATH by value; patch every lookup so neither
        # schema creation nor the initialization guard can reach a user's DB.
        for module in (config, database, server):
            patcher = patch.object(module, "DB_PATH", db_path)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(database.release_connections)
        database.init_schema()
        database.release_connections()
        self.db_path = db_path

    def seed_messages(self, unit):
        # Match the tools' local-time interpretation without assuming a host TZ.
        start = int(datetime.datetime(2024, 5, 20).timestamp()) * unit
        next_day = int(datetime.datetime(2024, 5, 21).timestamp()) * unit
        after_range = int(datetime.datetime(2024, 5, 22).timestamp()) * unit
        noon = int(datetime.datetime(2024, 5, 20, 12).timestamp()) * unit
        rows = [
            ("before-start", "room-a", start - 1),
            ("at-start", "room-a", start),
            ("before-noon", "room-a", noon - 1),
            ("at-noon-a", "room-a", noon),
            ("at-noon-b", "room-a", noon),
            ("other-session", "room-b", noon),
            ("before-midnight", "room-a", next_day - 1),
            ("next-midnight", "room-a", next_day),
            ("last-in-range", "room-a", after_range - 1),
            ("after-range", "room-a", after_range),
        ]
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM contacts")
            conn.executemany(
                "INSERT INTO sessions (username, display_name) VALUES (?, ?)",
                [("room-a", "Synthetic Room A"), ("room-b", "Synthetic Room B")],
            )
            conn.execute(
                "INSERT INTO contacts (username, display_name) VALUES (?, ?)",
                ("sender-a", "Synthetic Sender"),
            )
            # Keep insertion order independent of chronological order.
            conn.executemany(
                "INSERT INTO messages "
                "(source_file, local_id, session_username, sender_username, "
                "create_time, local_type, content) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("synthetic", i, session, "sender-a", ts, 1, content)
                    for i, (content, session, ts) in enumerate(reversed(rows))
                ],
            )

    def assert_messages(self, results, expected):
        for row in results:
            self.assertNotIn("error", row)
        self.assertCountEqual([row["content"] for row in results], expected)
        times = [row["time"] for row in results]
        self.assertEqual(times, sorted(times))
        for row in results:
            self.assertEqual(row["sender_name"], "Synthetic Sender")

    def test_date_includes_start_and_excludes_next_midnight(self):
        for unit in (1, 1000):
            with self.subTest(unit=unit):
                self.seed_messages(unit)
                results = server.get_messages_by_date("2024-05-20")
                self.assert_messages(results, [
                    "at-start", "before-noon", "at-noon-a", "at-noon-b",
                    "other-session", "before-midnight",
                ])
                self.assertEqual(results[0]["time"], "2024-05-20 00:00:00")
                self.assertEqual(results[-1]["time"], "2024-05-20 23:59:59")

    def test_range_date_end_includes_the_entire_final_day(self):
        for unit in (1, 1000):
            with self.subTest(unit=unit):
                self.seed_messages(unit)
                results = server.get_messages_by_range("2024-05-20", "2024-05-21")
                self.assert_messages(results, [
                    "at-start", "before-noon", "at-noon-a", "at-noon-b",
                    "other-session", "before-midnight", "next-midnight",
                    "last-in-range",
                ])
                self.assertEqual(results[-1]["time"], "2024-05-21 23:59:59")

    def test_range_exact_time_bounds_are_half_open(self):
        for unit in (1, 1000):
            for end in ("2024-05-20 12:00", "2024-05-20 12:00:00"):
                with self.subTest(unit=unit, end=end):
                    self.seed_messages(unit)
                    results = server.get_messages_by_range("2024-05-20", end)
                    self.assert_messages(results, ["at-start", "before-noon"])
                    results = server.get_messages_by_range(end, "2024-05-21 00:00:00")
                    self.assert_messages(results, [
                        "at-noon-a", "at-noon-b", "other-session", "before-midnight",
                    ])

    def test_date_and_range_pages_cover_filtered_messages_once(self):
        queries = (
            (server.get_messages_by_date, ("2024-05-20",)),
            (server.get_messages_by_range, ("2024-05-20", "2024-05-20")),
        )
        expected = ["at-start", "before-noon", "at-noon-a", "at-noon-b", "before-midnight"]
        for unit in (1, 1000):
            self.seed_messages(unit)
            for query, args in queries:
                for page_size in (1, 2, 3):
                    with self.subTest(unit=unit, query=query.__name__, page_size=page_size):
                        pages = [
                            query(*args, session_id="room-a", limit=page_size, offset=offset)
                            for offset in range(0, len(expected), page_size)
                        ]
                        self.assertEqual(
                            [len(page) for page in pages],
                            [min(page_size, len(expected) - offset)
                             for offset in range(0, len(expected), page_size)],
                        )
                        results = [row for page in pages for row in page]
                        self.assert_messages(results, expected)
                        self.assertTrue(all(row["session_name"] == "Synthetic Room A" for row in results))
                        self.assertEqual(
                            query(*args, session_id="room-a", limit=page_size, offset=len(expected)),
                            [],
                        )

    def test_queries_return_empty_for_missing_date_or_session(self):
        for unit in (1, 1000):
            with self.subTest(unit=unit):
                self.seed_messages(unit)
                self.assertEqual(server.get_messages_by_date("2024-05-23"), [])
                self.assertEqual(server.get_messages_by_range("2024-05-23", "2024-05-24"), [])
                self.assertEqual(server.get_messages_by_date("2024-05-20", session_id="missing"), [])
                self.assertEqual(
                    server.get_messages_by_range("2024-05-20", "2024-05-21", session_id="missing"),
                    [],
                )

    def test_queries_return_empty_for_initialized_database_without_messages(self):
        self.assertEqual(server.get_messages_by_date("2024-05-20"), [])
        self.assertEqual(server.get_messages_by_range("2024-05-20", "2024-05-21"), [])

    def test_messages_since_time_only_does_not_skip_same_second(self):
        """回归：只用 after_time 续拉时，同一秒超过一页的消息不能被永久跳过。"""
        for unit in (1, 1000):
            with self.subTest(unit=unit):
                self.seed_messages(unit)
                first = server.get_messages_since(
                    after_time="2024-05-20 12:00:00", session_id="room-a", limit=1
                )
                self.assertGreaterEqual(first["count"], 1)
                second = server.get_messages_since(
                    after_time=first["next_time"], session_id="room-a", limit=10
                )
                contents = {row["content"] for row in first["messages"] + second["messages"]}
                # 允许重复（时间游标是含语义），但绝不能丢。
                self.assertTrue({"at-noon-a", "at-noon-b"}.issubset(contents))

    def test_messages_since_composite_cursor_is_exact(self):
        """回归：after_time + after_id 组成 (create_time, id) 复合游标，不漏也不重。"""
        for unit in (1, 1000):
            with self.subTest(unit=unit):
                self.seed_messages(unit)
                first = server.get_messages_since(
                    after_time="2024-05-20 12:00:00", session_id="room-a", limit=1
                )
                second = server.get_messages_since(
                    after_time=first["next_time"],
                    after_id=first["next_cursor"],
                    session_id="room-a",
                    limit=10,
                )
                first_ids = {row["id"] for row in first["messages"]}
                second_ids = {row["id"] for row in second["messages"]}
                self.assertFalse(first_ids & second_ids)
                contents = {row["content"] for row in first["messages"] + second["messages"]}
                self.assertTrue({"at-noon-a", "at-noon-b"}.issubset(contents))


if __name__ == "__main__":
    unittest.main()
