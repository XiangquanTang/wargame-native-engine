#!/usr/bin/env python3
"""Offline NanoJev unified-games-v1 compatibility repair for Transformers 4.51.3.

Preview: python fix_nanojev_compat.py --checkpoint-dir PATH --check
Apply:   python fix_nanojev_compat.py --checkpoint-dir PATH
Stop the inference service before applying. No packages or weights are changed.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

SPECIALS = [
    '<|im_start|>', '<|im_end|>', '<|object_ref_start|>', '<|object_ref_end|>',
    '<|box_start|>', '<|box_end|>', '<|quad_start|>', '<|quad_end|>',
    '<|vision_start|>', '<|vision_end|>', '<|vision_pad|>', '<|image_pad|>',
    '<|video_pad|>',
]
ROPE = {'rope_theta': 1000000, 'rope_type': 'default'}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def regular(path):
    require(not path.is_symlink() and path.is_file(),
            'Missing file, non-regular file, or symlink: ' + str(path))


def encode(obj):
    return (json.dumps(obj, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def atomic_write(path, payload, mode):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--checkpoint-dir', required=True, type=Path)
    parser.add_argument('--check', action='store_true',
                        help='Validate proposed repairs in temporary copies; do not modify checkpoint')
    args = parser.parse_args()
    root = args.checkpoint_dir.expanduser().absolute()
    for directory in [root, root / 'tokenizer', root / 'backbone_config']:
        require(directory.is_dir() and not directory.is_symlink(),
                'Missing directory or symlink: ' + str(directory))
    # Also reject symlinked ancestors rather than following an unexpected destination.
    for ancestor in root.parents:
        require(not ancestor.is_symlink(), 'Symlinked ancestor: ' + str(ancestor))
    tp = root / 'tokenizer' / 'tokenizer_config.json'
    bp = root / 'backbone_config' / 'config.json'
    vocab = root / 'tokenizer' / 'tokenizer.json'
    for path in [tp, bp, vocab]:
        regular(path)
    originals = {p: p.read_bytes() for p in [tp, bp]}
    modes = {p: stat.S_IMODE(p.stat().st_mode) for p in originals}
    tc, bc = (json.loads(originals[p]) for p in [tp, bp])
    require(isinstance(tc, dict) and isinstance(bc, dict), 'Configurations must be JSON objects')
    new_tc, new_bc = copy.deepcopy(tc), copy.deepcopy(bc)
    require(tc.get('tokenizer_class') in ('Qwen2Tokenizer', 'Qwen2TokenizerFast'),
            'Unexpected tokenizer class; refusing to modify')
    extra = tc.get('extra_special_tokens', {})
    additional = tc.get('additional_special_tokens', [])
    require(isinstance(additional, list) and all(isinstance(x, str) for x in additional),
            'Unexpected additional_special_tokens format')
    if isinstance(extra, list):
        require(extra == SPECIALS, 'Unexpected extra_special_tokens list; refusing to discard or reinterpret tokens')
        new_tc['additional_special_tokens'] = list(dict.fromkeys(additional + extra))
        del new_tc['extra_special_tokens']
    else:
        require(isinstance(extra, dict) and all(isinstance(k, str) and isinstance(v, str)
                                               for k, v in extra.items()),
                'Unexpected extra_special_tokens format')
        require(all(s in additional for s in SPECIALS),
                'Expected 13 tokens absent from additional_special_tokens; refusing to guess')
    require(bc.get('model_type') == 'qwen3', 'Unexpected backbone model type')
    require(bc.get('rope_parameters') == ROPE,
            'Unexpected RoPE parameters; this repair only supports the official default RoPE schema')
    require('rope_theta' not in bc or bc['rope_theta'] == ROPE['rope_theta'],
            'Conflicting legacy rope_theta; refusing to overwrite')
    require('rope_scaling' not in bc or bc['rope_scaling'] is None,
            'Conflicting legacy rope_scaling; refusing to overwrite')
    new_bc['rope_theta'] = ROPE['rope_theta']
    new_bc['rope_scaling'] = None
    proposed = {tp: encode(new_tc), bp: encode(new_bc)}
    changed = [p for p, old, new in [(tp, tc, new_tc), (bp, bc, new_bc)] if old != new]

    import transformers
    from transformers import AutoConfig, AutoTokenizer
    from tokenizers import Tokenizer
    require(transformers.__version__ == '4.51.3',
            'Run in the service environment with Transformers 4.51.3; found ' + transformers.__version__)
    raw = Tokenizer.from_file(str(vocab))
    raw_vocab = raw.get_vocab()
    require(all(raw.token_to_id(s) == 151644 + i for i, s in enumerate(SPECIALS)),
            'Official special-token IDs do not match; refusing repair')

    def validate(tokenizer_dir, backbone_dir):
        tokenizer = AutoTokenizer.from_pretrained(str(tokenizer_dir), local_files_only=True,
                                                  trust_remote_code=False)
        require(tokenizer.get_vocab() == raw_vocab, 'Validation failed: vocabulary or IDs changed')
        for s in SPECIALS:
            require(s in tokenizer.all_special_tokens, 'Missing special-token registration: ' + s)
            require(tokenizer.encode(s, add_special_tokens=False) == [raw.token_to_id(s)],
                    'Special-token encoding changed: ' + s)
        for sample in ['hello world', '你好，世界！',
                       '<|im_start|>user\n选择动作：left/right<|im_end|>\n']:
            require(tokenizer.encode(sample, add_special_tokens=False) ==
                    raw.encode(sample, add_special_tokens=False).ids,
                    'Sample tokenization changed')
        config = AutoConfig.from_pretrained(str(backbone_dir), local_files_only=True,
                                           trust_remote_code=False)
        require(config.rope_theta == 1000000 and config.rope_scaling is None,
                'Validation failed: legacy RoPE settings not effective')

    # All validation occurs on copies before making backups or changing either file.
    with tempfile.TemporaryDirectory(prefix='nanojev-compat-check-') as temp:
        temp = Path(temp)
        tdir, bdir = temp / 'tokenizer', temp / 'backbone_config'
        tdir.mkdir()
        bdir.mkdir()
        for path in (root / 'tokenizer').iterdir():
            require(not path.is_symlink(), 'Symlink in tokenizer directory: ' + str(path))
            if path.is_file():
                shutil.copy2(path, tdir / path.name)
        (tdir / tp.name).write_bytes(proposed[tp])
        (bdir / bp.name).write_bytes(proposed[bp])
        validate(tdir, bdir)
    print('PASS: full vocabulary unchanged (%d entries), 13 special-token IDs preserved, RoPE theta=1000000.' % len(raw_vocab))
    if not changed:
        print('Already compatible. No files changed.')
        return
    print(('Would update: ' if args.check else 'Will update: ') +
          ', '.join(str(p.relative_to(root)) for p in changed))
    if args.check:
        print('CHECK ONLY: checkpoint unchanged. Run without --check to apply.')
        return
    for p in originals:
        regular(p)
        require(p.read_bytes() == originals[p], 'File changed during validation: ' + str(p))
    backup_dir = Path(tempfile.mkdtemp(prefix='compat-backup-', dir=str(root)))
    for p in changed:
        destination = backup_dir / p.relative_to(root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(originals[p])
        require(destination.read_bytes() == originals[p], 'Backup verification failed')
    print('Backup: ' + str(backup_dir), flush=True)
    committed = []
    try:
        for p in changed:
            regular(p)
            require(p.read_bytes() == originals[p], 'File changed before commit: ' + str(p))
            atomic_write(p, proposed[p], modes[p])
            committed.append(p)
        validate(root / 'tokenizer', root / 'backbone_config')
    except BaseException:
        failures = []
        for p in reversed(committed):
            try:
                atomic_write(p, originals[p], modes[p])
            except BaseException as error:
                failures.append(str(p) + ': ' + str(error))
        if failures:
            print('ROLLBACK ERROR: restore from ' + str(backup_dir) + '\n' + '\n'.join(failures), file=sys.stderr)
        else:
            print('Validation/commit failed; modified configs restored from originals.', file=sys.stderr)
        raise
    print('SUCCESS: both configs verified. Weights, tokenizer.json, installed packages and service flags unchanged.')
    print('Restart using the existing fp32 service command; do not add --disable-native-triton.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        sys.exit(1)
