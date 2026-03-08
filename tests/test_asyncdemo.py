"""
Tests for asyncdemo.py — concurrent async task execution.

asyncdemo.py calls asyncio.run(main()) at module level, so we extract
functions by compiling the source with that line removed rather than
importing the module directly.
"""
import asyncio
import io
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_SRC = Path(__file__).parent.parent / "asyncdemo.py"


def _load_namespace() -> dict:
    """Compile asyncdemo.py without the top-level asyncio.run() call."""
    code = _SRC.read_text(encoding="utf-8")
    safe = "\n".join(
        "" if line.strip().startswith("asyncio.run") else line
        for line in code.splitlines()
    )
    ns: dict = {}
    exec(compile(safe, str(_SRC), "exec"), ns)
    return ns


_NS = _load_namespace()


class TestTaskCoroutine(unittest.TestCase):

    def _run(self, coro):
        return asyncio.run(coro)

    def test_prints_started_message(self):
        captured = []
        with patch("builtins.print", side_effect=lambda *a, **k: captured.append(" ".join(str(x) for x in a))):
            self._run(_NS["task"]("Alpha", 0))
        self.assertTrue(any("Alpha started" in line for line in captured))

    def test_prints_finished_message(self):
        captured = []
        with patch("builtins.print", side_effect=lambda *a, **k: captured.append(" ".join(str(x) for x in a))):
            self._run(_NS["task"]("Alpha", 0))
        self.assertTrue(any("Alpha finished" in line for line in captured))

    def test_start_printed_before_finish(self):
        order = []
        def capture(*a, **k):
            msg = " ".join(str(x) for x in a)
            if "started" in msg or "finished" in msg:
                order.append(msg)
        with patch("builtins.print", side_effect=capture):
            self._run(_NS["task"]("Beta", 0))
        self.assertIn("Beta started", order[0])
        self.assertIn("Beta finished", order[-1])

    def test_task_uses_name_in_output(self):
        captured = []
        with patch("builtins.print", side_effect=lambda *a, **k: captured.append(" ".join(str(x) for x in a))):
            self._run(_NS["task"]("UniqueTaskName", 0))
        full_output = "\n".join(captured)
        self.assertIn("UniqueTaskName", full_output)

    def test_task_completes_after_delay(self):
        start = time.monotonic()
        asyncio.run(_NS["task"]("T", 0.05))
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.04)

    def test_ingestion_complete_printed(self):
        # rich's Console.print writes to stdout directly, not via builtins.print
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            self._run(_NS["task"]("MyTask", 0))
        self.assertIn("Ingestion complete", buf.getvalue())


class TestMainGather(unittest.TestCase):

    def test_all_three_tasks_complete(self):
        captured = []
        with patch("builtins.print", side_effect=lambda *a, **k: captured.append(" ".join(str(x) for x in a))):
            asyncio.run(_NS["main"]())
        full = "\n".join(captured)
        for name in ("A", "B", "C"):
            self.assertIn(f"{name} started", full)
            self.assertIn(f"{name} finished", full)

    def test_tasks_run_concurrently_not_sequentially(self):
        """Three tasks with 0.1s delay should finish in ~0.1s, not ~0.3s."""
        async def timed():
            start = time.monotonic()
            await asyncio.gather(
                _NS["task"]("A", 0.1),
                _NS["task"]("B", 0.1),
                _NS["task"]("C", 0.1),
            )
            return time.monotonic() - start

        elapsed = asyncio.run(timed())
        self.assertLess(elapsed, 0.25, "Tasks appear to be running sequentially")

    def test_main_returns_none(self):
        result = asyncio.run(_NS["main"]())
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
