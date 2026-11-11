"""Generation selects models explicitly and never replaces old replay evidence."""
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from wargame_native_engine.agents import NoOpAgent
from wargame_native_engine.analysis import analyze_replay_to_jsonl
from wargame_native_engine.analysis.generation import (
    generate_native_analysis, generate_source_analysis,
)
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.cli.generate_analysis import main, parser
from wargame_native_engine.compat.source_frame.template import source_frame_template_for_scenario
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from wargame_native_engine.training import NativeTrainingEnv
from wargame_native_engine.viewer.replay_view import load_replay_view

ROOT = Path(__file__).resolve().parents[1]


class AnalysisGenerationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        scenario = load_scenario(ROOT / 'examples/source_movement_scenario.json')
        env = NativeTrainingEnv(
            lambda: SourceCompatibleEnvironment(GameSession(NativeEngineBackend(scenario)),
                template=source_frame_template_for_scenario(scenario)),
            controlled_side='red', opponent=NoOpAgent(), storage_root=self.root/'storage',
            max_steps=2, decision_interval_ms=1000, frame_interval_ms=1000,
            training_interface_version='decision-v2')
        try:
            env.reset(seed=42)
            self.replay = env.publish(self.root/'original').replay_path
        finally:
            env.close()

    def test_source_model_is_byte_identical_and_viewer_compatible(self):
        before = self.replay.read_bytes()
        old_sidecar = (self.replay.parent/'frame_analysis.native.jsonl').read_bytes()
        reference = self.root/'reference.jsonl'
        analyze_replay_to_jsonl(self.replay, reference)
        result = generate_source_analysis(self.replay, self.root/'source-analysis')
        self.assertEqual(result.analysis.output_path.read_bytes(), reference.read_bytes())
        self.assertEqual(result.replay_path.read_bytes(), before)
        self.assertEqual(self.replay.read_bytes(), before)
        self.assertEqual((self.replay.parent/'frame_analysis.native.jsonl').read_bytes(), old_sidecar)
        self.assertEqual(load_replay_view(result.output_directory).frame_count, result.analysis.frame_count)

    def test_native_identity_is_distinct_hashes_align_and_unknown_is_preserved(self):
        source = generate_source_analysis(self.replay, self.root/'source-model')
        native = generate_native_analysis(self.replay, self.root/'native-model')
        source_view = load_replay_view(source.output_directory)
        native_view = load_replay_view(native.output_directory)
        self.assertNotEqual(source_view.model_id, native_view.model_id)
        self.assertNotEqual(source_view.model_hash, native_view.model_hash)
        self.assertEqual(native.replay_path.read_bytes(), self.replay.read_bytes())
        from wargame_native_engine.analysis.jsonl import analysis_sidecar_from_jsonl
        sidecar = analysis_sidecar_from_jsonl(native.analysis.output_path.read_text())
        original = analysis_sidecar_from_jsonl(source.analysis.output_path.read_text())
        for old, new in zip(original.timeline.frame_analyses, sidecar.timeline.frame_analyses):
            self.assertEqual(old.provenance.frame_hash, new.provenance.frame_hash)
            self.assertEqual(old.provenance.scenario_hash, new.provenance.scenario_hash)
            for unit in new.unit_analyses:
                self.assertIsNone(unit.effective_power)

    def test_existing_directory_and_input_descendant_are_rejected(self):
        output = self.root/'existing'
        output.mkdir()
        marker = output/'keep.txt'
        marker.write_text('keep')
        with self.assertRaises(FileExistsError):
            generate_source_analysis(self.replay, output)
        with self.assertRaisesRegex(ValueError, 'outside'):
            generate_source_analysis(self.replay, self.replay.parent/'new')
        self.assertEqual(marker.read_text(), 'keep')

    def test_failure_leaves_no_partial_result(self):
        output = self.root/'failed'
        with patch('wargame_native_engine.analysis.generation.analyze_replay_to_jsonl',
                   side_effect=ValueError('bad frame')):
            with self.assertRaisesRegex(ValueError, 'bad frame'):
                generate_source_analysis(self.replay, output)
        self.assertFalse(output.exists())
        self.assertTrue(self.replay.exists())

    def test_native_model_rejects_historical_container_and_missing_manifest(self):
        with self.assertRaisesRegex(ValueError, 'Native replay container'):
            generate_native_analysis(self.replay, self.root/'bad', source_format='historical')
        old_dir = self.root/'old'
        old_dir.mkdir()
        old = old_dir/'replay.zip'
        with ZipFile(self.replay) as source, ZipFile(old, 'w') as target:
            for name in source.namelist():
                if name != 'replay_manifest.native.jsonl':
                    target.writestr(name, source.read(name))
        with self.assertRaisesRegex(ValueError, 'Native replay manifest'):
            generate_native_analysis(old, self.root/'bad')
        self.assertFalse((self.root/'bad').exists())

    def test_cli_requires_model_and_preserves_old_sidecar(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(['--input', str(self.replay), '--output-directory', str(self.root/'new')])
        with redirect_stdout(StringIO()):
            code = main(['--input', str(self.replay), '--output-directory', str(self.root/'cli'),
                         '--analysis-model', 'source'])
        self.assertEqual(code, 0)
        self.assertTrue((self.root/'cli/frame_analysis.native.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
