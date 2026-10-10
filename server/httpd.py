# Copyright Marc Ketel
# SPDX-License-Identifier: Apache-2.0
#
# HTTP API and page handlers.

import json
import logging
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from .generation import GenerationQueue
from .report import instruction_of, render_report
from .store import Store, DEFAULT_LEASE_SECONDS, pair_dir, ACT_WORK_DIR, PASS, FAIL

LOG = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"
SETTINGS_KEYS = {"load_base", "halt_address"}
OPTIONAL_SETTINGS_KEYS = {"firmware_env"}
# Test ids are ELF paths relative to the pair's elfs dir.
TEST_ID_RE = re.compile(r"^[A-Za-z0-9_./-]+\.elf$")


def validate_settings(settings: dict) -> str | None:
    """Returns an error message, or None when the settings are complete."""
    if not isinstance(settings, dict):
        return "settings must be an object"
    missing = SETTINGS_KEYS - settings.keys()
    if missing:
        return f"settings missing required keys: {', '.join(sorted(missing))}"
    unknown = settings.keys() - SETTINGS_KEYS - OPTIONAL_SETTINGS_KEYS
    if unknown:
        return f"settings has unknown keys: {', '.join(sorted(unknown))}"
    if not all(isinstance(settings[k], str) for k in settings):
        return "settings values must be strings"
    return None


