from pathlib import Path

from wargame_native_engine.cli.run_game import main


if __name__ == "__main__":
    raise SystemExit(main([
        "--scenario", str(Path(__file__).with_name("minimal_scenario.json")),
        "--red-agent", "first-valid", "--blue-agent", "first-valid",
        "--seed", "42", "--max-ticks", "500",
        "--output", "/tmp/wargame-native-minimal-example",
    ]))
