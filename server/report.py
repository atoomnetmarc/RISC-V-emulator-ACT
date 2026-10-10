# Copyright Marc Ketel
# SPDX-License-Identifier: Apache-2.0
#
# Per-instruction, cross-config failure report rendered from the state files
# and raw output logs.

import html
import re
import urllib.parse
from pathlib import Path

from .store import ACT_WORK_DIR, PASS, FAIL, pair_dir

# rv32i_m-I-add-01.elf -> add; rv32e-csr-01.elf -> csr
INSTRUCTION_RE = re.compile(r"^(?:rv\d+i_m-|rv\d+i-)(?P<instr>[A-Za-z0-9_]+?)-\d+$")


def instruction_of(test_id: str) -> str:
    stem = test_id.removesuffix(".elf")
    match = INSTRUCTION_RE.match(stem)
    return match.group("instr") if match else stem


def generation_error(config: str, tag: str) -> str | None:
    """Read generation.log if it exists, otherwise return None."""
    log_path = pair_dir(config, tag) / "generation.log"
    if log_path.exists():
        return log_path.read_text(errors="replace")
    return None


def collect(store) -> dict:
    """Per-instruction pass/fail counts across all (config, tag) pairs."""
    instructions: dict = {}
    generation_errors: dict = {}
    if not ACT_WORK_DIR.is_dir():
        return instructions, generation_errors
    for config_dir in sorted(ACT_WORK_DIR.iterdir()):
        if not config_dir.is_dir():
            continue
        for tag_dir in sorted(config_dir.iterdir()):
            state_path = tag_dir / "state.json"
            if not tag_dir.is_dir() or not state_path.exists():
                continue
            config, tag = config_dir.name, tag_dir.name
            err = generation_error(config, tag)
            if err:
                generation_errors[f"{config}/{tag}"] = err
            for test_id, entry in store.read_state(config, tag)["tests"].items():
                instr = instruction_of(test_id)
                stats = instructions.setdefault(
                    instr, {"pass": 0, "fail": 0, "failures": []}
                )
                if entry["state"] == PASS:
                    stats["pass"] += 1
                elif entry["state"] == FAIL:
                    stats["fail"] += 1
                    excerpt = store.log_excerpt(config, tag, test_id)
                    stats["failures"].append(
                        {
                            "config": config,
                            "tag": tag,
                            "test_id": test_id,
                            "exit_status": entry.get("exit_status"),
                            "excerpt": excerpt,
                        }
                    )
    return instructions, generation_errors


def render_report(store) -> str:
    instructions, generation_errors = collect(store)
    total_pass = sum(s["pass"] for s in instructions.values())
    total = total_pass + sum(s["fail"] for s in instructions.values())
    percentage = 100.0 * total_pass / total if total else 0.0

    rows = []
    for instr in sorted(instructions):
        stats = instructions[instr]
        failures = []
        for failure in stats["failures"]:
            excerpt = html.escape(failure["excerpt"])
            query = urllib.parse.urlencode(
                {
                    "config": failure["config"],
                    "tag": failure["tag"],
                    "test_id": failure["test_id"],
                }
            )
            failures.append(
                f"<div class='failure'>"
                f"<span class='env'>{html.escape(failure['config'])}/"
                f"{html.escape(failure['tag'])}</span> "
                f"<span class='test'>{html.escape(failure['test_id'])}</span> "
                f"exit {failure['exit_status']} "
                f"<a href='/log?{query}'>full log</a>"
                f"<pre>{excerpt}</pre></div>"
            )
        status = "ok" if stats["fail"] == 0 else "bad"
        total = stats['pass'] + stats['fail']
        body = "".join(failures) or "<p class='muted'>All tests passed.</p>"
        rows.append(
            f"<details class='instr {status}'>"
            f"<summary>{html.escape(instr)} <span class='counts'><span class='pass'>pass: {stats['pass']}</span><span class='fail'>fail: {stats['fail']}</span><span>total: {total}</span></span></summary>{body}</details>"
        )

    gen_error_rows = []
    for pair, err in sorted(generation_errors.items()):
        excerpt = html.escape(err)
        query = urllib.parse.urlencode({"config": pair.split("/")[0], "tag": pair.split("/")[1]})
        gen_error_rows.append(
            f"<details class='instr bad'>"
            f"<summary>{html.escape(pair)}: generation failed</summary>"
            f"<div class='failure'>"
            f"<pre>{excerpt}</pre>"
            f"<a href='/generation-log?{query}'>full generation log</a>"
            f"</div></details>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>ACT report</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<h1>Cross-config failure report</h1>
<p>Overall: {total_pass}/{total} passed ({percentage:.1f}%).</p>
{''.join(gen_error_rows) or ''}
{''.join(rows) or '<p>No results recorded.</p>'}
</body>
</html>"""