class Handler(BaseHTTPRequestHandler):
    store = Store()
    generation = GenerationQueue()
    # settings per tag, for last-write-wins invalidation
    tag_settings: dict = {}

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code: int, body: str) -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, code: int, body: str) -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length))

    def do_GET(self):  # noqa: N802 - stdlib signature
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if route == "/":
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif route == "/static/style.css":
            self._send_file(STATIC_DIR / "style.css", "text/css; charset=utf-8")
        elif route == "/report":
            self._send_html(200, render_report(self.store))
        elif route == "/openapi.yaml":
            self._send_file(STATIC_DIR / "openapi.yaml", "text/yaml; charset=utf-8")
        elif route == "/status":
            self._handle_status(query)
        elif route == "/tests":
            self._handle_tests(query)
        elif route == "/log":
            self._handle_log(query)
        elif route == "/generation-log":
            self._handle_generation_log(query)
        else:
            self.send_error(404)

    def _handle_status(self, query: dict) -> None:
        configs = query.get("config", [])
        tags = query.get("tag", [])
        pairs = {}
        for config, tag in self._known_pairs():
            if configs and config not in configs:
                continue
            if tags and tag not in tags:
                continue
            counts = self.store.counts(config, tag)
            if self.generation.failed(config, tag):
                counts["generation"] = "failed"
                error = self.generation.get_error(config, tag)
                if error:
                    counts["generation_error"] = error
            elif self.generation.generating(config, tag):
                counts["generation"] = "generating"

            # Per-instruction pass/fail counts
            instr_counts = {}
            state = self.store.read_state(config, tag)
            for test_id, entry in state["tests"].items():
                instr = instruction_of(test_id)
                ic = instr_counts.setdefault(instr, {"pass": 0, "fail": 0, "total": 0})
                ic["total"] += 1
                if entry["state"] == PASS:
                    ic["pass"] += 1
                elif entry["state"] == FAIL:
                    ic["fail"] += 1
            counts["instructions"] = instr_counts

            pairs[f"{config}/{tag}"] = counts
        self._send_json(200, {"pairs": pairs})

    def _handle_tests(self, query: dict) -> None:
        config = query.get("config", [""])[0]
        tag = query.get("tag", [""])[0]
        if not config or not tag:
            self._send_json(400, {"error": "config and tag are required"})
            return
        self._send_json(200, {"tests": self.store.tests(config, tag)})

    def _handle_log(self, query: dict) -> None:
        config = query.get("config", [""])[0]
        tag = query.get("tag", [""])[0]
        test_id = query.get("test_id", [""])[0]
        # The test id becomes a file name; keep it to a safe shape.
        if not (config and tag and test_id) or ".." in test_id or not TEST_ID_RE.match(test_id):
            self._send_json(400, {"error": "config, tag, and a valid test_id are required"})
            return
        text = self.store.log_text(config, tag, test_id)
        if text is None:
            self.send_error(404)
            return
        self._send_text(200, text)

    def _handle_generation_log(self, query: dict) -> None:
        config = query.get("config", [""])[0]
        tag = query.get("tag", [""])[0]
        if not config or not tag:
            self._send_json(400, {"error": "config and tag are required"})
            return
        pair = pair_dir(config, tag)
        log_path = pair / "generation.log"
        if not log_path.exists():
            self.send_error(404)
            return
        self._send_text(200, log_path.read_text(errors="replace"))

    def _known_pairs(self) -> list:
        pairs = list(self.generation.status.keys())
        if ACT_WORK_DIR.is_dir():
            for config_dir in sorted(ACT_WORK_DIR.iterdir()):
                if not config_dir.is_dir():
                    continue
                for tag_dir in sorted(config_dir.iterdir()):
                    if tag_dir.is_dir() and (tag_dir / "state.json").exists():
                        pair = (config_dir.name, tag_dir.name)
                        if pair not in pairs:
                            pairs.append(pair)
        return pairs

    def do_POST(self):  # noqa: N802 - stdlib signature
        route = urllib.parse.urlparse(self.path).path
        try:
            payload = self._read_json()
            if route == "/batch":
                self._handle_batch(payload)
            elif route == "/claim":
                self._handle_claim(payload)
            elif route == "/result":
                self._handle_result(payload)
            else:
                self.send_error(404)
        except (KeyError, TypeError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})

    def _handle_batch(self, payload: dict) -> None:
        error = validate_settings(payload.get("settings"))
        if error:
            LOG.warning("batch rejected: %s", error)
            self._send_json(400, {"error": error})
            return
        settings = payload["settings"]
        configs = payload.get("configs")
        if not isinstance(configs, list) or not configs:
            LOG.warning("batch rejected: configs must be a non-empty list")
            self._send_json(400, {"error": "configs must be a non-empty list"})
            return
        for entry in configs:
            if not isinstance(entry, dict) or "config" not in entry or "tag" not in entry:
                LOG.warning("batch rejected: bad config entry %r", entry)
                self._send_json(400, {"error": "each config entry needs config and tag"})
                return
        for entry in configs:
            config, tag = entry["config"], entry["tag"]
            if (config, tag) in self.generation.status:
                # Known pair: idempotent merge. Different settings under the
                # same tag invalidate the generated binaries.
                if self.tag_settings.get(tag) != settings:
                    LOG.info("batch: settings changed for %s/%s, invalidating pair", config, tag)
                    self._invalidate(config, tag)
                    self.tag_settings[tag] = settings
                    self.generation.submit(config, tag, settings)
                else:
                    LOG.info("batch: %s/%s already known with same settings, merge is a no-op", config, tag)
                continue
            LOG.info("batch: new pair %s/%s, submitting generation with settings %s", config, tag, settings)
            self.tag_settings[tag] = settings
            self.generation.submit(config, tag, settings)
        self._send_json(200, {"accepted": True})

    def _invalidate(self, config: str, tag: str) -> None:
        import shutil

        shutil.rmtree(pair_dir(config, tag), ignore_errors=True)
        shutil.rmtree(Path(__file__).resolve().parent.parent / "config" / "cores" / "atoomnetmarc" / config / tag, ignore_errors=True)

    def _handle_claim(self, payload: dict) -> None:
        config, tag = payload["config"], payload["tag"]
        lease = payload.get("lease_seconds", DEFAULT_LEASE_SECONDS)
        if self.generation.failed(config, tag):
            LOG.warning("claim: generation failed for %s/%s", config, tag)
            self._send_json(500, {"error": f"generation failed for {config}/{tag}"})
            return
        if self.generation.generating(config, tag):
            LOG.info("claim: %s/%s still generating", config, tag)
            self._send_json(200, {"ready": False})
            return
        try:
            response = self.store.claim(config, tag, lease)
            LOG.info("claim: %s/%s -> %s", config, tag, response.get("test_id", "no test"))
            self._send_json(200, response)
        except RuntimeError as exc:
            LOG.exception("claim: %s/%s raised", config, tag)
            self._send_json(500, {"error": str(exc)})

    def _handle_result(self, payload: dict) -> None:
        LOG.info(
            "result: %s/%s test %s exit_status %s output %d bytes",
            payload["config"],
            payload["tag"],
            payload["test_id"],
            payload.get("exit_status", 1),
            len(payload.get("output", "")),
        )
        self.store.record_result(
            payload["config"],
            payload["tag"],
            payload["test_id"],
            payload["output"],
            payload.get("exit_status", 1),
        )
        self._send_json(200, {"recorded": True})
