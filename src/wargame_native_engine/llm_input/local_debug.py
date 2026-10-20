"""Bounded local CUDA readback experiment; no actions and no training."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import resource
import shutil
import sys
from time import monotonic

from .readback import PROMPT_VERSION, _leaves, check_readback, compact, expected_readback, messages

ROOT = Path(__file__).resolve().parents[3]
MODEL_ID = "Qwen/Qwen3-1.7B"
GIB = 1024 ** 3
INFERENCE_RSS_BYTES = 6 * GIB  # TASK-054-R1 only; other monitor callers retain their budgets.
HOST_RAM_FLOOR_BYTES = GIB
CASES = ("movement-red", "movement-blue", "visibility-red", "visibility-blue")
GENERATION = {"do_sample": True, "temperature": 0.7, "top_p": 0.8, "top_k": 20,
              "max_new_tokens": 1024, "use_cache": True}


def write(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n")
    temp.replace(path)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_cases(output):
    from wargame_native_engine.agents import ManualRealtimeClock, RealtimeAgentSession
    from wargame_native_engine.data import ScenarioBundle
    from wargame_native_engine.llm_input import get_llm_frame
    from wargame_native_engine.operations import ScenarioMatchFactory

    provenance = {}
    for label, fixture in (("movement", "examples/source_movement_scenario.json"),
                           ("visibility", "examples/demos/visibility_blocking/scenario.json")):
        bundle = ScenarioBundle.from_path(ROOT / fixture)
        factory = ScenarioMatchFactory(bundle=bundle, storage_root=output / "sessions" / label,
            red_controller_id="frame-debug-red", blue_controller_id="frame-debug-blue",
            decision_interval_ms=1000, frame_interval_ms=1000, record=False)
        session = RealtimeAgentSession(factory, run_namespace="task054-" + label,
            tick_interval_ms=1000, clock=ManualRealtimeClock())
        try:
            reset = session.reset(seed=42)
            for side in ("red", "blue"):
                case = output / (label + "-" + side)
                case.mkdir()
                frame = get_llm_frame(session, getattr(reset, side + "_binding"))
                write(case / "frame.json", frame)
                write(case / "messages.json", messages(frame))
                write(case / "tokens.json", {"input_tokens": None, "output_tokens": None,
                    "status": "NOT_RUN", "truncated": False, "input_limit": 8192, "max_new_tokens": 1024})
                write(case / "generation.json", {"status": "NOT_RUN", "reason": "INFERENCE_NOT_STARTED"})
                write(case / "input_manifest.json", {"mode": "FULL_SIDE_FRAME", "omitted_fields": [],
                    "retained_fields": ["$"], "frame_sha256": sha(case / "frame.json"),
                    "messages_sha256": sha(case / "messages.json"), "prompt_version": PROMPT_VERSION})
                native = frame["source_frame"]["extra"]["native"]
            assert session.pending_command_count == session.tick_count == 0
        finally:
            session.close()
        provenance[label] = {"fixture": fixture, "source_sha256": sha(ROOT / fixture),
            "scenario_sha256": bundle.scenario_identity.sha256, "map_sha256": bundle.map_identity.sha256,
            "seed": 42, "engine_version": native["engine_version"],
            "ruleset_id": native["ruleset_id"], "ruleset_version": native["ruleset_version"]}
    write(output / "fixtures.json", provenance)


def cache_root():
    return Path(os.environ.get("HF_HUB_CACHE", Path(os.environ.get("HF_HOME",
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "huggingface")) / "hub")).expanduser()


def verify_baseline_inputs(output, baseline):
    """Compare full side inputs before any worker; never send baseline scores."""
    rows = []
    for name in CASES:
        previous, current = baseline / name, output / name
        row = {"case": name, "baseline_frame": str(previous / "frame.json"),
               "frame_sha256": sha(current / "frame.json"), "same_input": False}
        try:
            old = json.loads((previous / "frame.json").read_text())
            new = json.loads((current / "frame.json").read_text())
            old_messages = json.loads((previous / "messages.json").read_text())
            row.update(baseline_frame_sha256=sha(previous / "frame.json"),
                baseline_messages_sha256=sha(previous / "messages.json"),
                canonical_frame_sha256=hashlib.sha256(compact(new).encode()).hexdigest(),
                baseline_canonical_frame_sha256=hashlib.sha256(compact(old).encode()).hexdigest(),
                same_input=compact(old) == compact(new) == compact(json.loads(old_messages[1]["content"])))
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        rows.append(row)
    verified = all(row["same_input"] for row in rows)
    write(output / "baseline_inputs.json", {"baseline_run": str(baseline),
        "status": "VERIFIED" if verified else "BLOCKED", "cases": rows})
    return verified


def existing_parent(path):
    while not path.exists():
        path = path.parent
    return path


def precheck(args):
    import torch
    from packaging.version import Version
    versions = {}
    for name in ("torch", "transformers", "accelerate", "safetensors", "huggingface-hub"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    cache = cache_root()
    target = Path(args.model_path).expanduser().resolve() if args.model_path else cache
    ram = next(int(line.split()[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines()
               if line.startswith("MemAvailable:"))
    evidence = {"python": sys.executable, "versions": versions, "torch_path": torch.__file__,
        "cuda_build": torch.version.cuda, "cuda_available": torch.cuda.is_available(),
        "available_ram_bytes": ram, "cache_target": str(target),
        "cache_free_bytes": shutil.disk_usage(existing_parent(target)).free,
        "inherited_limits": {name: list(resource.getrlimit(getattr(resource, name)))
            for name in ("RLIMIT_AS", "RLIMIT_RSS", "RLIMIT_DATA", "RLIMIT_MEMLOCK")},
        "model_requested": args.model_path or MODEL_ID}
    model_cache = cache / "models--Qwen--Qwen3-1.7B"
    evidence["cache_main_revision"] = ((model_cache / "refs/main").read_text().strip()
        if (model_cache / "refs/main").is_file() else None)
    failures = []
    if not evidence["cuda_available"]:
        failures.append("CUDA_UNAVAILABLE_IN_EXECUTOR")
    else:
        free, total = torch.cuda.mem_get_info(0)
        evidence.update(device=torch.cuda.get_device_name(0), available_vram_bytes=free,
                        total_vram_bytes=total, bf16_supported=torch.cuda.is_bf16_supported())
        if free < 5 * GIB:
            failures.append("VRAM_BELOW_5_GIB")
    if ram < 5 * GIB:
        failures.append("RAM_BELOW_5_GIB")
    if evidence["cache_free_bytes"] < 10 * GIB:
        failures.append("CACHE_DISK_BELOW_10_GIB")
    for name in ("RLIMIT_AS", "RLIMIT_DATA"):
        if evidence["inherited_limits"][name][0] != resource.RLIM_INFINITY:
            failures.append(name + "_MUST_BE_UNLIMITED_FOR_CUDA")
    if any(versions[k] is None for k in ("transformers", "accelerate", "safetensors", "huggingface-hub")):
        failures.append("DEPENDENCIES_MISSING_USE_HOST_SCRIPT")
    if versions["transformers"] and Version(versions["transformers"]) < Version("4.51.0"):
        failures.append("TRANSFORMERS_BELOW_4_51")
    evidence.update(failures=failures, status="READY" if not failures else "HOST_GPU_RUN_REQUIRED",
        inference_rss_limit_bytes=INFERENCE_RSS_BYTES, min_available_ram_bytes=HOST_RAM_FLOOR_BYTES,
        required_available_ram_bytes=5 * GIB)
    write(args.output / "precheck.json", evidence)


def prepare_model(args):
    from huggingface_hub import HfApi, snapshot_download
    started = monotonic()
    cache = cache_root().resolve()
    if cache.is_relative_to(ROOT):
        raise ValueError("model cache must be outside repository")
    selected_revision = args.revision
    downloaded = 0
    if args.model_path:
        model = Path(args.model_path).expanduser().resolve(strict=True)
        if model.is_relative_to(ROOT):
            raise ValueError("model weights must remain outside repository")
        selected_revision = args.revision  # local identity is content hashes, not a guessed HF commit
    else:
        # Local cache first. Probe only this model's ordinary HF ref/snapshot.
        ref = cache / "models--Qwen--Qwen3-1.7B" / "refs/main"
        if selected_revision is None and ref.is_file():
            selected_revision = ref.read_text().strip()
        try:
            model = Path(snapshot_download(MODEL_ID, revision=selected_revision or "main",
                cache_dir=str(cache), local_files_only=True, token=False))
            _model_files(model)  # incomplete snapshots are not cached success
        except (OSError, ValueError):
            if args.local_files_only:
                raise ValueError("local snapshot unavailable; downloads forbidden")
            info = HfApi(token=False).model_info(MODEL_ID, revision=selected_revision or "main", files_metadata=True)
            selected_revision = info.sha
            names = [f.rfilename for f in info.siblings if "/" not in f.rfilename and
                (f.rfilename.endswith(".safetensors") or f.rfilename in _CONFIG_FILES)]
            downloaded = sum(f.size or 0 for f in info.siblings if f.rfilename in names
                             and not (cache / "models--Qwen--Qwen3-1.7B" / "snapshots" / info.sha / f.rfilename).exists())
            model = Path(snapshot_download(MODEL_ID, revision=info.sha, cache_dir=str(cache),
                allow_patterns=names, max_workers=2, token=False))
        selected_revision = model.name
    files = _model_files(model)
    file_identity = {p.name: {"bytes": p.stat().st_size, "sha256": sha(p)} for p in files}
    manifest_evidence = None
    if args.model_manifest:
        previous = json.loads(args.model_manifest.read_text())
        if (previous["resolved_path"] != str(model) or previous["revision"] != selected_revision
                or previous["file_identity"] != file_identity):
            raise ValueError("cached model differs from existing model manifest")
        manifest_evidence = {"path": str(args.model_manifest.resolve()), "sha256": sha(args.model_manifest),
                             "status": "VERIFIED_FULL_CONTENT_HASHES"}
    print(f"Cache hit: {model}; files={len(files)}; manifest={manifest_evidence['status'] if manifest_evidence else 'NEW_IDENTITY'}", flush=True)
    write(args.output / "model.json", {"requested": args.model_path or MODEL_ID,
        "source_kind": "explicit_local_path" if args.model_path else "official_huggingface_repository",
        "resolved_path": str(model), "revision": selected_revision,
        "config_identity": json.loads((model / "config.json").read_text()),
        "file_identity": file_identity, "verified_against_manifest": manifest_evidence,
        "local_files_only": args.local_files_only or bool(args.model_path),
        "cache_bytes": sum(p.stat().st_size for p in files), "download_bytes_requested": downloaded,
        "download_size_semantics": "uncached selected file bytes from official metadata; wire/resume bytes not measured",
        "prepare_seconds": monotonic() - started})


_CONFIG_FILES = {"config.json", "generation_config.json", "tokenizer.json", "tokenizer_config.json",
    "special_tokens_map.json", "vocab.json", "merges.txt", "added_tokens.json",
    "chat_template.jinja", "model.safetensors.index.json"}


def _model_files(model):
    config = json.loads((model / "config.json").read_text())
    if config.get("quantization_config"):
        raise ValueError("quantized models are outside this experiment")
    weights = sorted(model.glob("*.safetensors"))
    if not weights or not (model / "tokenizer_config.json").is_file() or not (model / "tokenizer.json").is_file():
        raise ValueError("incomplete safetensors/tokenizer snapshot")
    index = model / "model.safetensors.index.json"
    if index.is_file():
        for name in set(json.loads(index.read_text())["weight_map"].values()):
            if Path(name).name != name or not (model / name).is_file():
                raise ValueError("missing/invalid weight shard")
    return sorted([p for p in model.iterdir() if p.is_file() and p.name in _CONFIG_FILES] + weights)


def phase(output, name, **extra):
    write(output / "phase.json", {"phase": name, "phase_started_monotonic_seconds": monotonic(), **extra})
    print(f"Stage: {name}" + (f" case={extra['case']}" if 'case' in extra else ""), flush=True)


def load_memory(output, boundary, torch):
    """Small worker-only boundary sample; external guard still owns aggregate RSS."""
    record = {"boundary": boundary, "pid": os.getpid(), "monotonic_seconds": monotonic(),
        "scope": "worker /proc/self/status; CUDA allocator on cuda:0",
        "cuda_allocated_bytes": torch.cuda.memory_allocated(0),
        "cuda_reserved_bytes": torch.cuda.memory_reserved(0)}
    try:
        status = Path("/proc/self/status").read_text().splitlines()
        record.update({key + "_bytes": next((int(line.split()[1]) * 1024 for line in status
            if line.startswith(key + ":")), None) for key in ("VmRSS", "RssAnon", "RssFile", "RssShmem")})
    except OSError as exc:
        record["proc_status_error"] = f"{type(exc).__name__}:{exc}"
    write(output / ("load_memory_" + boundary + ".json"), record)


def infer(args):
    # Transformers 5.17 core_model_loading defaults to an asynchronous thread
    # pool. Its supported flag makes materialization sequential. This is a
    # precaution, not evidence that parallel loading caused the old RSS peak.
    os.environ["HF_DEACTIVATE_ASYNC_LOAD"] = "1"
    os.environ["HF_ENABLE_PARALLEL_LOADING"] = "false"
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from packaging.version import Version
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA required; CPU fallback forbidden")
    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    torch.cuda.set_device(0)
    torch.cuda.set_per_process_memory_fraction(min(1., 6 * GIB / torch.cuda.get_device_properties(0).total_memory), 0)
    torch.set_grad_enabled(False)
    identity = json.loads((args.output / "model.json").read_text())
    model_path = identity["resolved_path"]
    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    version = importlib.metadata.version("transformers")
    dtype_key = "dtype" if Version(version) >= Version("5") else "torch_dtype"
    load_kwargs = {"local_files_only": True, "trust_remote_code": False,
        dtype_key: dtype, "device_map": {"": "cuda:0"}, "low_cpu_mem_usage": True}
    write(args.output / "load_request.json", {"model_path": model_path,
        "transformers_version": version, "kwargs": {**load_kwargs, dtype_key: str(dtype)},
        "low_cpu_mem_usage_semantics": "ignored_by_transformers_5" if Version(version) >= Version("5") else "requested",
        "loader_environment": {k: os.environ[k] for k in ("HF_DEACTIVATE_ASYNC_LOAD", "HF_ENABLE_PARALLEL_LOADING")}})
    started = monotonic()
    phase(args.output, "load")
    load_memory(args.output, "before", torch)
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True, trust_remote_code=False)
    model = AutoModelForCausalLM.from_pretrained(model_path, **load_kwargs)
    torch.cuda.synchronize()
    load_memory(args.output, "after", torch)
    model.eval()
    count = sum(p.numel() for p in model.parameters())
    if not 1_000_000_000 <= count <= 3_500_000_000:
        raise ValueError(f"model outside 1–3B class: {count} parameters")
    if any(p.device != torch.device("cuda:0") for p in model.parameters()):
        raise RuntimeError("model not entirely on cuda:0")
    parameter_dtypes = sorted({str(p.dtype) for p in model.parameters()})
    parameter_devices = sorted({str(p.device) for p in model.parameters()})
    if parameter_dtypes != [str(dtype)]:
        raise RuntimeError(f"unexpected parameter dtypes: {parameter_dtypes}; requested {dtype}")
    torch.cuda.synchronize()
    write(args.output / "load.json", {"load_seconds": monotonic() - started, "parameter_count": count,
        "precision": str(dtype), "device": "cuda:0", "parameter_dtypes": parameter_dtypes,
        "parameter_devices": parameter_devices, "model_load_count": 1, "training_updates": 0,
        "action_submissions": 0, "generation": GENERATION, "seed": 42, "prompt_version": PROMPT_VERSION,
        "chat_template_sha256": hashlib.sha256(str(tokenizer.chat_template).encode()).hexdigest()})
    calls = 0
    for name in CASES:
        case = args.output / name
        phase(args.output, "tokenize", case=name)
        conversation = json.loads((case / "messages.json").read_text())
        prompt = tokenizer.apply_chat_template(conversation, tokenize=False,
            add_generation_prompt=True, enable_thinking=False)
        (case / "prompt.txt").write_text(prompt)
        inputs = tokenizer(prompt, return_tensors="pt", truncation=False, add_special_tokens=False)
        tokens = inputs["input_ids"].shape[-1]
        write(case / "tokens.json", {"input_tokens": tokens, "input_limit": 8192, "truncated": False,
            "max_new_tokens": 1024, "prompt_sha256": sha(case / "prompt.txt")})
        if tokens > 8192:
            write(case / "generation.json", {"status": "NOT_RUN", "reason": "INPUT_OVER_8192_TOKENS"})
            del inputs
            continue
        inputs = inputs.to("cuda:0")
        torch.manual_seed(42)
        torch.cuda.reset_peak_memory_stats(0)
        calls += 1
        phase(args.output, "generate", case=name, call=calls)
        started = monotonic()
        with torch.inference_mode():
            output = model.generate(**inputs, **GENERATION, pad_token_id=tokenizer.eos_token_id)
        torch.cuda.synchronize()
        elapsed = monotonic() - started
        phase(args.output, "save", case=name, call=calls)
        completion = output[0, tokens:].tolist()
        raw = tokenizer.decode(completion, skip_special_tokens=True)
        (case / "response.txt").write_text(raw)  # save before parsing, including failures
        write(case / "output_token_ids.json", completion)
        write(case / "generation.json", {"status": "GENERATED", "seconds": elapsed,
            "input_tokens": tokens, "output_tokens": len(completion), "call": calls,
            "hit_token_limit": len(completion) >= 1024, "allocated_bytes": torch.cuda.memory_allocated(0),
            "reserved_bytes": torch.cuda.memory_reserved(0), "peak_allocated_bytes": torch.cuda.max_memory_allocated(0),
            "peak_reserved_bytes": torch.cuda.max_memory_reserved(0)})
        del inputs, output, completion
        torch.cuda.empty_cache()
    phase(args.output, "complete", actual_calls=calls)


def summarize(output, status, guards):
    rows, correct, total, valid, structured, passed, calls = [], 0, 0, 0, 0, 0, 0
    for name in CASES:
        case = output / name
        frame = json.loads((case / "frame.json").read_text())
        # This scoring path executes only after all inference has ended.
        expected = expected_readback(frame)
        write(case / "expected.json", expected)
        # A saved response, even an empty one or one saved just before worker
        # interruption, is observed model output. Missing output is not bad JSON.
        generated = (case / "response.txt").is_file()
        if generated:
            checked = check_readback((case / "response.txt").read_text(), frame)
        else:
            checked = {"status": "NOT_RUN", "json_valid": None, "structure_valid": None,
                "pass": None, "parsed": None, "correct_fields": None,
                "total_fields": len(_leaves(expected)), "mismatches": [], "structure_errors": [],
                "parse_error": None, "reason": "NO_RESPONSE_GENERATED"}
        write(case / "check.json", checked)
        row = {"case": name, "status": ("PASS" if checked["pass"] else "FAIL") if generated else "NOT_RUN",
            "json_valid": checked["json_valid"], "structure_valid": checked["structure_valid"],
            "correct_fields": checked["correct_fields"],
            "total_fields": checked["total_fields"], "mismatches": checked["mismatches"],
            "structure_errors": checked["structure_errors"], "parse_error": checked.get("parse_error")}
        rows.append(row)
        correct += checked["correct_fields"] or 0
        total += checked["total_fields"]
        valid += int(bool(checked["json_valid"]))
        structured += int(bool(checked["structure_valid"]))
        passed += int(bool(checked["pass"]))
        calls += int(generated)
    load = json.loads((output / "load.json").read_text()) if (output / "load.json").exists() else None
    guard_ok = all(g["reason"] == "worker_exit" and g.get("returncode") == 0
                   and g.get("cleanup_status") == "PASS" for g in guards)
    phase_path = output / "phase.json"
    last_phase = json.loads(phase_path.read_text()) if phase_path.exists() else {}
    summary = {"implementation_status": "IMPLEMENTED_OFFLINE_CHECKS_SEPARATE",
        "inference_status": status if status != "COMPLETED" else ("CUDA_GENERATED" if calls else "CUDA_LOADED_NO_COMPLETIONS"),
        "run_status": status, "readback_status": "PASS" if passed == 4 and guard_ok else "PARTIAL" if calls else "NOT_RUN",
        "planned_cases": 4, "observed_cases": calls, "passed_cases": passed if calls else None,
        "json_valid_cases": valid if calls else None, "json_valid_rate": valid / 4 if calls else None,
        "structure_valid_cases": structured if calls else None,
        "structure_valid_rate": structured / 4 if calls else None,
        "fully_passed_rate": passed / 4 if calls else None,
        "correct_fields": correct if calls else None, "total_fields": total,
        "field_accuracy": correct / total if calls else None, "actual_completed_calls": calls,
        "quality_denominator": "all planned cases/expected fields; null when no response observed",
        "response_status": "已生成回答" if calls else "未生成回答",
        "attempted_calls": max([calls, last_phase.get("call", 0)]
            + [json.loads((output / n / "generation.json").read_text()).get("call", 0)
               for n in CASES if (output / n / "generation.json").exists()]),
        "model_load": load, "training_updates": 0, "action_submissions": 0,
        "prompt_version": PROMPT_VERSION, "guards_pass": guard_ok, "guards": guards, "cases": rows}
    write(output / "summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model-path")
    parser.add_argument("--revision")
    parser.add_argument("--local-files-only", action="store_true", help="forbid model downloads")
    parser.add_argument("--model-manifest", type=Path, help="verify local snapshot against an existing model.json")
    parser.add_argument("--baseline-run", type=Path, help="stop before workers if any original side input differs")
    parser.add_argument("--prepare-seconds", type=float, default=600,
                        help="remaining preparation budget; R1 cached host entry uses 60 seconds")
    parser.add_argument("--stage", choices=("precheck", "prepare", "infer"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 14 <= args.prepare_seconds <= 600:
        parser.error("prepare-seconds must be between 14 and 600")
    args.output = args.output.resolve()
    if args.stage:
        {"precheck": precheck, "prepare": prepare_model, "infer": infer}[args.stage](args)
        return 0
    if not (args.output.is_relative_to(ROOT / "local_data") or args.output.is_relative_to(Path("/tmp"))):
        parser.error("output must be under local_data/ or /tmp")
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] = "30"
    os.environ["HF_HUB_ETAG_TIMEOUT"] = "15"
    write(args.output / "code_identity.json", {name: sha(ROOT / name) for name in (
        "src/wargame_native_engine/llm_input/__init__.py",
        "src/wargame_native_engine/llm_input/readback.py",
        "src/wargame_native_engine/llm_input/local_debug.py",
        "scripts/run_llm_frame_debug.py", "scripts/benchmark_continuous_compare.py",
        "scripts/diagnose_realtime_resources.py", "scripts/task054-host-verify.sh")})
    read_cases(args.output)
    if args.baseline_run and not verify_baseline_inputs(args.output, args.baseline_run.resolve()):
        summarize(args.output, "BASELINE_INPUT_BLOCKED", [])
        print(f"Baseline input mismatch/missing evidence; stopped before loading. See {args.output / 'baseline_inputs.json'}", flush=True)
        return 2
    from scripts.benchmark_continuous_compare import monitor
    guards = []
    status = "HOST_GPU_RUN_REQUIRED"
    for stage, cap in (("precheck", 60), ("prepare", args.prepare_seconds), ("infer", 600)):
        print(f"Stage: {stage}; interpreter={sys.executable}; model={args.model_path or MODEL_ID}", flush=True)
        stage_out = args.output / stage
        stage_out.mkdir()
        command = [sys.executable, "-m", "scripts.run_llm_frame_debug", "--stage", stage,
                   "--output", str(args.output)]
        for option in ("model_path", "revision", "model_manifest"):
            if getattr(args, option):
                command.extend(["--" + option.replace("_", "-"), str(getattr(args, option))])
        if args.local_files_only:
            command.append("--local-files-only")
        # Phase file belongs to this worker; monitor reads it in its output dir.
        (stage_out / "phase.json").symlink_to(args.output / "phase.json")
        guard = monitor(command, ROOT, stage_out, args.output, cap, INFERENCE_RSS_BYTES if stage == "infer" else 4 * GIB,
            resource_profile="gpu", storage_limit_bytes=64 * 1024 ** 2,
            phase_limits={"generate": 120} if stage == "infer" else None,
            min_available_ram_bytes=HOST_RAM_FLOOR_BYTES, report_phases=True)
        guards.append(guard)
        print(f"Stage: {stage} finished; reason={guard['reason']}; returncode={guard.get('returncode')}; "
              f"log={stage_out / 'worker.log'}", flush=True)
        if guard["reason"] != "worker_exit" or guard.get("returncode") != 0 or guard.get("cleanup_status") != "PASS":
            status = "FAILED_" + stage.upper()
            break
        if stage == "precheck":
            checked = json.loads((args.output / "precheck.json").read_text())
            if checked["status"] != "READY":
                print(f"Precheck blocked: {checked.get('failures', [])}", flush=True)
                break
        if stage == "prepare":
            print(f"Cache hit and content identity recorded: {args.output / 'model.json'}", flush=True)
        if stage == "infer":
            status = "COMPLETED"
    summary = summarize(args.output, status, guards)
    for row in summary["cases"]:
        print(f"Readback: {row['case']} {row['status']}; JSON={row['json_valid']}; "
              f"structure={row['structure_valid']}; fields={row['correct_fields']}/{row['total_fields']}; "
              f"response={args.output / row['case'] / 'response.txt'}; "
              f"diff={args.output / row['case'] / 'check.json'}", flush=True)
    print(json.dumps({"output": str(args.output), "inference_status": summary["inference_status"],
                      "readback_status": summary["readback_status"],
                      "response_status": summary["response_status"], "passed_cases": summary["passed_cases"],
                      "correct_fields": summary["correct_fields"], "total_fields": summary["total_fields"],
                      "summary": str(args.output / "summary.json")}, ensure_ascii=False), flush=True)
    return 0 if summary["readback_status"] == "PASS" else 2
