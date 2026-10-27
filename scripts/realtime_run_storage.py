"""Durable scratch storage for a realtime run, retained on failure.

The retained directory is diagnostic evidence, never a published replay. Keeping
it beside the requested output avoids losing already-written facts when the
launcher unwinds after a worker failure or shutdown timeout.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import sys
import tempfile
import traceback
from typing import Iterator

_TRACEBACK_LIMIT = 64 * 1024
_MESSAGE_LIMIT = 2048


def _exception_identity(error: BaseException) -> dict[str, str]:
    try:
        message = str(error)[:_MESSAGE_LIMIT]
    except BaseException:
        message = '<exception message unavailable>'
    return {
        'type': f'{type(error).__module__}.{type(error).__qualname__}',
        'message': message,
    }


def _root_cause(error: BaseException) -> BaseException:
    seen: set[int] = set()
    while id(error) not in seen:
        seen.add(id(error))
        cause = error.__cause__
        if cause is None and not error.__suppress_context__:
            cause = error.__context__
        if cause is None or id(cause) in seen:
            break
        error = cause
    return error


def _retain_failure(storage: Path, output: Path, error: BaseException) -> None:
    """Best-effort bounded report; never replace the original exception."""
    report = {
        'schema': 'realtime-failure-report/1.0',
        'status': 'FAILED_RUN_DIAGNOSTIC_ONLY',
        'requested_output': str(output),
        'storage_directory': str(storage),
        'exception': _exception_identity(error),
        'root_cause': _exception_identity(_root_cause(error)),
        'warning': 'Raw facts may include an incomplete final step. This is not a valid published replay.',
    }
    try:
        (storage / 'failure.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8',
        )
        formatted = ''.join(traceback.TracebackException.from_exception(
            error, capture_locals=False,
        ).format())
        if len(formatted) > _TRACEBACK_LIMIT:
            formatted = '[traceback truncated; root identity is in failure.json]\n' + formatted[-_TRACEBACK_LIMIT:]
        (storage / 'failure-traceback.txt').write_text(formatted, encoding='utf-8')
    except BaseException as diagnostic_error:
        # In particular, a full disk must not hide the original failure or
        # trigger cleanup of the only remaining raw run evidence.
        try:
            print(f'FAILURE_REPORT_WRITE_ERROR: {type(diagnostic_error).__name__}', file=sys.stderr, flush=True)
        except BaseException:
            pass
    try:
        print(f'FAILED_RUN_DIAGNOSTICS: {storage}', file=sys.stderr, flush=True)
        print('原始运行资料已保留；不是完整复盘，请保留此目录用于排查。', file=sys.stderr, flush=True)
    except BaseException:
        pass


@contextmanager
def realtime_run_storage(output_dir: Path, *, overwrite: bool = False) -> Iterator[Path]:
    """Retain evidence on any failure; remove scratch only on normal success."""
    output = Path(output_dir).absolute()
    if output.exists() and not overwrite:
        raise FileExistsError(f'Output already exists; choose a new run directory: {output}')
    if output.exists() and not output.is_dir():
        raise NotADirectoryError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    storage = Path(tempfile.mkdtemp(
        prefix=f'.{output.name[:80]}.incomplete-', dir=output.parent,
    ))
    try:
        yield storage
    except BaseException as error:
        _retain_failure(storage, output, error)
        raise
    else:
        try:
            shutil.rmtree(storage)
        except BaseException as error:
            # Cleanup can fail after publication succeeded. Retain diagnostics
            # without claiming that an existing published replay is invalid.
            _retain_failure(storage, output, error)
            raise
