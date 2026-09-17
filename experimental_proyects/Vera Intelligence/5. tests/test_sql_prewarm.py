from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "4. scripts"))
import vi_agent


class SqlPrewarmTests(unittest.TestCase):
    def setUp(self):
        for name in ("_cached_sql_connection", "_sql_warm_future"):
            p = patch.object(vi_agent, name, None)
            p.start()
            self.addCleanup(p.stop)

    def test_background_open_deduplicates_and_first_query_reuses_it(self):
        started = threading.Event()
        release = threading.Event()
        connection = MagicMock(closed=False)

        def connect(**kwargs):
            started.set()
            if not release.wait(2):
                raise RuntimeError("test deadline")
            return connection

        with patch.object(vi_agent, "postgres_connection_kwargs", return_value={}), patch.object(
            vi_agent.psycopg, "connect", side_effect=connect
        ) as open_connection:
            future = vi_agent.prewarm_sql_connection()
            try:
                self.assertTrue(started.wait(2))
                self.assertFalse(future.done())
                self.assertIs(vi_agent.prewarm_sql_connection(), future)
            finally:
                release.set()
                result = future.result(timeout=2)
            self.assertTrue(result)
            with patch.object(vi_agent, "_validate_sql"), patch.object(
                vi_agent, "_fetch_readonly_rows", return_value={"rows": []}
            ) as fetch:
                vi_agent.run_readonly_sql("SELECT ejemplo")
            fetch.assert_called_once_with(connection, "SELECT ejemplo")
            open_connection.assert_called_once_with(autocommit=True)

    def test_failure_is_nonfatal_and_later_warm_can_retry(self):
        with patch.object(vi_agent, "_get_reusable_sql_connection", side_effect=[RuntimeError("private"), MagicMock()]) as get:
            self.assertFalse(vi_agent.prewarm_sql_connection().result(timeout=2))
            self.assertTrue(vi_agent.prewarm_sql_connection().result(timeout=2))
            self.assertEqual(get.call_count, 2)

    def test_ready_connection_does_not_open_again(self):
        vi_agent._cached_sql_connection = MagicMock(closed=False)
        with patch.object(vi_agent, "_get_reusable_sql_connection") as get:
            self.assertTrue(vi_agent.prewarm_sql_connection().result(timeout=0))
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
