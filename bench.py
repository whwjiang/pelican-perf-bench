#!/usr/bin/env python3
"""Benchmark Pelican object transfers via the Pelican CLI."""

from __future__ import annotations

import argparse
import json
import math
import os
import posixpath
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


DEFAULT_FILE_SIZES = "4KB,64KB,1MB,10MB,100MB"
DEFAULT_CLIENTS = "1,2,4,8,16,32"
FILE_WRITE_CHUNK_SIZE = 1024 * 1024


@dataclass
class Config:
    federation: str
    prefix: str
    token_file: Path
    pelican_bin: str
    operation: str
    file_sizes: list[int]
    clients: list[int]
    duration: float
    warmup: float
    setup: bool
    teardown: bool
    output: str
    label: str
    workdir: Path | None
    client_flags: list[str]


@dataclass
class Sample:
    latency_ms: float
    bytes_transferred: int
    error: str | None


def parse_args(argv: Sequence[str]) -> Config:
    parser = argparse.ArgumentParser(
        description="Benchmark Pelican transfers over time windows using the Pelican CLI."
    )
    parser.add_argument("--federation", required=True, help="Federation host[:port] for pelican:// URLs")
    parser.add_argument("--prefix", required=True, help="Namespace prefix, e.g. /bench")
    parser.add_argument("--token-file", required=True, help="Token file passed to pelican object commands")
    parser.add_argument("--pelican-bin", default=os.environ.get("PELICAN_BIN", "pelican"))
    parser.add_argument("--operation", choices=("read", "write", "both"), default="both")
    parser.add_argument("--file-sizes", default=DEFAULT_FILE_SIZES)
    parser.add_argument("--clients", default=DEFAULT_CLIENTS)
    parser.add_argument("--duration", default="30s", help="Measurement window per cell")
    parser.add_argument("--warmup", default="5s", help="Discarded warmup window before each cell")
    parser.add_argument("--setup", dest="setup", action="store_true", default=True)
    parser.add_argument("--no-setup", dest="setup", action="store_false")
    parser.add_argument("--teardown", action="store_true", default=False)
    parser.add_argument("--output", default="-", help="NDJSON output path")
    parser.add_argument("--label", default="", help="Freeform label attached to results")
    parser.add_argument("--workdir", help="Scratch directory for local sources/downloads")
    parser.add_argument(
        "--client-flag",
        dest="client_flags",
        action="append",
        default=[],
        help="Extra flag forwarded to pelican object commands; may be repeated",
    )

    args = parser.parse_args(argv)
    token_file = Path(args.token_file)
    if not token_file.is_file():
        parser.error(f"token file not found: {token_file}")

    return Config(
        federation=args.federation,
        prefix=normalize_prefix(args.prefix),
        token_file=token_file,
        pelican_bin=resolve_pelican_bin(args.pelican_bin),
        operation=args.operation,
        file_sizes=parse_sizes(args.file_sizes),
        clients=parse_ints(args.clients, "client count"),
        duration=parse_duration(args.duration),
        warmup=parse_duration(args.warmup),
        setup=args.setup,
        teardown=args.teardown,
        output=args.output,
        label=args.label,
        workdir=Path(args.workdir) if args.workdir else None,
        client_flags=args.client_flags,
    )


def resolve_pelican_bin(value: str) -> str:
    candidate = Path(value)
    if candidate.is_file():
        return str(candidate.resolve())
    resolved = shutil.which(value)
    if resolved:
        return resolved
    raise SystemExit(f"pelican binary not found: {value}")


def normalize_prefix(prefix: str) -> str:
    prefix = prefix.strip()
    if not prefix:
        raise SystemExit("--prefix must not be empty")
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    return prefix.rstrip("/") or "/"


def parse_sizes(value: str) -> list[int]:
    sizes = [parse_size(part.strip()) for part in value.split(",") if part.strip()]
    if not sizes:
        raise SystemExit("no file sizes provided")
    return sizes


