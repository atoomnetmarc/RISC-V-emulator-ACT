This is intended for me only. It contains code and hints on how to use [RISC-V Architectural Certification Tests](https://github.com/riscv/riscv-arch-test) tests using my [Linux implementation](https://github.com/atoomnetmarc/RISC-V-emulator-Native) of my [RISC-V emulator](https://github.com/atoomnetmarc/RISC-V-emulator).

# Quickstart

Build the container image once:

```bash
cd ../RISC-V-emulator-Tools-Container && podman build -t riscv-emulator-tools-container .
```

Start the server (stays in the foreground):

```bash
scripts/act_container.sh
```

In a second terminal, build the emulator binary and throw a client at it:

```bash
pio run -e RV32I -d ../RISC-V-emulator-Native
PYTHONPATH=client python -m actwork --server http://127.0.0.1:8000 \
  --ini ../RISC-V-emulator-Native/platformio_isa-extension-combination_env.ini \
  --smoke '^rv32i$' --tag gcc \
  --load-base 0x80000000 --halt-address 0x20000000 \
  --run '../RISC-V-emulator-Native/binaries/gcc/{env} {binary}'
```

Watch live progress at http://127.0.0.1:8000/ and the failure report at http://127.0.0.1:8000/report.

# Architecture

The container runs the ACT framework and the DUT work server (`server/`, plain Python stdlib). The DUT (native emulator, simavr/AVR, or real hardware) runs on the host or on a remote machine next to the work client (`client/`). The client pulls work over HTTP, runs it on the DUT, and reports raw output back. The API contract is `server/static/openapi.yaml`

`scripts/act_container.sh` resolves the image (local build wins, else `podman pull`), mounts this repo at `/act`, the emulator repo at `/emulator` and this repo's `work/` over the container clone's `work/` (so ELFs, state, summaries and run logs land on the host), and exports `INI_FILE` pointing at the emulator repo. Without arguments it starts the server and publishes `ACT_PORT` (default 8000). With arguments it exec's the given command inside the container (`--print` dumps the exact command). `podman` is the only host dependency.

# Client

One client per DUT. The client reads the ini, selects the ISA strings (`--full` for every environment or `--smoke` for the `# smoke`-marked ones, then the filter regex), posts the batch with its generation settings, and iterates: claim, run on the DUT, post output. The `--run` template is any shell command with `{env}` (the PlatformIO environment for the config, e.g. `RV32IA`) and `{binary}` placeholders, so any DUT works and each config runs against its own binary. The ELFs are self-checking: the test compares its results against the reference signature on the DUT and prints `RVCP-SUMMARY: TEST PASSED` or `TEST FAILED` on the console, which the server parses as the verdict. The lease doubles as the run timeout: the client kills the DUT when it expires, which fails the test for lack of a verdict.

```bash
PYTHONPATH=client python -m actwork --server http://127.0.0.1:8000 \
  --ini ../RISC-V-emulator-Native/platformio_isa-extension-combination_env.ini \
  --full '^RV32IM' --tag gcc-13 \
  --load-base 0x80000000 --halt-address 0x20000000 \
  --run './binaries/gcc/{env} {binary}'
```

The compiler of the DUT binaries is part of the `--run` template, not of this repository. Build the emulator with another compiler in the emulator repo (`python3 cmake/run-matrix.py clang` writes `binaries/clang/`) and point the template at that directory under a matching tag:

```bash
PYTHONPATH=client python -m actwork --server http://127.0.0.1:8000 \
  --ini ../RISC-V-emulator-Native/platformio_isa-extension-combination_env.ini \
  --smoke '^rv32i$' --tag clang \
  --load-base 0x80000000 --halt-address 0x20000000 \
  --run '../RISC-V-emulator-Native/binaries/clang/{env} {binary}'
```

A client restart can re-post its batch without losing results; claims expire after `--lease-seconds` and the tests are re-handed out.

Results are stored per `(config, tag)` pair in `work/<config>/<tag>/` (`state.json`, `summary.log`, `logs/`). The server hands out nothing new once every test in a pair has a pass/fail state, so a rerun only re-executes tests after their stored state is removed. The generated core configs under `config/cores/atoomnetmarc/` are gitignored and regenerated on demand. `make reset-results` removes the stored results but keeps the built ELFs, so a rerun skips generation and the ELF build; `make reset-failed` keeps the passes and only re-executes the failed tests. Run both while the server and clients are idle. To start over:

```bash
make reset-failed                     # only failed tests re-run, passes kept
make reset-results                    # all tests re-run, ELFs and core configs kept
make clean CONFIG=<config> TAG=<tag>  # one pair: framework build + core config
make clean-all                        # every pair: framework builds + core configs + results
```

# Generation

The first generation for a `(config, tag)` pair runs `scripts/gen_core.py` (inside the container) with the posted settings, which writes `config/cores/atoomnetmarc/<config>/<tag>/` from the templates in `config/cores/template/`. A config name is the bare ISA string and maps to a PlatformIO environment in `../RISC-V-emulator-Native/platformio_isa-extension-combination_env.ini` (`rv32imacb_zicsr_zifencei` maps to env `RV32IMACBZicsr_Zifencei`). The generated directory is gitignored; `make clean CONFIG=<config>` removes it. Per-core metadata (such as excluded ACT extensions) travels from the emulator's ini generator as `# act-` comment lines in the env block.

Raw output, `summary.log`, and the per-pair state file land under `work/src/riscv-arch-test/work/<config>/<tag>/`. Remove the ACT build outputs (including the framework's `work/` directory) and the generated core configs with `scripts/act_container.sh make clean CONFIG=<config>`.

# AVR backend (simavr)

Tests can run on an ATmega1284P simulated by [simavr](https://github.com/rv8-io/simavr). One firmware, `ATmega1284P_ACT` in `../RISC-V-emulator-AVR/platformio.ini`, runs every config: it implements the full ACT ISA combination and talks to the simavr peripherals (VPI-backed RAM with host memory, and an exit register that stops the simulation and signals pass/fail). Build it and copy the result to `hex/`:

```bash
pio run -e ATmega1284P_ACT -d ../RISC-V-emulator-AVR
cp ../RISC-V-emulator-AVR/.pio/build/ATmega1284P_ACT/firmware.hex ../RISC-V-emulator-AVR/hex/ATmega1284P_ACT.hex
```

The AVR client is just another client: it selects from the AVR project's own ini, where only the ACT firmware envs carry `# isa:` / `# smoke` markers (`ATmega1284P_ACT` marks the full ISA combination it implements; the other envs are core builds, not test firmwares). It posts its own settings (`--halt-address 0x02000000`) under its own tag and runs every test against the one ACT firmware:

```bash
PYTHONPATH=client python -m actwork --server http://127.0.0.1:8000 \
  --ini ../RISC-V-emulator-AVR/platformio.ini \
  --smoke --tag avr \
  --load-base 0x80000000 --halt-address 0x02000000 \
  --run '../RISC-V-emulator-SimAVR/binaries/gcc/simavr-host ../RISC-V-emulator-AVR/hex/ATmega1284P_ACT.hex {binary}'
```

The simavr wrapper builds in `../RISC-V-emulator-SimAVR` (env `simavr-host`). To debug a failing test, point `--run` at the disassembly firmware instead: build `ATmega1284P_ACT_DISASM` (per-instruction UART disassembly, much slower) and copy it the same way.

# License

I license my code Apache2.0, however check the files for individual licenses.

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
