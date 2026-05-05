# pelican-perf-bench

Standalone benchmark tool that measures read and write performance through the
Pelican client CLI. It runs each `(operation, file size, client count)` cell for
fixed warmup and measurement windows, then emits NDJSON with both throughput and
latency statistics.

## Requirements

- Python 3.11+
- A Pelican client binary, either on `PATH` or passed via `--pelican-bin`
- A token file with access to the target namespace

The repo wrapper [testing/scripts/run_perf_bench.sh](/workspaces/pelican/testing/scripts/run_perf_bench.sh)
automatically falls back to the bundled
[testing/pelican-7.18.0/pelican](/workspaces/pelican/testing/pelican-7.18.0/pelican)
binary when `pelican` is not installed globally.

## Running

Point the benchmark at a federation endpoint and namespace prefix. Transfers are
performed with `pelican object get` and `pelican object put`, not direct origin
HTTP requests.

```sh
testing/scripts/run_perf_bench.sh \
    --federation localhost:8443 \
    --prefix /bench \
    --token-file /path/to/token \
    --label posixv2-local \
    --duration 30s \
    --output results-posixv2.ndjson
```

Restrict the matrix with `--file-sizes` and `--clients`:

```sh
testing/scripts/run_perf_bench.sh \
    --federation osg-htc.org \
    --prefix /globus/bench \
    --token-file ./globus.token \
    --file-sizes 1MB,100MB,1GB \
    --clients 1,4,16 \
    --operation read \
    --output results-globus.ndjson
```

## Flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--federation` | required | Federation host[:port] used in `pelican://...` URLs |
| `--prefix` | required | Namespace prefix, e.g. `/bench` |
| `--token-file` | required | Token file passed to Pelican CLI transfers |
| `--pelican-bin` | `pelican` | Pelican client binary path |
| `--operation` | `both` | `read`, `write`, or `both` |
| `--file-sizes` | `4KB,64KB,1MB,10MB,100MB` | Comma-separated sizes |
| `--clients` | `1,2,4,8,16,32` | Comma-separated concurrency levels |
| `--duration` | `30s` | Measurement window per cell |
| `--warmup` | `5s` | Discarded warmup window before each cell |
| `--setup` / `--no-setup` | `--setup` | Stage read files before the matrix |
| `--teardown` | `false` | Delete staged files after the matrix |
| `--output` | stdout | NDJSON output path |
| `--label` | — | Freeform label attached to every record |
| `--workdir` | tempfile | Scratch directory for local sources/downloads |
| `--client-flag` | — | Extra flag forwarded to Pelican CLI transfers |

## Result format

One JSON object per cell, for example:

```json
{
  "label": "posixv2-local",
  "federation": "localhost:8443",
  "prefix": "/bench",
  "operation": "read",
  "file_size_bytes": 1048576,
  "num_clients": 8,
  "sample_count": 512,
  "error_count": 0,
  "total_bytes": 536870912,
  "cell_duration_s": 30.08,
  "throughput_mbps": 142.8,
  "latency_median_ms": 421.7,
  "latency_p99_ms": 613.4,
  "ts": "2026-05-04T00:00:00Z"
}
```

- `throughput_mbps` is aggregate throughput across all successful transfers in the cell.
- `latency_median_ms` and `latency_p99_ms` are per-transfer wall-clock latencies
  measured around each `pelican object get` or `pelican object put` invocation.

## How It Works

- Read setup first runs `pelican object ls --json` on `{prefix}/reads` and only stages missing objects at `{prefix}/reads/size-{N}.bin`.
- Read workers repeatedly run `pelican object get pelican://{federation}/...`.
- Write workers repeatedly run `pelican object put <local-file> pelican://{federation}/...`.
- Local source files are materialized on disk in 1 MiB zero-filled chunks, not
  created as sparse files.
- Each worker loops until the cell deadline, so results are time-windowed instead
  of single-shot.
- Latency is measured per transfer, while throughput is computed over the full cell.
- Very large objects such as `1GB` are best treated as opt-in sizes with a longer
  `--duration` so each cell captures multiple transfers instead of one long upload.

## Staging An Origin

[perf-bench/scripts/start-posixv2-origin.sh](/workspaces/pelican/perf-bench/scripts/start-posixv2-origin.sh)
starts a local origin and prints the corresponding benchmark arguments.

## Notes

- This benchmark measures the end-to-end Pelican client path, including discovery
  and client-side transfer overhead, not raw direct-origin HTTP only.
- Reads now write to a local scratch directory because the benchmark exercises the
  real CLI path rather than an in-process HTTP client.
- Run the client on a separate host from the origin when you care about deployment-realistic numbers.
