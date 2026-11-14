# Copyright Marc Ketel
# SPDX-License-Identifier: Apache-2.0
#
# Queue state: one state file per (config, tag) pair next to summary.log.

import base64
import fcntl
import json
import logging
import re
import subprocess
import tempfile
import time
from pathlib import Path

LOG = logging.getLogger(__name__)

REPO_DIR = Path(__file__).resolve().parent.parent
# The container mounts this repository's work dir at the framework clone's
# WORKDIR root, so all per-pair state lives in work/<CONFIG>/<TAG>/.
ACT_WORK_DIR = REPO_DIR / "work"
# Lock files live outside work/ so a framework clean cannot remove them and
# split a lock across two inodes.
LOCK_DIR = REPO_DIR / ".locks"
OBJCOPY = "riscv64-unknown-elf-objcopy"

# RVCP-SUMMARY: TEST PASSED / RVCP-SUMMARY: TEST FAILED
VERDICT_RE = re.compile(r"RVCP-SUMMARY: TEST (PASSED|FAILED)")

PENDING = "pending"
CLAIMED = "claimed"
PASS = "pass"
FAIL = "fail"

DEFAULT_LEASE_SECONDS = 30


def pair_dir(config: str, tag: str) -> Path:
    return ACT_WORK_DIR / config / tag


class PairError(Exception):
    """Generation for the pair failed."""


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"tests": {}}


def parse_verdict(output: str) -> str | None:
    match = VERDICT_RE.search(output)
    return match.group(1) if match else None


