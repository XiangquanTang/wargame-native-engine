"""Replay loading remains fully validated while streaming each frame once."""
from contextlib import contextmanager
import gzip
import json
from pathlib import Path
import tempfile
from threading import Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zipfile import ZipFile

from wargame_native_engine.agents import NoOpAgent
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.template import source_frame_template_for_scenario
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from wargame_native_engine.training import NativeTrainingEnv
from wargame_native_engine.viewer.map_config import coordinate_fallback_record
from wargame_native_engine.viewer.replay_view import load_replay_view, _ViewerSourceFrameImporter
from wargame_native_engine.viewer.server import ReplayViewerHTTPServer, _accepts_gzip

ROOT = Path(__file__).resolve().parents[1]


class ReplayLoadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        scenario = load_scenario(ROOT / 'examples/source_movement_scenario.json')
        env = NativeTrainingEnv(
            lambda: SourceCompatibleEnvironment(GameSession(NativeEngineBackend(scenario)),
                template=source_frame_template_for_scenario(scenario)),
            controlled_side='red', opponent=NoOpAgent(), storage_root=cls.root/'storage',
            max_steps=4, decision_interval_ms=1000, frame_interval_ms=1000,
            training_interface_version='decision-v2')
        try:
            env.reset(seed=42)
            for _ in range(3):
                binding = env.current_decision_binding
                action = next((i for i, item in enumerate(binding.action_space.actions)
                    if item.get('action', {}).get('action_type') == 'WAIT'), 0)
                env.step_bound_action(action, binding)
            env.publish(cls.root/'published')
        finally:
            env.close()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_single_stream_and_fallback_bounds_match(self):
        count = 0
        original = _ViewerSourceFrameImporter.from_dict
        def counted(instance, *args, **kwargs):
            nonlocal count
            count += 1
            return original(instance, *args, **kwargs)
        with patch.object(_ViewerSourceFrameImporter, 'from_dict', counted), patch(
            'wargame_native_engine.io.importers.source_replay_archive.ZipFile', wraps=ZipFile
        ) as opened:
            view = load_replay_view(self.root/'published')
            self.assertEqual(count, view.frame_count)
            self.assertEqual(opened.call_count, 4)
        self.assertEqual(view.map_record()['bounds'], coordinate_fallback_record(view.archive.frames)['bounds'])
        self.assertEqual(len(tuple(view.archive.frames)), view.frame_count)

    def test_corrupt_last_frame_rejected_not_just_first_frame_checked(self):
        output = self.root/'corrupt'
        output.mkdir(exist_ok=True)
        (output/'frame_analysis.native.jsonl').write_bytes((self.root/'published/frame_analysis.native.jsonl').read_bytes())
        with ZipFile(self.root/'published/replay.zip') as source, ZipFile(output/'replay.zip', 'w') as target:
            final_name = f'{sum(name[:-5].isdigit() for name in source.namelist() if name.endswith(".json"))-1}.json'
            for name in source.namelist():
                payload = source.read(name)
                target.writestr(name, payload+b' ' if name == final_name else payload)
        with self.assertRaisesRegex(ValueError, 'frame hash mismatch'):
            load_replay_view(output)

    def test_native_map_validation_uses_same_single_stream(self):
        with patch('wargame_native_engine.io.importers.source_replay_archive.ZipFile', wraps=ZipFile) as opened:
            view = load_replay_view(self.root/'published', native_scenario_map=ROOT/'examples/source_movement_scenario.json')
            self.assertEqual(opened.call_count, 4)
            self.assertEqual(view.map_record()['source']['replay_content_binding']['scenario_hash'], 'matched')

    def test_bundle_gzip_and_cache_preserve_response_and_errors(self):
        view = load_replay_view(self.root/'published')
        server = ReplayViewerHTTPServer(('127.0.0.1', 0), view)
        thread = Thread(target=server.serve_forever, daemon=True);thread.start()
        try:
            base=f'http://127.0.0.1:{server.server_port}'
            route='/api/frame-bundle/1'
            with urlopen(Request(base+route, headers={'Accept-Encoding':'gzip'})) as response:
                self.assertEqual(response.headers['Content-Encoding'], 'gzip')
                self.assertEqual(response.headers['Vary'], 'Accept-Encoding')
                record=json.loads(gzip.decompress(response.read()))
            self.assertEqual(record, {'frame':view.frame_record(1), 'analysis':view.analysis_record(1)})
            cached=server._response_cache[route]
            with urlopen(Request(base+route, headers={'Accept-Encoding':'gzip;q=0, *;q=1'})) as response:
                self.assertIsNone(response.headers.get('Content-Encoding'))
                self.assertEqual(json.loads(response.read()), record)
            self.assertIs(server._response_cache[route], cached)
            for suffix in ('-1','99999','nan'):
                with self.assertRaises(HTTPError) as caught:
                    urlopen(base+'/api/frame-bundle/'+suffix)
                self.assertEqual(caught.exception.code, 400)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)

    def test_response_cache_has_byte_and_entry_limits(self):
        server=ReplayViewerHTTPServer(('127.0.0.1',0), SimpleNamespace())
        try:
            for index in range(130):server.encoded_record(str(index), lambda:{'value': 'x'*1000})
            self.assertEqual(len(server._response_cache),128)
            self.assertNotIn('0',server._response_cache)
            self.assertLessEqual(server._response_cache_bytes, 32*1024*1024)
            self.assertEqual(server._response_cache_bytes, sum(sum(map(len,value)) for value in server._response_cache.values()))
            with patch('wargame_native_engine.viewer.server._json_bytes', return_value=b'x'*(17*1024*1024)), patch('wargame_native_engine.viewer.server.gzip.compress', return_value=b'z'):
                server.encoded_record('large-1', lambda: {})
                server.encoded_record('large-2', lambda: {})
                self.assertNotIn('large-1', server._response_cache)
                self.assertIn('large-2', server._response_cache)
                self.assertLessEqual(server._response_cache_bytes, 32*1024*1024)
            with patch('wargame_native_engine.viewer.server._json_bytes', return_value=b'x'*(32*1024*1024)), patch('wargame_native_engine.viewer.server.gzip.compress', return_value=b'z'):
                server.encoded_record('oversized', lambda: {})
                self.assertNotIn('oversized', server._response_cache)
        finally:server.server_close()

    def test_gzip_quality_respected(self):
        for value in ('', 'gzip;q=0', 'gzip;q=0, *;q=1', 'gzip;q=NaN', 'gzip;q=2'):
            self.assertFalse(_accepts_gzip(value), value)
        for value in ('gzip', 'br, gzip;q=0.5', '*;q=1'):
            self.assertTrue(_accepts_gzip(value), value)