def parse_size(value: str) -> int:
    raw = value.strip().upper()
    suffixes = {"GB": 1024**3, "MB": 1024**2, "KB": 1024, "B": 1}
    multiplier = 1
    for suffix, factor in suffixes.items():
        if raw.endswith(suffix):
            multiplier = factor
            raw = raw[: -len(suffix)]
            break
    try:
        size = int(raw.strip()) * multiplier
    except ValueError as exc:
        raise SystemExit(f"invalid size {value!r}") from exc
    if size <= 0:
        raise SystemExit(f"size must be positive: {value}")
    return size


def parse_ints(value: str, label: str) -> list[int]:
    items: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            item = int(part)
        except ValueError as exc:
            raise SystemExit(f"invalid {label}: {part!r}") from exc
        if item <= 0:
            raise SystemExit(f"{label} must be positive: {part!r}")
        items.append(item)
    if not items:
        raise SystemExit(f"no {label}s provided")
    return items


def parse_duration(value: str) -> float:
    value = value.strip().lower()
    suffixes = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    for suffix, factor in suffixes.items():
        if value.endswith(suffix):
            amount = float(value[: -len(suffix)])
            if amount < 0:
                raise SystemExit(f"duration must be non-negative: {value}")
            return amount * factor
    amount = float(value)
    if amount < 0:
        raise SystemExit(f"duration must be non-negative: {value}")
    return amount


def human_bytes(size: int) -> str:
    if size >= 1 << 30:
        return f"{size // (1 << 30)}GB"
    if size >= 1 << 20:
        return f"{size // (1 << 20)}MB"
    if size >= 1 << 10:
        return f"{size // (1 << 10)}KB"
    return f"{size}B"


def materialize_file(path: Path, size: int) -> None:
    remaining = size
    chunk = b"a" * min(FILE_WRITE_CHUNK_SIZE, size)
    with path.open("wb") as handle:
        while remaining > 0:
            to_write = min(len(chunk), remaining)
            handle.write(chunk[:to_write])
            remaining -= to_write


def operation_list(operation: str) -> list[str]:
    return ["read", "write"] if operation == "both" else [operation]


class BenchContext:
    def __init__(self, cfg: Config, root: Path) -> None:
        self.cfg = cfg
        self.root = root
        self.sources = root / "sources"
        self.downloads = root / "downloads"
        self.sources.mkdir(parents=True, exist_ok=True)
        self.downloads.mkdir(parents=True, exist_ok=True)

    def local_source(self, size: int) -> Path:
        path = self.sources / f"size-{size}.bin"
        if not path.exists() or path.stat().st_size != size:
            materialize_file(path, size)
        return path

    def local_download(self, worker_id: int, size: int) -> Path:
        return self.downloads / f"worker-{worker_id}-size-{size}.bin"

    def remote_url(self, *parts: str) -> str:
        segments: list[str] = []
        prefix = self.cfg.prefix.strip("/")
        if prefix:
            segments.append(prefix)
        segments.extend(part.strip("/") for part in parts if part.strip("/"))
        joined = posixpath.join(*segments) if segments else ""
        base = f"pelican://{self.cfg.federation}"
        return f"{base}/{joined}" if joined else f"{base}/"

    def read_url(self, size: int) -> str:
        return self.remote_url("reads", f"size-{size}.bin")

    def write_url(self, worker_id: int) -> str:
        return self.remote_url("writes", f"worker-{worker_id}.bin")


