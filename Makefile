# Makefile
# Wrapper that bridges the ACT4 framework clone (in ./work/src/riscv-arch-test)
# with the config in this repository.

# ACT4 framework clone (gitignored under ./work/src/). The pinned tag lives in
# ../RISC-V-emulator-Tools-Container/Dockerfile (ARG ACT_TAG); the container's
# ENV ACT_DIR=/opt/riscv-arch-test overrides this path.
ACT_DIR ?= $(CURDIR)/work/src/riscv-arch-test

# Compiler tag. Namespaces the generated config, the ELFs, and the results
# under one (config, tag) pair. The work server passes the client's tag.
TAG ?= gcc

# Generation settings JSON (load_base, halt_address). The work server writes
# the posted settings to this file before invoking gen_core.py.
SETTINGS ?= $(CURDIR)/work/settings.json

# Config directory layout in this repository: <CONFIG>/<TAG>.
CONFIG_DIR := config/cores/atoomnetmarc/$(CONFIG)/$(TAG)

# Targets that do not need a core configuration.
ifeq ($(filter clean clean-all reset-results reset-failed,$(MAKECMDGOALS)),)

# Core configurations are generated on demand from the bare ISA string
# (e.g. CONFIG=rv32imacb_zicsr_zifencei). The generator maps the name to a
# PlatformIO environment in ../RISC-V-emulator-Native and writes the config
# directory. Existing directories are left untouched.
ifeq ($(wildcard $(CONFIG_DIR)/config.mk),)
$(shell python3 scripts/gen_core.py $(CONFIG) --tag $(TAG) --settings $(SETTINGS) >/dev/null)
endif

# Per-core settings (EMULATOR_ENV, EXCLUDE_EXTENSIONS) live in the config
# directory itself, so adding a core needs no Makefile changes. Command-line
# overrides still win over the values set here.
include $(CONFIG_DIR)/config.mk

endif

# Absolute path to the config file, passed into the external framework.
CONFIG_FILE := $(abspath $(CONFIG_DIR)/test_config.yaml)

# Sail reference model binary; container overrides to /opt/sail-riscv/build/c_emulator.
SAIL_BIN ?= $(CURDIR)/work/src/sail-riscv/build/c_emulator

# UDB gem binary; ruby version directory detected dynamically (no hardcoded 3.x.y).
UDB_BIN := $(firstword $(wildcard $(ACT_DIR)/framework/src/act/data/vendor/bundle/ruby/*/bin))
UDB_GEMFILE := $(ACT_DIR)/framework/src/act/data/Gemfile

# Parallel jobs for run_tests.py. Each emulator process allocates 16 MiB of
# RAM, so keep this modest to avoid exhausting memory on large test suites.
JOBS ?= $(shell nproc)

.PHONY: elfs clean clean-all reset-results reset-failed

# COLUMNS is forced wide because the udb progress bar crashes with
# "negative argument" when the rendered config name does not fit the
# terminal width (long ISA combination names).
# The framework builds into $(WORKDIR)/<udb yaml stem>/elfs; gen_core.py names
# the udb yaml after the tag, so WORKDIR=work/<CONFIG> lands the elfs in the
# pair layout work/<CONFIG>/<TAG>/elfs that the work server serves from.
elfs:
	@test -n "$(UDB_BIN)" || { echo "UDB gems not found under $(ACT_DIR)/framework/src/act/data/vendor/bundle/ruby/*/bin - run in the container or install UDB gems"; exit 1; }
	@cd "$(ACT_DIR)" && COLUMNS=250 PATH="$(SAIL_BIN):$(UDB_BIN):$$PATH" BUNDLE_GEMFILE="$(UDB_GEMFILE)" RUBYOPT="-rbundler/setup" XDG_DATA_HOME="$(ACT_DIR)/work/udb-xdg/$(CONFIG)/data" make -j"$(JOBS)" elfs WORKDIR="$(ACT_DIR)/work/$(CONFIG)" CONFIG_FILES="$(CONFIG_FILE)" $(if $(EXCLUDE_EXTENSIONS),EXCLUDE_EXTENSIONS="$(EXCLUDE_EXTENSIONS)",)

clean:
	@cd "$(ACT_DIR)" && make clean || true
	@rm -rf "$(CONFIG_DIR)" "$(ACT_DIR)/work/$(CONFIG)" "$(ACT_DIR)/work/udb-xdg/$(CONFIG)"

# Stored results live in work/<CONFIG>/<TAG>/ next to the built ELFs. Removing
# the state makes the server re-register every ELF as pending, so a rerun
# skips generation and the ELF build. Run while the server and clients are
# idle: the state files are guarded by flock.
reset-results:
	@rm -f work/*/*/state.json work/*/*/summary.log work/*/*/state.lock
	@rm -rf work/*/*/logs

# Reset only the failed tests; passes are kept and not re-executed.
reset-failed:
	@for s in work/*/*/state.json; do \
		[ -e "$$s" ] || continue; \
		jq '.tests |= with_entries(select(.value.state != "fail"))' "$$s" > "$$s.tmp" && mv "$$s.tmp" "$$s"; \
	done
	@rm -f work/*/*/summary.log
	@rm -rf work/*/*/logs

# Full reset of every (config, tag) pair: framework build, generated core
# configs, and stored results. Recurses into clean for each pair found under
# config/cores/atoomnetmarc/.
clean-all: reset-results
	@for cfg in config/cores/atoomnetmarc/*/; do \
		[ -e "$$cfg" ] || continue; \
		config=$$(basename "$$cfg"); \
		for tagdir in "$$cfg"*/; do \
			[ -e "$$tagdir" ] || continue; \
			$(MAKE) clean CONFIG=$$config TAG=$$(basename "$$tagdir") --no-print-directory; \
		done; \
	done
	@rm -rf config/cores/atoomnetmarc/*
