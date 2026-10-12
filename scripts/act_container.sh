#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Run a command inside the ACT container image (riscv-emulator-tools-container).
#
# Without arguments: start the DUT work server inside the container and
# publish it on the host port given by ACT_PORT (default 8000).
#
#   scripts/act_container.sh
#   ACT_PORT=9000 scripts/act_container.sh
#   SERVER_ARGS="--workers 32" scripts/act_container.sh
#
# With arguments: dumb wrapper. Resolves the image, assembles the podman run
# command (mounts + workdir + INI_FILE export) and exec's the given command
# inside it. All other per-run facts (CONFIG, EMULATOR_SRC_DIR, JOBS, ...) are
# make variables on the command line, e.g.:
#
#   scripts/act_container.sh make elfs CONFIG=rv32i EMULATOR_SRC_DIR=/emulator
#
# Image resolution: local build wins, else pull the remote tag, else a helpful
# error. No build fallback. Override names via ACT_IMAGE_LOCAL/ACT_IMAGE_REMOTE.
# Use --print to dump the exact command without executing it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACT_REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"
EMULATOR_REPO="$(cd "${ACT_REPO}/.." && pwd)/RISC-V-emulator-Native"

LOCAL_IMAGE="${ACT_IMAGE_LOCAL:-riscv-emulator-tools-container}"
REMOTE_IMAGE="${ACT_IMAGE_REMOTE:-ghcr.io/atoomnetmarc/riscv-emulator-tools-container:latest}"

if [[ "${1:-}" == "--print" ]]; then
  PRINT_ONLY=1
  shift
else
  PRINT_ONLY=0
fi

if podman image exists "$LOCAL_IMAGE"; then
  IMAGE="$LOCAL_IMAGE"
elif podman pull "$REMOTE_IMAGE" >/dev/null 2>&1; then
  IMAGE="$REMOTE_IMAGE"
else
  echo "Image '$LOCAL_IMAGE' not found and could not pull '$REMOTE_IMAGE'." >&2
  echo "Build it: cd ${ACT_REPO%/*}/RISC-V-emulator-Tools-Container && podman build -t $LOCAL_IMAGE ." >&2
  exit 1
fi

# Without arguments: start the DUT work server and publish it on the host.
if [[ $# -eq 0 ]]; then
  exec podman run --rm \
    --pids-limit=0 \
    -v "${ACT_REPO}:/act" \
    -v "${EMULATOR_REPO}:/emulator" \
    -v "${ACT_REPO}/work:/opt/riscv-arch-test/work" \
    -w /act \
    -e INI_FILE=/emulator/platformio_isa-extension-combination_env.ini \
    -e UV_NO_SYNC=1 \
    -p "${ACT_PORT:-8000}:8000" \
    -- "$IMAGE" python3 -m server ${SERVER_ARGS:-}
fi

# Mounts:
#   /act                      - this repository (rw: gen_core.py writes configs)
#   /emulator                 - emulator source repo (pre-built binaries inside)
#   /opt/riscv-arch-test/work - container clone's work dir (rw: ELF, summaries,
#                               run logs, report.html all land on the host)
# -it only when stdin is a tty so test_all.sh loops work non-interactively.
TTY_FLAG=()
if [[ -t 0 ]]; then
  TTY_FLAG=(-it)
fi

CMD=(podman run --rm --pids-limit=0 "${TTY_FLAG[@]}"
  -v "${ACT_REPO}:/act"
  -v "${EMULATOR_REPO}:/emulator"
  -v "${ACT_REPO}/work:/opt/riscv-arch-test/work"
  -w /act
  -e INI_FILE=/emulator/platformio_isa-extension-combination_env.ini
  -e UV_NO_SYNC=1)
CMD+=(-- "$IMAGE" "$@")

if ((PRINT_ONLY)); then
  printf '%q ' "${CMD[@]}"
  printf '\n'
else
  exec "${CMD[@]}"
fi
