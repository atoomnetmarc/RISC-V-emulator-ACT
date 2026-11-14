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

from .store import ACT_WORK_DIR, REPO_DIR, Store, pair_dir

LOG = logging.getLogger(__name__)

GENERATING = "generating"
READY = "ready"
FAILED = "failed"


class GenerationQueue:
    """Runs `make elfs` for each (config, tag) pair.

    Up to `workers` pairs run concurrently. Pairs that share a config are
    serialized with the per-config file lock, because the framework builds
    into a work directory named after the config. The status lives in
    generation.json next to the pair state, so a reset on disk resets it.
    """

    def __init__(self, workers: int = 4) -> None:
        self.store = Store()
        self._queue: queue.Queue = queue.Queue()
        self._recover()
        for _ in range(max(1, workers)):
            threading.Thread(target=self._worker, daemon=True).start()

    def _recover(self) -> None:
        """Mark pairs left generating by a server that stopped as failed."""
        if not ACT_WORK_DIR.is_dir():
            return
        for path in ACT_WORK_DIR.glob("*/*/generation.json"):
            config, tag = path.parent.parent.name, path.parent.name
            data = self.store.read_generation(config, tag)
            if data and data.get("status") == GENERATING:
                with self.store.lock(config, tag):
                    self.store.write_generation(
                        config,
                        tag,
                        FAILED,
                        error="server restarted during generation",
                        settings=data.get("settings"),
                    )

    def submit(self, config: str, tag: str, settings: dict) -> None:
        with self.store.lock(config, tag):
            self.store.write_generation(config, tag, GENERATING, settings=settings)
        self._queue.put((config, tag, settings))

    def _worker(self) -> None:
        LOG.info("generation worker started")
        while True:
            config, tag, settings = self._queue.get()
            LOG.info("generation: picked up %s/%s with settings %s", config, tag, settings)
            try:
                self._generate(config, tag, settings)
                LOG.info("generation: %s/%s ready", config, tag)
            except Exception as exc:
                LOG.exception("generation: %s/%s failed", config, tag)
                with self.store.lock(config, tag):
                    self.store.write_generation(
                        config, tag, FAILED, error=str(exc), settings=settings
                    )

    def _generate(self, config: str, tag: str, settings: dict) -> None:
        pair = pair_dir(config, tag)
        pair.mkdir(parents=True, exist_ok=True)
        settings_path = pair / "settings.json"
        settings_path.write_text(json.dumps(settings, indent=2, sort_keys=True))
        with self.store.config_lock(config):
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
            (pair / "generation.log").unlink(missing_ok=True)
            with self.store.lock(config, tag):
                self.store.write_generation(config, tag, READY, settings=settings)

    def _status(self, config: str, tag: str) -> dict:
        return self.store.read_generation(config, tag) or {}

    def failed(self, config: str, tag: str) -> bool:
        return self._status(config, tag).get("status") == FAILED

    def generating(self, config: str, tag: str) -> bool:
        return self._status(config, tag).get("status") == GENERATING

    def get_error(self, config: str, tag: str) -> str | None:
        return self._status(config, tag).get("error")
