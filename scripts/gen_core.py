#!/usr/bin/env python3
# Copyright Marc Ketel
# SPDX-License-Identifier: Apache-2.0
#
# Generate a core test configuration from an ISA combination.
#
# Usage: gen_core.py <config> --tag <tag> --settings <settings.json>
#   e.g. gen_core.py rv32imacb_zicsr_zifencei --tag gcc --settings s.json
#
# The config name is the bare ISA string. It maps to a PlatformIO
# environment in ../RISC-V-emulator-Native/platformio_isa-extension-combination_env.ini
# (rv32imacb_zicsr_zifencei -> env RV32IMACBZicsr_Zifencei). The env block
# supplies the emulator build flags and the "# act-" metadata comments.
#
# The settings JSON holds the DUT generation parameters:
#   {"load_base": "0x80000000", "halt_address": "0x20000000"}
# Both keys are required. The server passes the settings posted by the
# client, so one generated config serves any DUT that matches them.
#
# Output: config/cores/atoomnetmarc/<config>/<tag>/ with the files the ACT
# framework and the Makefile wrapper need.

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_DIR / "config" / "cores" / "template"
CORES_DIR = REPO_DIR / "config" / "cores" / "atoomnetmarc"
# INI_FILE defaults to the ini under EMULATOR_SRC_DIR (the emulator source
# repo). The ACT container sets EMULATOR_SRC_DIR=/emulator; override INI_FILE
# directly for a non-standard layout.
_EMULATOR_SRC_DIR = os.environ.get(
    "EMULATOR_SRC_DIR",
    str(REPO_DIR.parent / "RISC-V-emulator-Native"),
)
INI_FILE = Path(os.environ.get(
    "INI_FILE",
    str(Path(_EMULATOR_SRC_DIR) / "platformio_isa-extension-combination_env.ini"),
))

SETTINGS_KEYS = ("load_base", "halt_address")
OPTIONAL_SETTINGS_KEYS = ("firmware_env",)

