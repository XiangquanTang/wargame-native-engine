#!/usr/bin/env python3
"""Read/extract independent decision samples without hash lookup or source replay."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import tempfile
from wargame_native_engine.matches.decision_records import load_record


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('non-finite JSON value')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', type=Path, required=True)
    p.add_argument('--side', choices=('red', 'blue'))
    p.add_argument('--start-window', type=int, default=0)
    p.add_argument('--end-window', type=int)
    p.add_argument('--limit', type=int, default=20, help='maximum human-readable lines; 0 means all')
    p.add_argument('--output', type=Path, help='new self-contained JSONL clip; never overwrite')
    a = p.parse_args(argv)
    if a.start_window < 0 or a.limit < 0 or (a.end_window is not None and a.end_window < a.start_window):
        p.error('invalid window range or limit')
    if a.output and a.output.exists():
        p.error('--output must not exist')
    temporary = None
    count = 0
    try:
        if a.output:
            a.output.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='.decision-clip-', dir=a.output.parent)
            output = os.fdopen(fd, 'w', encoding='utf-8')
        else:
            output = None
        try:
            with a.input.open(encoding='utf-8') as source:
                for line_number, line in enumerate(source, 1):
                    if not line.strip():
                        continue
                    row = load_record(json.loads(line, object_pairs_hook=_pairs, parse_constant=_constant))
                    if (a.side and row['side'] != a.side) or row['window'] < a.start_window or (a.end_window is not None and row['window'] > a.end_window):
                        continue
                    count += 1
                    if output:
                        output.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
                    if not a.limit or count <= a.limit:
                        selection = row['selection']
                        desc = selection['description'] if selection else '不下达指令'
                        plan = row['policy_input']['strategic_plan']
                        task = None
                        if plan and selection:
                            actor = selection['action'].get('actor_id')
                            task = next((x for x in plan.get('assignments', ()) if actor in x['unit_ids']), None)
                        print(f"窗口{row['window']} 时间{row['sim_time_ms']}ms | {desc} | "
                              f"候选{len(row['policy_input']['legal_candidates'])} | "
                              f"执行:{row['execution']['status']} | LLM任务:{json.dumps(task, ensure_ascii=False)}")
        finally:
            if output:
                output.close()
        if not count:
            raise ValueError('no matching decision samples')
        if a.output:
            # O_EXCL semantics for final destination even if another process races.
            os.link(temporary, a.output)
            os.unlink(temporary)
            temporary = None
            print('CLIP:', a.output)
        print('SAMPLES:', count)
        return 0
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()


if __name__ == '__main__':
    raise SystemExit(main())
