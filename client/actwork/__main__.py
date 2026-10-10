#!/usr/bin/env python3
# Copyright Marc Ketel
# SPDX-License-Identifier: Apache-2.0
#
# DUT work client: reads the emulator ini, posts the batch, claims tests,
# runs them on the DUT, and posts raw output back.

import argparse
import base64
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .api import ApiError, post
from .ini import select_configs

def run_dut(run_template: str, env_name: str, binary_path: Path, timeout: int) -> tuple:
    """Run the DUT with the claimed binary. Returns (output, exit_status)."""
    command = run_template.format(env=env_name, binary=binary_path)
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout + result.stderr, result.returncode
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        return output, 124


def process_pair(base_url: str, config: str, tag: str, env_name: str, run_template: str, lease: int, poll_seconds: float) -> None:
    while True:
        try:
            response = post(base_url, "/claim", {"config": config, "tag": tag, "lease_seconds": lease}, retries=True)
        except ApiError as exc:
            print(f"{config}/{tag}: {exc}; stopping this pair")
            return
        if response.get("done"):
            print(f"{config}/{tag}: done")
            return
        if response.get("ready") is False:
            time.sleep(poll_seconds)
            continue
        test_id = response["test_id"]
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as fh:
            fh.write(base64.b64decode(response["binary"]))
            binary_path = Path(fh.name)
        try:
            output, exit_status = run_dut(run_template, env_name, binary_path, lease)
            post(
                base_url,
                "/result",
                {"test_id": test_id, "config": config, "tag": tag, "output": output, "exit_status": exit_status},
                retries=True,
            )
            verdict = "PASSED" if "RVCP-SUMMARY: TEST PASSED" in output else "FAILED"
            print(f"{config}/{tag}: {test_id} {verdict}")
        finally:
            binary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="ACT DUT work client")
    parser.add_argument("--server", required=True, help="work server base URL")
    parser.add_argument("--ini", required=True, type=Path, help="emulator platformio_isa-extension-combination_env.ini")
    parser.add_argument("--full", action="store_true", help="run every environment in the ini")
    parser.add_argument("--smoke", action="store_true", help="run the '# smoke'-marked environments")
    parser.add_argument("--tag", required=True, help="compiler tag (e.g. gcc, clang, avr)")
    parser.add_argument("--load-base", required=True, help="generation load base (e.g. 0x80000000)")
    parser.add_argument("--halt-address", required=True, help="generation halt address (e.g. 0x20000000)")
    parser.add_argument("--run", required=True, help="DUT run command template with {env} and {binary} placeholders")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1, help="concurrent workers per config")
    parser.add_argument("--lease-seconds", type=int, default=30)
    parser.add_argument("filter", nargs="?", default=".", help="regex applied after subset selection")
    args = parser.parse_args()

    if args.full == args.smoke:
        parser.error("exactly one of --full or --smoke is required")

    mode = "full" if args.full else "smoke"
    configs = select_configs(args.ini, mode, args.filter)
    base_url = args.server.rstrip("/")

    # One batch per config: the settings carry that config's firmware env,
    # and a batch post is an idempotent merge keyed by (config, tag).
    for isa, env_name in configs:
        settings = {
            "load_base": args.load_base,
            "halt_address": args.halt_address,
            "firmware_env": env_name,
        }
        post(
            base_url,
            "/batch",
            {
                "settings": settings,
                "configs": [{"config": isa, "tag": args.tag}],
                "lease_seconds": args.lease_seconds,
            },
            retries=True,
        )
    print(f"Posted batch: {len(configs)} configs under tag {args.tag}")

    for isa, env_name in configs:
        workers = [
            threading.Thread(
                target=process_pair,
                args=(base_url, isa, args.tag, env_name, args.run, args.lease_seconds, 2.0),
            )
            for _ in range(args.jobs)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()


if __name__ == "__main__":
    main()