def run_command(cmd: Sequence[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(stderr or f"command failed with exit code {proc.returncode}")


def run_command_capture(cmd: Sequence[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(stderr or f"command failed with exit code {proc.returncode}")
    return proc.stdout


def pelican_command(cfg: Config, verb: str) -> list[str]:
    return [cfg.pelican_bin, "object", verb, "-t", str(cfg.token_file), *cfg.client_flags]


def pelican_put_command(cfg: Config, source: str, destination: str) -> list[str]:
    return [
        cfg.pelican_bin,
        "object",
        "put",
        *cfg.client_flags,
        source,
        destination,
        "--token",
        str(cfg.token_file),
    ]


def pelican_ls_command(cfg: Config, target: str) -> list[str]:
    return [
        cfg.pelican_bin,
        "object",
        "ls",
        "--json",
        target,
        "--token",
        str(cfg.token_file),
        *cfg.client_flags,
    ]


def remote_existing_names(ctx: BenchContext, target: str) -> set[str]:
    try:
        output = run_command_capture(pelican_ls_command(ctx.cfg, target))
    except RuntimeError as exc:
        print(f"[setup] warning: failed to list {target}: {exc}", file=sys.stderr)
        return set()

    payload = output.strip()
    if not payload:
        return set()

    try:
        entries = json.loads(payload)
    except json.JSONDecodeError as exc:
        print(f"[setup] warning: failed to parse listing for {target}: {exc}", file=sys.stderr)
        return set()

    if not isinstance(entries, list):
        print(f"[setup] warning: unexpected listing format for {target}", file=sys.stderr)
        return set()

    return {entry for entry in entries if isinstance(entry, str)}


def stage_reads(ctx: BenchContext, sizes: Sequence[int]) -> None:
    reads_url = ctx.remote_url("reads")
    existing = remote_existing_names(ctx, reads_url)
    for size in sizes:
        local_path = ctx.local_source(size)
        url = ctx.read_url(size)
        object_name = f"size-{size}.bin"
        if object_name in existing:
            print(f"[setup]   {url} ({human_bytes(size)}) already present", file=sys.stderr)
            continue
        print(f"[setup]   {url} ({human_bytes(size)})", file=sys.stderr)
        run_command(pelican_put_command(ctx.cfg, str(local_path), url))


def delete_remote(ctx: BenchContext, url: str) -> None:
    proc = subprocess.run(
        [ctx.cfg.pelican_bin, "object", "delete", "-t", str(ctx.cfg.token_file), *ctx.cfg.client_flags, url],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        msg = proc.stderr.strip() or proc.stdout.strip()
        print(f"[teardown] warning: failed to delete {url}: {msg}", file=sys.stderr)


def cleanup_remote(ctx: BenchContext, operations: Sequence[str], max_clients: int) -> None:
    if "read" in operations:
        for size in ctx.cfg.file_sizes:
            delete_remote(ctx, ctx.read_url(size))
    if "write" in operations:
        for worker_id in range(max_clients):
            delete_remote(ctx, ctx.write_url(worker_id))


def run_transfer(command: list[str], success_bytes: int) -> Sample:
    start = time.perf_counter()
    proc = subprocess.run(command, capture_output=True, text=True)
    latency_ms = (time.perf_counter() - start) * 1000.0
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip()
        return Sample(latency_ms=latency_ms, bytes_transferred=0, error=stderr or f"exit {proc.returncode}")
    return Sample(latency_ms=latency_ms, bytes_transferred=success_bytes, error=None)


def worker_loop(
    deadline: float,
    transfer: Callable[[int], Sample],
    worker_id: int,
    output: list[Sample] | None,
    lock: threading.Lock | None,
) -> None:
    while time.monotonic() < deadline:
        sample = transfer(worker_id)
        if output is not None and lock is not None:
            with lock:
                output.append(sample)


def run_phase(
    num_clients: int,
    duration_s: float,
    transfer: Callable[[int], Sample],
    collect: bool,
) -> tuple[list[Sample], float]:
    deadline = time.monotonic() + duration_s
    samples: list[Sample] = []
    lock = threading.Lock() if collect else None
    start = time.perf_counter()
    threads = [
        threading.Thread(
            target=worker_loop,
            args=(deadline, transfer, worker_id, samples if collect else None, lock),
            daemon=False,
        )
        for worker_id in range(num_clients)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return samples, time.perf_counter() - start


def percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def summarize_samples(
    cfg: Config,
    operation: str,
    size: int,
    num_clients: int,
    elapsed: float,
    samples: Sequence[Sample],
) -> dict[str, object]:
    successes = [sample for sample in samples if sample.error is None]
    latencies = [sample.latency_ms for sample in successes]
    total_bytes = sum(sample.bytes_transferred for sample in successes)
    throughput_mbps = (total_bytes * 8.0) / (1_000_000.0 * elapsed) if elapsed > 0 else 0.0

    return {
        "label": cfg.label or None,
        "federation": cfg.federation,
        "prefix": cfg.prefix,
        "operation": operation,
        "file_size_bytes": size,
        "num_clients": num_clients,
        "sample_count": len(successes),
        "error_count": len(samples) - len(successes),
        "total_bytes": total_bytes,
        "cell_duration_s": elapsed,
        "throughput_mbps": throughput_mbps,
        "latency_median_ms": statistics.median(latencies) if latencies else None,
        "latency_p99_ms": percentile(latencies, 0.99),
        "latency_min_ms": min(latencies) if latencies else None,
        "latency_max_ms": max(latencies) if latencies else None,
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def write_result(handle, result: dict[str, object]) -> None:
    payload = {key: value for key, value in result.items() if value is not None}
    handle.write(json.dumps(payload) + "\n")
    handle.flush()


def transfer_factory(ctx: BenchContext, operation: str, size: int) -> Callable[[int], Sample]:
    if operation == "read":

        def transfer(worker_id: int) -> Sample:
            destination = ctx.local_download(worker_id, size)
            cmd = [*pelican_command(ctx.cfg, "get"), "--inplace", ctx.read_url(size), str(destination)]
            return run_transfer(cmd, size)

        return transfer

    if operation == "write":
        source = ctx.local_source(size)

        def transfer(worker_id: int) -> Sample:
            cmd = pelican_put_command(ctx.cfg, str(source), ctx.write_url(worker_id))
            return run_transfer(cmd, size)

        return transfer

    raise ValueError(f"unsupported operation: {operation}")


def open_output(path: str):
    if path in ("", "-"):
        return sys.stdout
    return open(path, "w", encoding="utf-8")


def main(argv: Sequence[str]) -> int:
    cfg = parse_args(argv)
    print(f"[config] pelican_bin={cfg.pelican_bin}", file=sys.stderr)
    tmp_ctx = tempfile.TemporaryDirectory(prefix="pelican-perf-bench-") if cfg.workdir is None else None
    workdir = Path(tmp_ctx.name) if tmp_ctx is not None else cfg.workdir
    assert workdir is not None
    workdir.mkdir(parents=True, exist_ok=True)
    ctx = BenchContext(cfg, workdir)
    operations = operation_list(cfg.operation)

    output_handle = open_output(cfg.output)
    close_output = output_handle is not sys.stdout
    try:
        if cfg.setup and "read" in operations:
            print("[setup] staging read files", file=sys.stderr)
            stage_reads(ctx, cfg.file_sizes)

        for operation in operations:
            for size in cfg.file_sizes:
                transfer = transfer_factory(ctx, operation, size)
                for num_clients in cfg.clients:
                    print(f"[run] op={operation} size={human_bytes(size)} clients={num_clients}", file=sys.stderr)
                    if cfg.warmup > 0:
                        run_phase(num_clients, cfg.warmup, transfer, collect=False)
                    samples, elapsed = run_phase(num_clients, cfg.duration, transfer, collect=True)
                    result = summarize_samples(cfg, operation, size, num_clients, elapsed, samples)
                    median_ms = result.get("latency_median_ms")
                    p99_ms = result.get("latency_p99_ms")
                    median_str = f"{median_ms:.1f}" if isinstance(median_ms, (int, float)) else "n/a"
                    p99_str = f"{p99_ms:.1f}" if isinstance(p99_ms, (int, float)) else "n/a"
                    print(
                        "[run]   -> "
                        f"{result['throughput_mbps']:.1f} Mbps "
                        f"(median {median_str} ms, p99 {p99_str} ms, "
                        f"{result['sample_count']} samples, {result['error_count']} errors)",
                        file=sys.stderr,
                    )
                    write_result(output_handle, result)

        if cfg.teardown:
            print("[teardown] removing staged files", file=sys.stderr)
            cleanup_remote(ctx, operations, max(cfg.clients))
    finally:
        if close_output:
            output_handle.close()
        if tmp_ctx is not None:
            tmp_ctx.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