# Per-subset metadata. "udb" entries land in implemented_extensions of the
# architecture configuration, "sail" keys are flipped to supported in
# sail.json, "params" are appended to the yaml params section, "header" defines
# are appended to rvtest_config.h.
SUBSETS = {
    "M": {
        "udb": [("M", "2.0")],
        "sail": ["M"],
        "params": ["  # M params", "  MUTABLE_MISA_M: false"],
        "header": [],
    },
    "A": {
        "udb": [("A", "2.1.0"), ("Zaamo", "1.0.0"), ("Zalrsc", "1.0.0")],
        "sail": ["A", "Zaamo", "Zalrsc"],
        "params": [
            "  # A params",
            "  MISALIGNED_AMO: false",
            '  LRSC_RESERVATION_STRATEGY: "reserve exactly enough to cover the access"',
            "  LRSC_FAIL_ON_VA_SYNONYM: false",
            '  LRSC_MISALIGNED_BEHAVIOR: "always raise access fault"',
            "  LRSC_FAIL_ON_NON_EXACT_LRSC: false",
            "  MUTABLE_MISA_A: false",
        ],
        "header": ["ZAAMO_SUPPORTED", "ZALRSC_SUPPORTED"],
    },
    "C": {
        "udb": [("C", "2.0"), ("Zca", "1.0.0")],
        "sail": ["Zca"],
        "params": ["  # C params", "  MUTABLE_MISA_C: false"],
        "header": ["ZCA_SUPPORTED"],
    },
    "B": {
        "udb": [
            ("B", "1.0.0"),
            ("Zba", "1.0.0"),
            ("Zbb", "1.0.0"),
            ("Zbc", "1.0.0"),
            ("Zbs", "1.0.0"),
        ],
        "sail": ["B", "Zba", "Zbb", "Zbc", "Zbs"],
        "params": ["  # B params", "  MUTABLE_MISA_B: true"],
        "header": ["ZBA_SUPPORTED", "ZBB_SUPPORTED", "ZBC_SUPPORTED", "ZBS_SUPPORTED"],
    },
    # The standalone Zaamo/Zalrsc subsets carry no MUTABLE_MISA_A param: UDB
    # only allows that parameter when the A extension itself is implemented.
    "Zaamo": {
        "udb": [("Zaamo", "1.0.0")],
        "sail": ["Zaamo"],
        "params": ["  # Zaamo params", "  MISALIGNED_AMO: false"],
        "header": ["ZAAMO_SUPPORTED"],
    },
    "Zalrsc": {
        "udb": [("Zalrsc", "1.0.0")],
        "sail": ["Zalrsc"],
        "params": [
            "  # Zalrsc params",
            '  LRSC_RESERVATION_STRATEGY: "reserve exactly enough to cover the access"',
            "  LRSC_FAIL_ON_VA_SYNONYM: false",
            '  LRSC_MISALIGNED_BEHAVIOR: "always raise access fault"',
            "  LRSC_FAIL_ON_NON_EXACT_LRSC: false",
        ],
        "header": ["ZALRSC_SUPPORTED"],
    },
    "Zmmul": {
        "udb": [("Zmmul", "1.0.0")],
        "sail": ["Zmmul"],
        "params": [],
        "header": [],
    },
    "Zicsr": {"udb": [("Zicsr", "2.0")], "sail": [], "params": [], "header": []},
    "Zifencei": {"udb": [("Zifencei", "2.0.0")], "sail": [], "params": [], "header": []},
    # The standalone Zb* subsets carry no MUTABLE_MISA_B param: UDB only
    # allows that parameter when the B extension itself is implemented.
    "Zba": {
        "udb": [("Zba", "1.0.0")],
        "sail": ["Zba"],
        "params": [],
        "header": ["ZBA_SUPPORTED"],
    },
    "Zbb": {
        "udb": [("Zbb", "1.0.0")],
        "sail": ["Zbb"],
        "params": [],
        "header": ["ZBB_SUPPORTED"],
    },
    "Zbc": {
        "udb": [("Zbc", "1.0.0")],
        "sail": ["Zbc"],
        "params": [],
        "header": ["ZBC_SUPPORTED"],
    },
    "Zbkc": {
        "udb": [("Zbkc", "1.0.0")],
        "sail": ["Zbkc"],
        "params": [],
        "header": ["ZBKC_SUPPORTED"],
    },
    "Zbkb": {
        "udb": [("Zbkb", "1.0.0")],
        "sail": ["Zbkb"],
        "params": [],
        "header": ["ZBKB_SUPPORTED"],
    },
    "Zbkx": {
        "udb": [("Zbkx", "1.0.0")],
        "sail": ["Zbkx"],
        "params": [],
        "header": ["ZBKX_SUPPORTED"],
    },
    "Zcb": {
        "udb": [("Zcb", "1.0.0"), ("Zca", "1.0.0")],
        "sail": ["Zcb"],
        "params": [],
        "header": ["ZCB_SUPPORTED"],
    },
    "Zcmop": {
        "udb": [("Zcmop", "1.0.0"), ("Zca", "1.0.0")],
        "sail": ["Zcmop"],
        "params": [],
        "header": [],
    },
    "Zimop": {
        "udb": [("Zimop", "1.0.0")],
        "sail": ["Zimop"],
        "params": [],
        "header": [],
    },
    "Zicond": {
        "udb": [("Zicond", "1.0.0")],
        "sail": ["Zicond"],
        "params": ["  # Zicond params", "  TIME_CSR_IMPLEMENTED: true"],
        "header": ["ZICOND_SUPPORTED"],
    },
    "Zicntr": {
        "udb": [("Zicntr", "2.0.0")],
        "sail": ["Zicntr"],
        "params": [],
        "header": [],
    },
    "Zihintntl": {
        "udb": [("Zihintntl", "1.0.0")],
        "sail": ["Zihintntl"],
        "params": [],
        "header": [],
    },
    "Zihintpause": {
        "udb": [("Zihintpause", "2.0.0")],
        "sail": ["Zihintpause"],
        "params": [],
        "header": [],
    },
    "Zbs": {
        "udb": [("Zbs", "1.0.0")],
        "sail": ["Zbs"],
        "params": [],
        "header": ["ZBS_SUPPORTED"],
    },
    # Not an ISA extension: the emulator is built with RVE_E_MISALIGNED=1.
    # The base template already enables misaligned load/store support, so this
    # subset only makes the env name parseable and generates a plain RV32I config.
    "Misalign": {
        "udb": [],
        "sail": [],
        "params": [],
        "header": [],
    },
}