def elf_to_binary(elf: Path) -> bytes:
    # objcopy cannot stream to /dev/stdout (not seekable), so use a temp file.
    with tempfile.NamedTemporaryFile() as tmp:
        result = subprocess.run([OBJCOPY, "-O", "binary", str(elf), tmp.name], capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(f"objcopy failed: {result.stderr.decode(errors='replace').strip()}")
        return Path(tmp.name).read_bytes()


class Store:
    """State file, summary.log, and raw output logs for all pairs."""

    def _lock_path(self, config: str, tag: str) -> Path:
        return LOCK_DIR / config / f"{tag}.lock"

    def _config_lock_path(self, config: str) -> Path:
        return LOCK_DIR / config / ".config.lock"

    def _acquire(self, path: Path, timeout: float | None):
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "w")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fh
            except BlockingIOError:
                if deadline is not None and time.monotonic() >= deadline:
                    fh.close()
                    raise TimeoutError(f"lock busy: {path}")
                time.sleep(0.1)

    def lock(self, config: str, tag: str, timeout: float | None = None):
        return self._acquire(self._lock_path(config, tag), timeout)

    def config_lock(self, config: str, timeout: float | None = None):
        return self._acquire(self._config_lock_path(config), timeout)

    def state_path(self, config: str, tag: str) -> Path:
        return pair_dir(config, tag) / "state.json"

    def read_state(self, config: str, tag: str) -> dict:
        return load_state(self.state_path(config, tag))

    def write_state(self, config: str, tag: str, state: dict) -> None:
        path = self.state_path(config, tag)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
        tmp.replace(path)

    def generation_path(self, config: str, tag: str) -> Path:
        return pair_dir(config, tag) / "generation.json"

    def read_generation(self, config: str, tag: str) -> dict | None:
        try:
            return json.loads(self.generation_path(config, tag).read_text())
        except (OSError, ValueError):
            return None

    def write_generation(
        self,
        config: str,
        tag: str,
        status: str,
        error: str | None = None,
        settings: dict | None = None,
    ) -> None:
        path = self.generation_path(config, tag)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"status": status}
        if error is not None:
            payload["error"] = error
        if settings is not None:
            payload["settings"] = settings
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(path)

    def read_settings(self, config: str, tag: str) -> dict | None:
        try:
            return json.loads((pair_dir(config, tag) / "settings.json").read_text())
        except (OSError, ValueError):
            return None

    def test_ids(self, config: str, tag: str) -> list:
        elf_dir = pair_dir(config, tag) / "elfs"
        if not elf_dir.is_dir():
            return []
        # Test ids are ELF paths relative to the elfs dir (the framework
        # nests them as elfs/<suite>/<extension>/<name>.elf).
        return sorted(p.relative_to(elf_dir).as_posix() for p in elf_dir.rglob("*.elf"))

    def new_tests(self, config: str, tag: str) -> list:
        """Test ids from the ELF directory that are not in the state file yet."""
        known = self.read_state(config, tag)["tests"]
        return [t for t in self.test_ids(config, tag) if t not in known]

    def claim(self, config: str, tag: str, lease_seconds: int) -> dict:
        """Hand out the next unclaimed test, or report ready/done.

        Returns {"ready": False}, {"done": True}, or
        {"test_id": ..., "binary": <base64>}.
        Raises PairError when generation failed.
        """
        with self.lock(config, tag):
            state = self.read_state(config, tag)
            tests = state["tests"]
            new = self.new_tests(config, tag)
            if new:
                LOG.info("claim: %s/%s registering %d new tests from the elfs dir", config, tag, len(new))
                for test_id in new:
                    tests[test_id] = {"state": PENDING}
                self.write_state(config, tag, state)
            now = time.time()
            for test_id, entry in sorted(tests.items()):
                if entry["state"] == CLAIMED and entry.get("claim_expiry", 0) <= now:
                    LOG.info("claim: %s/%s lease expired for %s, back to pending", config, tag, test_id)
                    entry["state"] = PENDING
            pending = [
                test_id
                for test_id, entry in sorted(tests.items())
                if entry["state"] == PENDING
            ]
            if not pending:
                claimed = any(entry["state"] == CLAIMED for entry in tests.values())
                if claimed:
                    LOG.info("claim: %s/%s no pending tests, another client still holds claims", config, tag)
                    return {"ready": False}
                if tests and all(entry["state"] in (PASS, FAIL) for entry in tests.values()):
                    LOG.info("claim: %s/%s all %d tests done", config, tag, len(tests))
                    return {"done": True}
                LOG.info("claim: %s/%s empty or unfinished state (%d tests), nothing pending", config, tag, len(tests))
                return {"ready": False}
            test_id = pending[0]
            elf = pair_dir(config, tag) / "elfs" / test_id
            binary = elf_to_binary(elf)
            tests[test_id] = {
                "state": CLAIMED,
                "claim_expiry": now + lease_seconds,
            }
            self.write_state(config, tag, state)
            LOG.info("claim: %s/%s handed out %s (%d pending left)", config, tag, test_id, len(pending) - 1)
            return {"test_id": test_id, "binary": base64.b64encode(binary).decode("ascii")}

    def record_result(
        self, config: str, tag: str, test_id: str, output: str, exit_status: int
    ) -> None:
        """Parse the verdict and write state, summary line, and raw output
        inside one flock section. Idempotent: last write wins."""
        verdict = parse_verdict(output)
        state_name = PASS if verdict == "PASSED" else FAIL
        LOG.info("result: %s/%s test %s verdict %s", config, tag, test_id, verdict)
        with self.lock(config, tag):
            state = self.read_state(config, tag)
            state["tests"][test_id] = {
                "state": state_name,
                "verdict": verdict,
                "exit_status": exit_status,
            }
            self.write_state(config, tag, state)
            pair = pair_dir(config, tag)
            pair.mkdir(parents=True, exist_ok=True)
            with open(pair / "summary.log", "a") as fh:
                fh.write(f"{test_id} {state_name}\n")
            log_dir = pair / "logs"
            log_dir.mkdir(exist_ok=True)
            (log_dir / (test_id.replace("/", "__"))).with_suffix(".log").write_text(output)

    def counts(self, config: str, tag: str) -> dict:
        with self.lock(config, tag):
            tests = self.read_state(config, tag)["tests"].values()
        counts = {PENDING: 0, CLAIMED: 0, PASS: 0, FAIL: 0}
        for entry in tests:
            counts[entry["state"]] = counts.get(entry["state"], 0) + 1
        return counts

    def tests(self, config: str, tag: str) -> dict:
        """Test id -> state name for every registered test of the pair."""
        with self.lock(config, tag):
            return {
                test_id: entry["state"]
                for test_id, entry in self.read_state(config, tag)["tests"].items()
            }

    def log_text(self, config: str, tag: str, test_id: str) -> str | None:
        """Full raw output log for one test, or None when it has none yet."""
        path = self.log_path(config, tag, test_id)
        if not path.exists():
            return None
        return path.read_text(errors="replace")

    def log_excerpt(self, config: str, tag: str, test_id: str, lines: int = 20) -> str:
        path = self.log_path(config, tag, test_id)
        if not path.exists():
            return ""
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])

    @staticmethod
    def log_path(config: str, tag: str, test_id: str) -> Path:
        # record_result stores logs as <id without .elf, / -> __>.log
        stem = test_id.replace("/", "__").removesuffix(".elf")
        return pair_dir(config, tag) / "logs" / f"{stem}.log"
