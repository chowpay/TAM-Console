from __future__ import annotations

import os
import subprocess
from pathlib import Path


CLAUDE_CLI = Path(os.environ.get("TAM_CONSOLE_CLAUDE_CLI", "/usr/local/bin/claude-worker-cli"))
DEFAULT_TIMEOUT = int(os.environ.get("TAM_CONSOLE_CLAUDE_TIMEOUT", "240"))


def run_meeting_extraction(prompt: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    if not CLAUDE_CLI.exists():
        return False, f"Claude CLI not found at {CLAUDE_CLI}."

    system_prompt = (
        "You are TAM Console's meeting intelligence worker. Use only the extraction "
        "packet provided by the user. Do not use tools, browse, read files, or infer "
        "facts beyond the provided evidence. Return only valid JSON matching the "
        "requested schema; no markdown and no explanatory text outside the JSON."
    )
    cmd = [
        str(CLAUDE_CLI),
        "--print",
        "--output-format",
        "text",
        "--tools",
        "",
        "--system-prompt",
        system_prompt,
    ]
    max_budget = os.environ.get("TAM_CONSOLE_CLAUDE_MAX_BUDGET_USD", "").strip()
    if max_budget:
        cmd.extend(["--max-budget-usd", max_budget])

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"Claude extraction timed out after {timeout} seconds."
    except OSError as exc:
        return False, f"Claude extraction could not start: {exc}"

    output = (result.stdout or "").strip()
    if result.returncode != 0:
        error = (result.stderr or output or "Unknown Claude CLI error.").strip()
        return False, f"Claude extraction failed: {error}"
    if not output:
        return False, "Claude extraction returned no output."
    return True, output