def env_name_from_config(config: str) -> str:
    # Look up the PlatformIO environment case-insensitively
    # (rv32ibzicsr_zifencei -> RV32IBZicsr_Zifencei).
    for name in re.findall(r"^\[env:(\S+)\]", INI_FILE.read_text(), re.M):
        if name.lower() == config.lower():
            return name
    sys.exit(f"No environment matching {config!r} in {INI_FILE}")


def subsets_from_env(env: str) -> list:
    # The env name may carry a DUT prefix (e.g. ATmega1284P_RV32IMAB_Zicsr);
    # everything from "RV32I" on is the ISA combination. Matching is
    # case-insensitive so a lowercase "# isa:" marker string parses too.
    # RV32IMACBZicsr_Zifencei -> ["M", "A", "C", "B", "Zicsr", "Zifencei"]
    body = re.search(r"RV32I(.*)$", env, re.I).group(1)
    named = sorted([key for key in SUBSETS if len(key) > 1], key=len, reverse=True)
    parts = re.split("(" + "|".join(named) + ")", body, flags=re.I)
    lookup = {key.lower(): key for key in SUBSETS}
    subsets = []
    for part in parts:
        key = lookup.get(part.lower())
        if key:
            subsets.append(key)
        elif part:
            for letter in part:
                if letter == "_":
                    continue
                key = lookup.get(letter.lower())
                if key:
                    subsets.append(key)
                else:
                    sys.exit(f"Unknown subset letter {letter!r} in {env!r}")
    return subsets


def parse_ini_env(env: str) -> list:
    # Returns the act-exclude-extensions values from the env block.
    text = INI_FILE.read_text()
    block = re.search(rf"^\[env:{re.escape(env)}\]\n(.*?)(?=^\[env:|\Z)", text, re.M | re.S)
    if not block:
        sys.exit(f"Env {env!r} not found in {INI_FILE}")
    return re.findall(r"^# act-exclude-extensions: (.+)$", block.group(1), re.M)


def gen_yaml(out: Path, config: str, env: str, subsets: list) -> None:
    text = (TEMPLATE_DIR / "core.yaml").read_text()
    text = text.replace("rv32i variant", f"{config} variant")
    text = text.replace("name: rve-rv32i", f"name: {config}")
    text = text.replace(
        "description: RISC-V emulator (atoomnetmarc) - RV32I configuration DUT",
        f"description: RISC-V emulator (atoomnetmarc) - {env} configuration DUT",
    )
    # Extensions appear in ISA-string order, each followed by its derived
    # extensions (e.g. A also implies Zaamo and Zalrsc).
    ext_lines = []
    param_blocks = []
    for subset in subsets:
        meta = SUBSETS[subset]
        ext_lines += [f'  - {{ name: {name}, version: "= {version}" }}' for name, version in meta["udb"]]
        # TIME_CSR_IMPLEMENTED is only defined for configs with Zicntr.
        if meta["params"] and not (subset == "Zicond" and "Zicntr" not in subsets):
            param_blocks.append("\n".join(meta["params"]))
    text = text.replace(
        '  - { name: Sm, version: "= 1.12.0" }',
        '  - { name: Sm, version: "= 1.12.0" }' + ("\n" + "\n".join(ext_lines) if ext_lines else ""),
    )
    if param_blocks:
        text = text.replace(
            "  PHYS_ADDR_WIDTH: 32\n\n",
            "  PHYS_ADDR_WIDTH: 32\n\n" + "\n\n".join(param_blocks) + "\n\n",
        )
    (out / f"{config}.yaml").write_text(text)


