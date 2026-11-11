"""Portable launch wrapper contract; no checkpoint or GPU is loaded."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts.run_rl_rules_match import main


class RulesMatchLauncherTests(unittest.TestCase):
    def test_dispatches_existing_runner_without_mutating_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / name for name in ('map.zip', 'scenario.json', 'model.pt')]
            for path in paths:
                path.write_bytes(b'fixture')
            output = root / 'match'
            argv = ['--input', str(paths[0]), '--scenario-input', str(paths[1]),
                    '--red-checkpoint', str(paths[2]), '--output', str(output), '--max-steps', '3']
            with patch('scripts.run_match.main', return_value=0) as runner:
                self.assertEqual(main(argv), 0)
            args = runner.call_args.args[0]
            options = dict(zip(args[::2], args[1::2]))
            self.assertEqual(options['--red'], 'ppo')
            self.assertEqual(options['--blue'], 'rules')
            self.assertEqual(options['--candidate-limit'], '255')
            self.assertEqual(options['--failure-policy'], 'abort')
            self.assertEqual(options['--red-device'], 'cpu')
            self.assertEqual(options['--max-steps'], '3')
            self.assertFalse(output.exists())
            self.assertTrue(all(path.read_bytes() == b'fixture' for path in paths))

    def test_invalid_steps_are_rejected_before_dispatch(self):
        with patch('scripts.run_match.main') as runner, self.assertRaises(SystemExit):
            main(['--max-steps', '0'])
        runner.assert_not_called()

    def test_existing_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = root / 'input'
            item.write_text('fixture')
            argv = ['--input', str(item), '--scenario-input', str(item), '--red-checkpoint', str(item),
                    '--output', str(root)]
            with patch('scripts.run_match.main') as runner, self.assertRaises(SystemExit):
                main(argv)
            runner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
