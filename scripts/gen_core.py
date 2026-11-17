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
# sail.json, "params" are appended to the yaml params section. The framework
# derives the rvtest_config.h defines from the architecture configuration.
SUBSETS = {
    "M": {
        "udb": [("M", "2.0")],
        "sail": ["M"],
        "params": ["  # M params", "  MUTABLE_MISA_M: false"],
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
    },
    "C": {
        "udb": [("C", "2.0"), ("Zca", "1.0.0")],
        "sail": ["Zca"],
        "params": ["  # C params", "  MUTABLE_MISA_C: false"],
    },
    "B": {
        "udb": [
            ("B", "1.0.0"),
            ("Zba", "1.0.0"),
            ("Zbb", "1.0.0"),
            ("Zbc", "1.0.0"),
            ("Zbkc", "1.0.0"),
            ("Zbs", "1.0.0"),
        ],
        "sail": ["B", "Zba", "Zbb", "Zbc", "Zbkc", "Zbs"],
        "params": ["  # B params", "  MUTABLE_MISA_B: true"],
    },
    # The standalone Zaamo/Zalrsc subsets carry no MUTABLE_MISA_A param: UDB
    # only allows that parameter when the A extension itself is implemented.
    "Zaamo": {
        "udb": [("Zaamo", "1.0.0")],
        "sail": ["Zaamo"],
        "params": ["  # Zaamo params", "  MISALIGNED_AMO: false"],
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
    },
    "Zmmul": {
        "udb": [("Zmmul", "1.0.0")],
        "sail": ["Zmmul"],
        "params": [],
    },
    "Zicsr": {"udb": [("Zicsr", "2.0")], "sail": ["Zicsr"], "params": []},
    "Zifencei": {"udb": [("Zifencei", "2.0.0")], "sail": ["Zifencei"], "params": []},
    # Sv32 implies S-mode and Zicsr. The emulator implements Svade (a page
    # fault when A is clear, or when a store finds D clear) and Sstvala
    # (stval holds the faulting virtual address).
    "Sv32": {
        "udb": [
            ("U", "1.0.0"),
            ("S", "1.13.0"),
            ("Sv32", "1.0"),
            ("Svade", "1.0.0"),
            ("Sstvala", "1.0.0"),
        ],
        "sail": ["S", "U", "Sv32", "Svade", "Sstvala"],
        "params": [
            "  # S params",
            "  SXLEN: [32]",
            "  UXLEN: [32]",
            "  STVEC_MODES: [0, 1]",
            "  STVEC_BASE_ALIGNMENT_VECTORED: 0x4",
            "  SATP_MODE_BARE: true",
            "  ASID_WIDTH: 0",
            "  STVAL_WIDTH: 32",
            "  S_MODE_ENDIANNESS: little",
            "  U_MODE_ENDIANNESS: little",
            "  MUTABLE_MISA_S: false",
            "  MUTABLE_MISA_U: false",
            "  TRAP_ON_ECALL_FROM_S: true",
            "  TRAP_ON_ECALL_FROM_U: true",
            "  MSTATUS_FS_LEGAL_VALUES: [0]",
            "  MSTATUS_VS_LEGAL_VALUES: [0]",
            "  SCOUNTENABLE_EN: [false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false]",
            "  # Sstvala params",
            "  REPORT_VA_IN_STVAL_ON_BREAKPOINT: true",
            "  REPORT_VA_IN_STVAL_ON_INSTRUCTION_ACCESS_FAULT: true",
            "  REPORT_VA_IN_STVAL_ON_LOAD_ACCESS_FAULT: true",
            "  REPORT_VA_IN_STVAL_ON_STORE_AMO_ACCESS_FAULT: true",
            "  REPORT_VA_IN_STVAL_ON_INSTRUCTION_PAGE_FAULT: true",
            "  REPORT_VA_IN_STVAL_ON_LOAD_PAGE_FAULT: true",
            "  REPORT_VA_IN_STVAL_ON_STORE_AMO_PAGE_FAULT: true",
            "  REPORT_VA_IN_STVAL_ON_INSTRUCTION_MISALIGNED: true",
            "  REPORT_VA_IN_STVAL_ON_LOAD_MISALIGNED: true",
            "  REPORT_VA_IN_STVAL_ON_STORE_AMO_MISALIGNED: true",
            "  REPORT_ENCODING_IN_STVAL_ON_ILLEGAL_INSTRUCTION: true",
            "  # mtval page-fault reporting",
            "  REPORT_VA_IN_MTVAL_ON_INSTRUCTION_PAGE_FAULT: true",
            "  REPORT_VA_IN_MTVAL_ON_LOAD_PAGE_FAULT: true",
            "  REPORT_VA_IN_MTVAL_ON_STORE_AMO_PAGE_FAULT: true",
        ],
    },
    # The standalone Zb* subsets carry no MUTABLE_MISA_B param: UDB only
    # allows that parameter when the B extension itself is implemented.
    "Zba": {
        "udb": [("Zba", "1.0.0")],
        "sail": ["Zba"],
        "params": [],
    },
    "Zbb": {
        "udb": [("Zbb", "1.0.0")],
        "sail": ["Zbb"],
        "params": [],
    },
    # UDB requires Zbkc when Zbc is present, and the emulator enables Zbkc
    # whenever Zbc is enabled (RiscvEmulatorConfig.h).
    "Zbc": {
        "udb": [("Zbc", "1.0.0"), ("Zbkc", "1.0.0")],
        "sail": ["Zbc", "Zbkc"],
        "params": [],
    },
    "Zbkc": {
        "udb": [("Zbkc", "1.0.0")],
        "sail": ["Zbkc"],
        "params": [],
    },
    "Zbkb": {
        "udb": [("Zbkb", "1.0.0")],
        "sail": ["Zbkb"],
        "params": [],
    },
    "Zbkx": {
        "udb": [("Zbkx", "1.0.0")],
        "sail": ["Zbkx"],
        "params": [],
    },
    "Zcb": {
        "udb": [("Zcb", "1.0.0"), ("Zca", "1.0.0")],
        "sail": ["Zcb"],
        "params": [],
    },
    "Zcmop": {
        "udb": [("Zcmop", "1.0.0"), ("Zca", "1.0.0")],
        "sail": ["Zcmop"],
        "params": [],
    },
    "Zimop": {
        "udb": [("Zimop", "1.0.0")],
        "sail": ["Zimop"],
        "params": [],
    },
    "Zicond": {
        "udb": [("Zicond", "1.0.0")],
        "sail": ["Zicond"],
        "params": [],
    },
    "Zicntr": {
        "udb": [("Zicntr", "2.0.0")],
        "sail": ["Zicntr"],
        "params": ["  # Zicntr params", "  TIME_CSR_IMPLEMENTED: true"],
    },
    "Zihintntl": {
        "udb": [("Zihintntl", "1.0.0")],
        "sail": ["Zihintntl"],
        "params": [],
    },
    "Zihintpause": {
        "udb": [("Zihintpause", "2.0.0")],
        "sail": ["Zihintpause"],
        "params": [],
    },
    "Zbs": {
        "udb": [("Zbs", "1.0.0")],
        "sail": ["Zbs"],
        "params": [],
    },
    # Not an ISA extension: the emulator is built with RVE_E_MISALIGNED=1.
    # The base template already enables misaligned load/store support, so this
    # subset only makes the env name parseable and generates a plain RV32I config.
    "Misalign": {
        "udb": [],
        "sail": [],
        "params": [],
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


def parse_ini_defines(env: str) -> list:
    # Returns the act-define macro definitions from the env block.
    text = INI_FILE.read_text()
    block = re.search(rf"^\[env:{re.escape(env)}\]\n(.*?)(?=^\[env:|\Z)", text, re.M | re.S)
    if not block:
        return []
    return re.findall(r"^# act-define: (.+)$", block.group(1), re.M)


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
        if meta["params"]:
            param_blocks.append("\n".join(meta["params"]))
    # UDB only defines these parameters when the configuration implements the
    # extensions that define them. Emitting them unconditionally makes the
    # rv32i configuration fail validation ("not defined by this config").
    if "Zicsr" in subsets:
        param_blocks.append("\n".join([
            "  # Interrupts (defined by Zicsr + Sm)",
            "  MEI_INTR_IMPL: true",
            "  MSI_INTR_IMPL: false",
            "  MTI_INTR_IMPL: true",
        ]))
    if "Sv32" in subsets:
        param_blocks.append("\n".join([
            "  # Trap delegation: the writable medeleg and mideleg bits.",
            "  MEDELEG_DELEGATABLE: [true, true, true, true, true, true, true, true, true, false, false, false, true, true, false, true, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false]",
            "  MIDELEG_DELEGATABLE: [false, true, false, false, false, true, false, false, false, true, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false, false]",
            "  # Supervisor interrupts (defined by Zicsr + S)",
            "  SEI_INTR_IMPL: true",
            "  SSI_INTR_IMPL: true",
            "  STI_INTR_IMPL: true",
        ]))
    text = text.replace(
        '  - { name: Sm, version: "= 1.13.0" }',
        '  - { name: Sm, version: "= 1.13.0" }' + ("\n" + "\n".join(ext_lines) if ext_lines else ""),
    )
    if param_blocks:
        text = text.replace(
            "  PHYS_ADDR_WIDTH: 32\n\n",
            "  PHYS_ADDR_WIDTH: 32\n\n" + "\n\n".join(param_blocks) + "\n\n",
        )
    (out / f"{config}.yaml").write_text(text)


def gen_sail(out: Path, subsets: list) -> None:
    config = json.loads((TEMPLATE_DIR / "sail.json").read_text())
    enabled = set()
    for subset in subsets:
        enabled.update(SUBSETS[subset]["sail"])
    # Sv32 supports 34-bit physical addresses; without it the reference
    # rejects a width above 32.
    config["memory"]["physaddr_bits"] = 34 if "Sv32" in enabled else 32
    # The emulator has no PMP.
    config["memory"]["pmp"]["count"] = 0
    config["memory"]["pmp"]["usable_count"] = 0
    # The emulator splits a misaligned access at a 4 KiB page boundary.
    config["memory"]["misaligned"]["default_allowed_within_exp"] = 12
    # The emulator has a 9-bit ASID and no hypervisor.
    config["memory"]["asidlen"] = 9
    config["memory"]["vmidlen"] = 0
    # The emulator has no Sscofpmf, so the mhpmevent CSRs are 32 bits.
    if "Sscofpmf" not in enabled:
        config["base"]["hpmevent_width_bits"] = 32
    # The emulator implements only HPM counter 3.
    config["base"]["writable_hpm_counters"]["value"] = "0x0000_0008"
    # The emulator has no mcountinhibit.
    config["base"]["mcountinhibit"]["supported"] = False
    # The emulator's mcounteren and scounteren write masks.
    config["base"]["mcounteren_writable_bits"]["value"] = "0x0000_0007"
    config["base"]["scounteren_writable_bits"]["value"] = "0x0000_0007"
    # The emulator's WFI returns immediately.
    config["platform"]["wfi_is_nop"] = True
    config["platform"]["wfi_available_to_user_mode"] = True
    config["platform"]["archid"] = 1
    # The emulator's medeleg and mideleg write masks.
    config["base"]["medeleg"]["delegatable_bits"]["value"] = "0x0000_0000_000c_b1FF"
    config["base"]["mideleg"]["delegatable_bits"]["value"] = "0x0000_0222"
    # The emulator reports the faulting address in xtval.
    config["base"]["xtval_nonzero"] = {
        "illegal_instruction": True,
        "virtual_instruction": True,
        "load_guest_page_fault": True,
        "samo_guest_page_fault": True,
        "fetch_guest_page_fault": True,
        "software_breakpoint": True,
        "hardware_breakpoint": True,
        "load_address_misaligned": True,
        "load_access_fault": True,
        "load_page_fault": True,
        "samo_address_misaligned": True,
        "samo_access_fault": True,
        "samo_page_fault": True,
        "fetch_address_misaligned": True,
        "fetch_access_fault": True,
        "fetch_page_fault": True,
        "software_check": True,
        "hardware_error": True,
        "reserved_exceptions": False,
    }
    for key, value in config["extensions"].items():
        if key == "V":
            value["support_level"] = "Full" if key in enabled else "Disabled"
        elif "supported" in value:
            value["supported"] = key in enabled
        if "mutable_misa" in value:
            value["mutable_misa"] = key == "B"
    # The emulator has no Stateen extensions. Every stateen bit is read-only zero.
    stateen = config["extensions"].get("Stateen")
    if stateen:
        for key in ("Smstateen", "Ssstateen"):
            if key in stateen:
                stateen[key]["supported"] = key in enabled
        for key in ("mstateen0", "sstateen0"):
            if key in stateen:
                for bit in stateen[key]:
                    stateen[key][bit] = "CSRBit_ReadOnlyZero"
    # The emulator has no F or V. The reference must not declare their context.
    if "F" not in enabled:
        config["base"]["mstatus"]["fs_legal_states"] = "ExtContext_Off"
    if "V" not in enabled:
        config["base"]["mstatus"]["vs_legal_states"] = "ExtContext_Off"
    (out / "sail.json").write_text(json.dumps(config, indent=2) + "\n")


def gen_config_mk(out: Path, env: str, excludes: list) -> None:
    lines = [
        "# Per-core settings for the Makefile wrapper. Generated by scripts/gen_core.py.\n",
        f"EMULATOR_ENV := {env}\n",
        f"EXCLUDE_EXTENSIONS := {' '.join(excludes)}\n",
    ]
    (out / "config.mk").write_text("".join(lines))


def gen_test_config(out: Path, tag: str, config: str, subsets: list) -> None:
    text = (TEMPLATE_DIR / "test_config.yaml").read_text()
    # The framework puts the elfs in $(WORKDIR)/<name>/elfs; name the suite
    # after the tag so they land in work/<CONFIG>/<TAG>/elfs.
    text = text.replace("name: rve-rv32i", f"name: {tag}")
    text = text.replace("udb_config: rve-rv32i.yaml", f"udb_config: {config}.yaml")
    if "Sv32" in subsets:
        text = text.replace("include_priv_tests: False", "include_priv_tests: True")
    (out / "test_config.yaml").write_text(text)


def gen_link_ld(out: Path, settings: dict) -> None:
    text = (TEMPLATE_DIR / "link.ld").read_text()
    text = text.replace("{{LOAD_BASE}}", settings["load_base"])
    (out / "link.ld").write_text(text)


def gen_rvmodel_macros(out: Path, settings: dict, subsets: list, defines: list) -> None:
    text = (TEMPLATE_DIR / "rvmodel_macros.h").read_text()
    text = text.replace("{{HALT_ADDRESS}}", settings["halt_address"])
    if "Sv32" in subsets:
        # The DUT has standard M-mode. The framework's default M-mode boot sets
        # up the trap handlers, and STANDARD_SM_SUPPORTED also emits the
        # rvtest_set_*_int entry points that the privileged tests call.
        text = text.replace(
            "# The DUT has no privilege modes. An empty RVMODEL_BOOT_TO_MMODE skips the\n"
            "# framework's default M-mode boot, which writes M-mode CSRs at startup.\n"
            "#define RVMODEL_BOOT_TO_MMODE\n",
            "# The DUT has standard M-mode: use the framework's default M-mode boot.\n"
            "#define STANDARD_SM_SUPPORTED\n",
        )
        # The CLINT provides the machine timer and the machine software
        # interrupt at the Sail platform addresses.
        text = text.replace(
            "//#define RVMODEL_MTIME_ADDRESS  /* unimplemented */\n"
            "\n"
            "//#define RVMODEL_MTIMECMP_ADDRESS   /* unimplemented */\n",
            "#define RVMODEL_MTIME_ADDRESS     0x0200BFF8\n"
            "#define RVMODEL_MTIMECMP_ADDRESS  0x02004000\n"
            "#define RVMODEL_MSIP_ADDRESS      0x02000000\n"
            "\n",
        )
        # The platform interrupt CSRs raise and clear mip.MEIP. A write to
        # 0x8C0 sets and a write to 0x8C1 clears the software-set platform
        # bits. The _M variants serve the M-mode helpers, which do not use
        # T-SBI.
        text = text.replace(
            "#define RVMODEL_SET_MEXT_INT(_R1, _R2)\n"
            "\n"
            "#define RVMODEL_CLR_MEXT_INT(_R1, _R2)\n",
            "#define RVMODEL_SET_MEXT_INT(_R1, _R2) li _R1, 0x800; csrw 0x8C0, _R1\n"
            "\n"
            "#define RVMODEL_CLR_MEXT_INT(_R1, _R2) li _R1, 0x800; csrw 0x8C1, _R1\n"
            "\n"
            "#define RVMODEL_SET_MEXT_INT_M(_R1, _R2) li _R1, 0x800; csrw 0x8C0, _R1\n"
            "\n"
            "#define RVMODEL_CLR_MEXT_INT_M(_R1, _R2) li _R1, 0x800; csrw 0x8C1, _R1\n",
        )
    if defines:
        block = ""
        for definition in defines:
            name = re.split(r"[\s(]", definition, maxsplit=1)[0]
            block += f"#undef {name}\n#define {definition}\n"
        text = text.replace("#endif // _RVMODEL_MACROS_H", block + "\n#endif // _RVMODEL_MACROS_H")
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
    # UDB 0.1.16 declares that M 2.0.0 requires Zmmul. The emulator implements
    # full M, so Zmmul is implied whenever M is implemented.
    if "M" in subsets and "Zmmul" not in subsets:
        subsets.append("Zmmul")
    try:
        excludes = parse_ini_env(env_name_from_config(config))
    except SystemExit:
        excludes = []
    defines = parse_ini_defines(env_name_from_config(config))

    out = CORES_DIR / config / args.tag
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    gen_link_ld(out, settings)
    gen_rvmodel_macros(out, settings, subsets, defines)
    gen_yaml(out, config, env, subsets)
    gen_sail(out, subsets)
    gen_config_mk(out, env, excludes)
    gen_test_config(out, args.tag, config, subsets)
    print(f"Generated {out}")


if __name__ == "__main__":
    main()
