import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deeppurge.history import History, secure_open
from deeppurge.models import OperationResult


class HistoryTests(unittest.TestCase):
    def test_invalid_utf8_does_not_hide_valid_history_or_block_new_results(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_STATE_HOME': directory}):
            history = History()
            (history.root / 'activity.jsonl').write_bytes(b'\xff\n{"app":"valid fixture"}\n')
            self.assertEqual(history.read(), [{'app': 'valid fixture'}])
            history.append('New fixture', 'Remove', OperationResult('success', 'Verified result'))
            self.assertEqual([entry['app'] for entry in history.read()], ['valid fixture', 'New fixture'])

    def test_predictable_temp_hardlink_cannot_overwrite_an_unrelated_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_STATE_HOME': directory}):
            history = History()
            outside = Path(directory) / 'unrelated'; outside.write_text('preserve')
            (history.root / 'activity.new').hardlink_to(outside)
            history.append('Fixture', 'Remove', OperationResult('success', 'Completed'))
            self.assertEqual(outside.read_text(), 'preserve')
            self.assertEqual(len(history.read()), 1)
            self.assertEqual(list(history.root.glob('.activity-*.tmp')), [])

    def test_shared_state_file_is_rejected_before_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / 'unrelated'; outside.write_text('preserve')
            state = Path(directory) / 'state'; state.hardlink_to(outside)
            with self.assertRaises(OSError):
                secure_open(state, os.O_WRONLY | os.O_TRUNC)
            self.assertEqual(outside.read_text(), 'preserve')

    def test_fifo_state_is_rejected_without_waiting_for_a_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fifo'; os.mkfifo(path)
            with self.assertRaises(OSError):
                secure_open(path, os.O_RDONLY)

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
