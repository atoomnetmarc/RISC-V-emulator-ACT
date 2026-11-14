#!/usr/bin/env python3
# Copyright Marc Ketel
# SPDX-License-Identifier: Apache-2.0
#
# Reset stored results and generated pairs while the work server runs.
#
# Usage: reset.py {results,failed,clean} [--config C] [--tag T]
#                 [--timeout SECONDS] [--act-dir DIR]
#
# Every mutation takes the per-config lock and then the per-pair lock, the
# same order the server uses. The lock files live in .locks/ and are never
# removed, so a reset cannot split a lock across two inodes.

import argparse
import shutil
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

from server.store import ACT_WORK_DIR, FAIL, Store, pair_dir  # noqa: E402

CORES_DIR = REPO_DIR / "config" / "cores" / "atoomnetmarc"


def pairs() -> list:
    found = []
    if not ACT_WORK_DIR.is_dir():
        return found
    for config_dir in sorted(ACT_WORK_DIR.iterdir()):
        if not config_dir.is_dir() or config_dir.name.startswith("."):
            continue
        for tag_dir in sorted(config_dir.iterdir()):
            if tag_dir.is_dir() and (tag_dir / "state.json").exists():
                found.append((config_dir.name, tag_dir.name))
    return found


def tags_of(config: str) -> list:
    config_dir = ACT_WORK_DIR / config
    if not config_dir.is_dir():
        return []
    return [
        path.name
        for path in sorted(config_dir.iterdir())
        if path.is_dir() and not path.name.startswith(".")
    ]


def reset_results(store: Store, config: str, tag: str, timeout: float) -> None:
    with store.config_lock(config, timeout), store.lock(config, tag, timeout):
        pair = pair_dir(config, tag)
        (pair / "state.json").unlink(missing_ok=True)
        (pair / "summary.log").unlink(missing_ok=True)
        shutil.rmtree(pair / "logs", ignore_errors=True)


def reset_failed(store: Store, config: str, tag: str, timeout: float) -> None:
    with store.config_lock(config, timeout), store.lock(config, tag, timeout):
        state = store.read_state(config, tag)
        state["tests"] = {
            test_id: entry
            for test_id, entry in state["tests"].items()
            if entry["state"] != FAIL
        }
        store.write_state(config, tag, state)
        pair = pair_dir(config, tag)
        (pair / "summary.log").unlink(missing_ok=True)
        shutil.rmtree(pair / "logs", ignore_errors=True)


def clean(store: Store, config: str, tag: str, timeout: float, act_dir: Path) -> None:
    with store.config_lock(config, timeout):
        locks = [store.lock(config, other, timeout) for other in tags_of(config)]
        try:
            for path in (
                ACT_WORK_DIR / config,
                ACT_WORK_DIR / "udb-xdg" / config,
                act_dir / "work" / config,
                act_dir / "work" / "udb-xdg" / config,
                CORES_DIR / config / tag,
            ):
                shutil.rmtree(path, ignore_errors=True)
        finally:
            for fh in locks:
                fh.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset ACT work state under the server locks")
    parser.add_argument("scope", choices=("results", "failed", "clean"))
    parser.add_argument("--config")
    parser.add_argument("--tag")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--act-dir",
        type=Path,
        default=REPO_DIR / "work" / "src" / "riscv-arch-test",
    )
    args = parser.parse_args()

    store = Store()
    try:
        if args.scope == "clean":
            if not (args.config and args.tag):
                parser.error("clean needs --config and --tag")
            clean(store, args.config, args.tag, args.timeout, args.act_dir)
            return

        targets = pairs()
        if args.config:
            targets = [pair for pair in targets if pair[0] == args.config]
        if args.tag:
            targets = [pair for pair in targets if pair[1] == args.tag]
        action = reset_results if args.scope == "results" else reset_failed
        for config, tag in targets:
            action(store, config, tag, args.timeout)
    except TimeoutError as exc:
        sys.exit(f"reset: {exc}")


if __name__ == "__main__":
    main()