def gen_sail(out: Path, subsets: list) -> None:
    text = (TEMPLATE_DIR / "sail.json").read_text()
    for subset in subsets:
        for key in SUBSETS[subset]["sail"]:
            # The template may already list the extension as supported.
            text, count = re.subn(rf'("{key}": \{{\s*"supported": )false', r"\1true", text)
            if count == 0 and not re.search(rf'"{key}": \{{\s*"supported": true', text):
                sys.exit(f"sail.json template: expected one entry for {key!r}")
    (out / "sail.json").write_text(text)


def gen_header(out: Path, subsets: list) -> None:
    text = (TEMPLATE_DIR / "rvtest_config.h").read_text().rstrip("\n")
    defines = [d for subset in subsets for d in SUBSETS[subset]["header"]]
    if defines:
        text += "\n\n" + "\n".join(f"#define {d}" for d in defines)
    (out / "rvtest_config.h").write_text(text + "\n")


def gen_config_mk(out: Path, env: str, excludes: list) -> None:
    lines = [
        "# Per-core settings for the Makefile wrapper. Generated by scripts/gen_core.py.\n",
        f"EMULATOR_ENV := {env}\n",
        f"EXCLUDE_EXTENSIONS := {' '.join(excludes)}\n",
    ]
    (out / "config.mk").write_text("".join(lines))


def gen_test_config(out: Path, tag: str, config: str) -> None:
    text = (TEMPLATE_DIR / "test_config.yaml").read_text()
    # The framework puts the elfs in $(WORKDIR)/<name>/elfs; name the suite
    # after the tag so they land in work/<CONFIG>/<TAG>/elfs.
    text = text.replace("name: rve-rv32i", f"name: {tag}")
    text = text.replace("udb_config: rve-rv32i.yaml", f"udb_config: {config}.yaml")
    (out / "test_config.yaml").write_text(text)


def gen_link_ld(out: Path, settings: dict) -> None:
    text = (TEMPLATE_DIR / "link.ld").read_text()
    text = text.replace("{{LOAD_BASE}}", settings["load_base"])
    (out / "link.ld").write_text(text)


def gen_rvmodel_macros(out: Path, settings: dict) -> None:
    text = (TEMPLATE_DIR / "rvmodel_macros.h").read_text()
    text = text.replace("{{HALT_ADDRESS}}", settings["halt_address"])
    (out / "rvmodel_macros.h").write_text(text)


def load_settings(path: Path) -> dict:
    try:
        settings = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"Cannot read settings {path}: {exc}")
    missing = [key for key in SETTINGS_KEYS if key not in settings]
    if missing:
        sys.exit(f"Settings missing required keys: {', '.join(missing)}")
    allowed = SETTINGS_KEYS + OPTIONAL_SETTINGS_KEYS
    unknown = [key for key in settings if key not in allowed]
    if unknown:
        sys.exit(f"Settings has unknown keys: {', '.join(unknown)}")
    return settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--settings", required=True, type=Path)
    args = parser.parse_args()
    config = args.config
    settings = load_settings(args.settings)

    # A posted firmware_env names the PlatformIO environment to build (e.g.
    # the AVR ATmega1284P_ACT firmware env); without it the environment is
    # looked up from the ini by the ISA string.
    env = settings.get("firmware_env") or env_name_from_config(config)
    # An env whose name does not encode an ISA (ATmega1284P_ACT) takes its
    # subsets from the config, which carries the env's "# isa:" marker string.
    if re.search(r"RV32I", env, re.I):
        subsets = subsets_from_env(env)
    else:
        subsets = subsets_from_env(config)
    try:
        excludes = parse_ini_env(env_name_from_config(config))
    except SystemExit:
        excludes = []

    out = CORES_DIR / config / args.tag
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    gen_link_ld(out, settings)
    gen_rvmodel_macros(out, settings)
    gen_yaml(out, config, env, subsets)
    gen_sail(out, subsets)
    gen_header(out, subsets)
    gen_config_mk(out, env, excludes)
    gen_test_config(out, args.tag, config)
    print(f"Generated {out}")


if __name__ == "__main__":
    main()
