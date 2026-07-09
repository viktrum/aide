import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "judge"))
from judge_store import new_run_dir, finalize_run, get_latest_run_dir, list_runs, publish_artifacts


class JudgeStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_run_archive_and_latest(self):
        run_id, run_dir = new_run_dir(self.root)
        (run_dir / "rulebook.json").write_text('{"version": 2}')
        (run_dir / "baseline.json").write_text('{"sessions_scanned": 10}')
        meta = {"generated_at": "2026-07-09", "sessions_scanned": 10, "segments_flagged": 2}
        finalize_run(run_id, run_dir, meta, self.root)

        self.assertEqual(get_latest_run_dir(self.root), run_dir)
        self.assertTrue((self.root / "latest.json").exists())
        self.assertTrue((self.root / "rulebook.json").exists())
        runs = list_runs(self.root)
        self.assertEqual(runs[0]["run_id"], run_id)


if __name__ == "__main__":
    unittest.main()
