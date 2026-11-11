"""A failed launcher must retain raw evidence without inventing a replay."""
from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from scripts.realtime_run_storage import realtime_run_storage


class RealtimeFailureStorageTests(unittest.TestCase):
    def test_success_removes_scratch_not_published_output(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'run'
            with realtime_run_storage(output) as storage:
                (storage / 'facts.jsonl').write_text('raw')
                output.mkdir()
                (output / 'replay.zip').write_bytes(b'published')
            self.assertFalse(storage.exists())
            self.assertEqual((output / 'replay.zip').read_bytes(), b'published')

    def test_failure_keeps_raw_facts_root_cause_and_chain(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'run'
            stderr = StringIO()
            with redirect_stderr(stderr):
                with self.assertRaisesRegex(RuntimeError, 'cannot publish') as caught:
                    with realtime_run_storage(output) as storage:
                        (storage / 'facts.jsonl').write_text('committed-prefix\npartial')
                        try:
                            raise ValueError('first worker failure')
                        except ValueError as primary:
                            raise RuntimeError('cannot publish') from primary
            self.assertIsInstance(caught.exception.__cause__, ValueError)
            self.assertEqual((storage / 'facts.jsonl').read_text(), 'committed-prefix\npartial')
            report = json.loads((storage / 'failure.json').read_text())
            self.assertEqual(report['status'], 'FAILED_RUN_DIAGNOSTIC_ONLY')
            self.assertEqual(report['root_cause']['message'], 'first worker failure')
            self.assertIn('first worker failure', (storage / 'failure-traceback.txt').read_text())
            self.assertIn(str(storage), stderr.getvalue())
            self.assertFalse(output.exists())
            self.assertFalse(list(storage.rglob('replay.zip')))

    def test_report_is_bounded_and_excludes_traceback_locals(self):
        with tempfile.TemporaryDirectory() as root, redirect_stderr(StringIO()):
            private_local = 'do-not-serialize-local-values'
            with self.assertRaises(ValueError):
                with realtime_run_storage(Path(root) / 'run') as storage:
                    raise ValueError('x' * 100000)
            report = json.loads((storage / 'failure.json').read_text())
            self.assertEqual(len(report['root_cause']['message']), 2048)
            trace = (storage / 'failure-traceback.txt').read_text()
            self.assertLess(len(trace), 66000)
            self.assertNotIn(private_local, trace)

    def test_report_write_failure_preserves_primary_and_raw_storage(self):
        with tempfile.TemporaryDirectory() as root, redirect_stderr(StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'primary'):
                with realtime_run_storage(Path(root) / 'run') as storage:
                    (storage / 'facts').write_bytes(b'keep')
                    with patch('scripts.realtime_run_storage.Path.write_text', side_effect=OSError('disk full')):
                        # Explicitly call helper inside patch to emulate report I/O failure.
                        from scripts.realtime_run_storage import _retain_failure
                        _retain_failure(storage, Path(root) / 'run', RuntimeError('primary'))
                    raise RuntimeError('primary')
            self.assertEqual((storage / 'facts').read_bytes(), b'keep')

    def test_launcher_ctrl_c_keeps_failed_worker_evidence(self):
        from scripts import run_realtime_dual_control as launcher
        with tempfile.TemporaryDirectory() as root, redirect_stderr(StringIO()):
            output = Path(root) / 'run'
            bundle = MagicMock()
            session = MagicMock()
            session.reset.return_value = SimpleNamespace(red_binding='red', blue_binding='blue')
            primary = ValueError('worker original cause')
            wrapper = RuntimeError('failed session cannot publish')
            wrapper.__cause__ = primary
            session.publish.side_effect = wrapper
            server = MagicMock()
            server.server_address = ('127.0.0.1', 8003)
            server.serve_forever.side_effect = KeyboardInterrupt
            def factory(**kwargs):
                (kwargs['storage_root'] / 'committed-facts').write_text('preserve')
                return object()
            with patch.object(launcher.ScenarioBundle, 'from_path', return_value=bundle), \
                 patch.object(launcher, 'ScenarioMatchFactory', side_effect=factory), \
                 patch.object(launcher, 'RealtimeAgentSession', return_value=session), \
                 patch.object(launcher, 'source_weapon_profiles', return_value={}), \
                 patch.object(launcher, 'create_realtime_dual_control_server', return_value=server), \
                 patch('builtins.print'):
                with self.assertRaises(RuntimeError) as caught:
                    launcher.main(['--output-dir', str(output), '--run-namespace', 'failed-launch'])
            self.assertIs(caught.exception, wrapper)
            session.close.assert_called_once()
            folders = list(Path(root).glob('.run.incomplete-*'))
            self.assertEqual(len(folders), 1)
            self.assertEqual((folders[0] / 'committed-facts').read_text(), 'preserve')
            report = json.loads((folders[0] / 'failure.json').read_text())
            self.assertEqual(report['root_cause']['message'], 'worker original cause')
            self.assertFalse(output.exists())

    def test_launcher_ctrl_c_publishes_then_cleans_scratch(self):
        from scripts import run_realtime_dual_control as launcher
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'run'
            session, server = MagicMock(), MagicMock()
            server.server_address = ('127.0.0.1', 8003)
            server.serve_forever.side_effect = KeyboardInterrupt
            def publish(*args, **kwargs):
                output.mkdir()
                (output / 'replay.zip').write_bytes(b'published')
                return SimpleNamespace(
                    run_namespace='success', episode_id='one', tick_count=1,
                    fact_frame_count=1, accepted_command_count=0, applied_command_count=0,
                    terminal=False, truncated=True, output_dir=output,
                    replay_path=output / 'replay.zip', analysis_path=output / 'analysis.json',
                    command_path=output / 'commands.jsonl', tick_path=output / 'ticks.jsonl',
                )
            session.publish.side_effect = publish
            with patch.object(launcher.ScenarioBundle, 'from_path'), \
                 patch.object(launcher, 'ScenarioMatchFactory'), \
                 patch.object(launcher, 'RealtimeAgentSession', return_value=session), \
                 patch.object(launcher, 'source_weapon_profiles', return_value={}), \
                 patch.object(launcher, 'create_realtime_dual_control_server', return_value=server), \
                 patch('builtins.print'):
                self.assertEqual(launcher.main(['--output-dir', str(output), '--run-namespace', 'success']), 0)
            session.publish.assert_called_once()
            session.close.assert_called_once()
            server.server_close.assert_called_once()
            self.assertEqual((output / 'replay.zip').read_bytes(), b'published')
            self.assertFalse(list(Path(root).glob('.run.incomplete-*')))

    def test_launcher_partial_startup_always_closes_owned_resources(self):
        from scripts import run_realtime_dual_control as launcher
        for stage in ('reset', 'bind', 'start', 'serve', 'server_close', 'session_close'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as root:
                output = Path(root) / 'run'
                session, server, bundle = MagicMock(), MagicMock(), MagicMock()
                server.server_address = ('127.0.0.1', 8003)
                server.serve_forever.side_effect = KeyboardInterrupt
                primary = RuntimeError(f'primary {stage}')
                create = MagicMock(return_value=server)
                if stage == 'bind':
                    create.side_effect = primary
                else:
                    method = {
                        'reset': session.reset,
                        'start': session.start_realtime,
                        'serve': server.serve_forever,
                        'server_close': server.server_close,
                        'session_close': session.close,
                    }[stage]
                    method.side_effect = primary
                if stage in ('start', 'serve'):
                    server.server_close.side_effect = OSError('secondary server cleanup')
                    session.close.side_effect = OSError('secondary session cleanup')
                with patch.object(launcher.ScenarioBundle, 'from_path', return_value=bundle), \
                     patch.object(launcher, 'ScenarioMatchFactory'), \
                     patch.object(launcher, 'RealtimeAgentSession', return_value=session), \
                     patch.object(launcher, 'source_weapon_profiles', return_value={}), \
                     patch.object(launcher, 'create_realtime_dual_control_server', create), \
                     patch('builtins.print'):
                    with self.assertRaises(RuntimeError) as caught:
                        launcher.main(['--output-dir', str(output), '--run-namespace', 'startup-test'])
                self.assertIs(caught.exception, primary)
                session.close.assert_called_once()
                if stage in ('reset', 'bind'):
                    server.server_close.assert_not_called()
                else:
                    server.server_close.assert_called_once()
                if stage != 'session_close':
                    session.publish.assert_not_called()
                else:
                    session.publish.assert_called_once()
                retained = next(Path(root).glob('.run.incomplete-*'))
                report = json.loads((retained / 'failure.json').read_text())
                self.assertEqual(report['exception']['message'], f'primary {stage}')
                if stage in ('start', 'serve'):
                    trace = (retained / 'failure-traceback.txt').read_text()
                    self.assertIn('secondary server cleanup', trace)
                    self.assertIn('secondary session cleanup', trace)

    def test_scratch_cleanup_failure_keeps_report_and_published_output(self):
        with tempfile.TemporaryDirectory() as root, redirect_stderr(StringIO()):
            output = Path(root) / 'run'
            with self.assertRaisesRegex(OSError, 'cleanup denied'):
                with patch('scripts.realtime_run_storage.shutil.rmtree', side_effect=OSError('cleanup denied')):
                    with realtime_run_storage(output) as storage:
                        output.mkdir()
                        (output / 'replay.zip').write_bytes(b'valid publication')
            self.assertEqual((output / 'replay.zip').read_bytes(), b'valid publication')
            self.assertTrue((storage / 'failure.json').exists())

    def test_cleanup_note_with_unprintable_error_preserves_primary(self):
        from scripts.run_realtime_dual_control import _note_cleanup_error
        class UnprintableError(Exception):
            def __str__(self):
                raise ValueError('formatting failed')
        primary = ValueError('primary')
        _note_cleanup_error(primary, 'session', UnprintableError())
        self.assertIn('<exception message unavailable>', primary.__notes__[0])

    def test_existing_output_is_rejected_before_scratch_creation(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'run'
            output.mkdir()
            (output / 'keep').write_text('existing')
            with self.assertRaises(FileExistsError):
                with realtime_run_storage(output):
                    self.fail('must reject before starting')
            self.assertEqual([p.name for p in Path(root).iterdir()], ['run'])
            self.assertEqual((output / 'keep').read_text(), 'existing')

    def test_unprintable_error_does_not_mask_primary(self):
        class UnprintableError(Exception):
            def __str__(self):
                raise ValueError('formatting failed')
        primary = UnprintableError()
        with tempfile.TemporaryDirectory() as root, redirect_stderr(StringIO()):
            with self.assertRaises(UnprintableError) as caught:
                with realtime_run_storage(Path(root) / 'run') as storage:
                    raise primary
            self.assertIs(caught.exception, primary)
            report = json.loads((storage / 'failure.json').read_text())
            self.assertEqual(report['exception']['message'], '<exception message unavailable>')

    def test_failed_retries_have_unique_evidence_directories(self):
        with tempfile.TemporaryDirectory() as root, redirect_stderr(StringIO()):
            paths = []
            for _ in range(2):
                with self.assertRaises(ValueError):
                    with realtime_run_storage(Path(root) / 'run') as storage:
                        paths.append(storage)
                        raise ValueError('failure')
            self.assertNotEqual(paths[0], paths[1])
            self.assertTrue(all(path.exists() for path in paths))


if __name__ == '__main__':
    unittest.main()
