import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deeppurge.history import History
from deeppurge.models import OperationResult


class HistoryTests(unittest.TestCase):
    def test_result_roundtrip_and_process_lock(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_STATE_HOME': directory}):
            history = History()
            with history.operation_lock():
                with self.assertRaises(BlockingIOError):
                    with history.operation_lock():
                        self.fail('A concurrent operation acquired the same lock')
                history.append('Fixture', 'Remove', OperationResult('failed', 'Authentication cancelled'))
            self.assertEqual(history.read()[0]['status'], 'failed')
            self.assertEqual(history.root, Path(directory) / 'deep-purge-ultra')

    def test_symlink_state_file_refused_and_corrupt_entry_skipped(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_STATE_HOME': directory}):
            history = History()
            outside = Path(directory) / 'private'; outside.write_text('preserve')
            path = history.root / 'activity.jsonl'
            path.symlink_to(outside)
            with self.assertRaises(OSError):
                history.read()
            path.unlink()
            path.write_text('not-json\n{"app":"fixture","status":"success"}\n')
            self.assertEqual(len(history.read()), 1)
            self.assertEqual(outside.read_text(), 'preserve')
