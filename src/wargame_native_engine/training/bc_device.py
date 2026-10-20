"""Explicit device setup for bounded BC and optimizer-free argmax evaluation.

CPU initialization and generators remain authoritative. This does not enable
CUDA PPO updates, sampling, parallel training, or joint policies.
"""
from __future__ import annotations

import os
import resource
import sys
from pathlib import Path

from wargame_native_engine.training.learner_backend import require_pytorch


class CUDAUnavailableError(RuntimeError):
    """The requested CUDA device is inaccessible in this environment."""


def configure_device(device="cpu"):
    torch = require_pytorch()
    target = torch.device(device)
    if str(target) not in {"cpu", "cuda:0"}:
        raise ValueError("BC device must be cpu or cuda:0")
    if target.type == "cuda":
        # Must precede the first CUDA context/BLAS operation in this worker.
        os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
        if not torch.cuda.is_available():
            raise CUDAUnavailableError("CUDA unavailable in this execution environment; no fallback")
        torch.cuda.set_device(target)
        free, total = torch.cuda.mem_get_info(target)
        if free < 2 * 1024**3:
            raise RuntimeError("requires at least 2 GiB available CUDA memory")
        torch.cuda.set_per_process_memory_fraction(1024**3 / total, target)
        torch.use_deterministic_algorithms(True)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
    return target


def device_evidence(device, model=None):
    torch = require_pytorch()
    target = torch.device(device)
    result = {
        "requested_device": str(target), "torch_version": str(torch.__version__),
        "torch_path": torch.__file__, "cuda_build": torch.version.cuda,
        "python": sys.executable, "pid": os.getpid(),
        "rlimit_as": list(resource.getrlimit(resource.RLIMIT_AS)),
        "deterministic_algorithms": torch.are_deterministic_algorithms_enabled(),
        "cublas_workspace_config": os.environ.get("CUBLAS_WORKSPACE_CONFIG"),
        "torch_cpu_threads": torch.get_num_threads(),
        "torch_interop_threads": torch.get_num_interop_threads(),
        "proc_status": {line.split(":")[0]: line.split(":", 1)[1].strip()
                        for line in Path("/proc/self/status").read_text().splitlines()
                        if line.startswith(("VmSize:", "VmPeak:", "VmRSS:", "VmHWM:", "Threads:"))},
    }
    if model is not None:
        result["model_devices"] = sorted({str(p.device) for p in model.parameters()})
        result["model_dtypes"] = sorted({str(p.dtype) for p in model.parameters()})
    if target.type == "cuda":
        torch.cuda.synchronize(target)
        free, total = torch.cuda.mem_get_info(target)
        result.update(device_name=torch.cuda.get_device_name(target), free_bytes=free,
                      total_bytes=total, allocated_bytes=torch.cuda.memory_allocated(target),
                      reserved_bytes=torch.cuda.memory_reserved(target),
                      max_allocated_bytes=torch.cuda.max_memory_allocated(target),
                      max_reserved_bytes=torch.cuda.max_memory_reserved(target),
                      allocator_limit_bytes=1024**3, allocator_limit_scope="Torch allocator only; excludes driver/context",
                      allow_tf32_matmul=torch.backends.cuda.matmul.allow_tf32,
                      allow_tf32_cudnn=torch.backends.cudnn.allow_tf32)
    return result


def main():
    """Small real forward/backward precheck; run only in a monitored child."""
    import argparse
    import json
    p = argparse.ArgumentParser()
    p.add_argument("--device", required=True)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--run-id", default=None)
    args = p.parse_args()
    torch = require_pytorch()
    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    torch.manual_seed(937000)
    stat = Path('/proc/self/stat').read_text()
    result = {"status": "PRECHECK_DEVICE_FAILED", "python": sys.executable,
              "pid": os.getpid(), "start_ticks": int(stat[stat.rfind(')') + 2:].split()[19]),
              "run_id": args.run_id, "requested_device": args.device,
              "torch_version": str(torch.__version__), "torch_path": torch.__file__,
              "seed": 937000,
              "rlimit_as": list(resource.getrlimit(resource.RLIMIT_AS))}
    try:
        target = configure_device(args.device)
        generator = torch.Generator(device="cpu").manual_seed(937000)
        model = torch.nn.Linear(4, 2).to(target)
        x = torch.randn((2, 4), generator=generator).to(target)
        loss = model(x).square().mean()
        loss.backward()
        assert bool(torch.isfinite(loss)) and all(bool(torch.isfinite(p.grad).all()) for p in model.parameters())
        result.update(device_evidence(target, model), input_device=str(x.device),
                      loss_device=str(loss.device), gradient_devices=sorted({str(p.grad.device) for p in model.parameters()}), status="PASS")
    except Exception as exc:
        result.update(error_type=type(exc).__name__, error=str(exc))
        if isinstance(exc, CUDAUnavailableError):
            result.update(status="HOST_GPU_RUN_REQUIRED", error_code="CUDA_UNAVAILABLE")
    finally:
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
