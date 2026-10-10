# Copyright Marc Ketel
# SPDX-License-Identifier: Apache-2.0
#
# Background generation queue: `make elfs` runs concurrently for pairs with
# different configs, and serially for pairs sharing one config.

import json
import logging
import queue
import subprocess
import threading
from pathlib import Path

from .store import REPO_DIR, pair_dir

LOG = logging.getLogger(__name__)

GENERATING = "generating"
READY = "ready"
FAILED = "failed"


class GenerationQueue:
    """Runs `make elfs` for each (config, tag) pair.

    Up to `workers` pairs run concurrently. Pairs that share a config are
    serialized with a per-config lock, because the framework builds into a
    work directory named after the config.
    """

    def __init__(self, workers: int = 4) -> None:
        self.status: dict[tuple, str] = {}
        self.error: dict[tuple, str] = {}
        self._queue: queue.Queue = queue.Queue()
        self._locks_guard = threading.Lock()
        self._config_locks: dict[str, threading.Lock] = {}
        for _ in range(max(1, workers)):
            threading.Thread(target=self._worker, daemon=True).start()

    def submit(self, config: str, tag: str, settings: dict) -> None:
        self.status[(config, tag)] = GENERATING
        self.error.pop((config, tag), None)
        self._queue.put((config, tag, settings))

    def _worker(self) -> None:
        LOG.info("generation worker started")
        while True:
            config, tag, settings = self._queue.get()
            LOG.info("generation: picked up %s/%s with settings %s", config, tag, settings)
            try:
                with self._config_lock(config):
                    self._generate(config, tag, settings)
                self.status[(config, tag)] = READY
                self.error.pop((config, tag), None)
                LOG.info("generation: %s/%s ready", config, tag)
            except Exception as exc:
                LOG.exception("generation: %s/%s failed", config, tag)
                self.status[(config, tag)] = FAILED
                self.error[(config, tag)] = str(exc)

    def _config_lock(self, config: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._config_locks.get(config)
            if lock is None:
                lock = threading.Lock()
                self._config_locks[config] = lock
            return lock

    def _generate(self, config: str, tag: str, settings: dict) -> None:
        pair = pair_dir(config, tag)
        LOG.info("generation: %s/%s pair dir %s", config, tag, pair)
        pair.mkdir(parents=True, exist_ok=True)
        settings_path = pair / "settings.json"
        settings_path.write_text(json.dumps(settings, indent=2, sort_keys=True))
        command = ["make", "elfs", f"CONFIG={config}", f"TAG={tag}", f"SETTINGS={settings_path}"]
        LOG.info("generation: %s/%s running %s in %s", config, tag, command, REPO_DIR)
        result = subprocess.run(
            command,
            cwd=REPO_DIR,
            capture_output=True,
            text=True,
        )
        LOG.info(
            "generation: %s/%s make returned %d, %d bytes stdout, %d bytes stderr",
            config,
            tag,
            result.returncode,
            len(result.stdout),
            len(result.stderr),
        )
        LOG.info("generation: %s/%s make stdout:\n%s", config, tag, result.stdout)
        if result.stderr:
            LOG.info("generation: %s/%s make stderr:\n%s", config, tag, result.stderr)
        if result.returncode != 0:
            (pair / "generation.log").write_text(result.stdout + result.stderr)
            raise RuntimeError(f"make elfs failed for {config}/{tag}")

    def failed(self, config: str, tag: str) -> bool:
        return self.status.get((config, tag)) == FAILED

    def generating(self, config: str, tag: str) -> bool:
        return self.status.get((config, tag)) == GENERATING

    def get_error(self, config: str, tag: str) -> str | None:
        return self.error.get((config, tag))
