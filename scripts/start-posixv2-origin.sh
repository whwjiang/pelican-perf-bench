#!/usr/bin/env bash
# Start a local Pelican origin with the POSIXv2 backend for benchmarking.
# Prints the federation endpoint and a bearer token file on stdout; writes logs to a tempfile.
#
# Requires `pelican` on PATH. Backend can be switched by setting STORAGE_TYPE
# to "posix" instead (which uses the XRootD-based POSIX backend).
set -euo pipefail

STORAGE_TYPE="${STORAGE_TYPE:-posixv2}"
PREFIX="${PREFIX:-/bench}"
PORT="${PORT:-8443}"

WORKDIR="$(mktemp -d -t perf-bench-origin-XXXXXX)"
STORAGE_DIR="${WORKDIR}/storage"
CONFIG_FILE="${WORKDIR}/pelican.yaml"
LOG_FILE="${WORKDIR}/origin.log"

mkdir -p "${STORAGE_DIR}"

cat > "${CONFIG_FILE}" <<EOF
Origin:
  StorageType: ${STORAGE_TYPE}
  Exports:
    - FederationPrefix: ${PREFIX}
      StoragePrefix: ${STORAGE_DIR}
      Capabilities: ["Reads", "Writes", "Listings", "DirectReads"]
Server:
  WebPort: ${PORT}
EOF

echo "[start-posixv2-origin] workdir:    ${WORKDIR}" >&2
echo "[start-posixv2-origin] storage:    ${STORAGE_DIR}" >&2
echo "[start-posixv2-origin] config:     ${CONFIG_FILE}" >&2
echo "[start-posixv2-origin] log:        ${LOG_FILE}" >&2

pelican origin serve -f "https://localhost:${PORT}" --config "${CONFIG_FILE}" > "${LOG_FILE}" 2>&1 &
ORIGIN_PID=$!
echo "[start-posixv2-origin] pid:        ${ORIGIN_PID}" >&2

# Wait for the origin to come up
for _ in {1..30}; do
    if curl -sk "https://localhost:${PORT}/.well-known/openid-configuration" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

# Issue a token with read+write scopes for the configured prefix
TOKEN="$(pelican origin token create \
    --audience "https://localhost:${PORT}" \
    --scope "storage.read:${PREFIX} storage.modify:${PREFIX}" \
    --lifetime 3600 \
    --subject bench-client)"

echo "${TOKEN}" > "${WORKDIR}/token"

cat <<EOF
---
federation=localhost:${PORT}
prefix=${PREFIX}
token_file=${WORKDIR}/token
log_file=${LOG_FILE}
pid=${ORIGIN_PID}
---
To run the benchmark:

  testing/scripts/run_perf_bench.sh \\
    --federation localhost:${PORT} \\
    --prefix ${PREFIX} \\
    --token-file ${WORKDIR}/token \\
    --label ${STORAGE_TYPE}-local

To stop the origin:

  kill ${ORIGIN_PID}
EOF
