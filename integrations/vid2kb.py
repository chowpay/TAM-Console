from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Vid2KBConfig:
    host: str = field(default_factory=lambda: os.environ.get("TAM_CONSOLE_VID2KB_HOST", ""))
    user: str = field(default_factory=lambda: os.environ.get("TAM_CONSOLE_VID2KB_USER", ""))
    key: Path = field(default_factory=lambda: Path(os.environ.get("TAM_CONSOLE_VID2KB_KEY", "")))
    output_root: str = field(default_factory=lambda: os.environ.get("TAM_CONSOLE_VID2KB_OUTPUT_ROOT", ""))


def run_is_safe(run_name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._-]+", run_name or ""))


def ssh(command: str, config: Vid2KBConfig | None = None, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    cfg = config or Vid2KBConfig()
    return subprocess.run(
        [
            "ssh",
            "-i",
            str(cfg.key),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            f"{cfg.user}@{cfg.host}",
            command,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def available_runs(config: Vid2KBConfig | None = None) -> list[str]:
    cfg = config or Vid2KBConfig()
    if not cfg.host or not cfg.user or not cfg.output_root or not cfg.key.exists():
        return []
    command = f"find {shlex.quote(cfg.output_root)} -mindepth 1 -maxdepth 1 -type d -printf '%f\\n' | sort"
    result = ssh(command, cfg, timeout=5)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if run_is_safe(line.strip())]


def read_file(run_name: str, relative_path: str, max_bytes: int = 120000, config: Vid2KBConfig | None = None) -> str:
    cfg = config or Vid2KBConfig()
    if not cfg.host or not cfg.user or not cfg.output_root or not cfg.key.exists():
        return ""
    if not run_is_safe(run_name):
        raise ValueError("Unsafe vid2kb run name.")
    safe_relative = relative_path.strip("/")
    run_relative = run_name + "/" + safe_relative
    command = (
        f"cd {shlex.quote(cfg.output_root)} && "
        f"test -f {shlex.quote(run_relative)} && "
        f"head -c {int(max_bytes)} {shlex.quote(run_relative)}"
    )
    result = ssh(command, cfg, timeout=10)
    if result.returncode != 0:
        return ""
    return result.stdout


def run_date(run_name: str, config: Vid2KBConfig | None = None) -> str:
    cfg = config or Vid2KBConfig()
    if not cfg.host or not cfg.user or not cfg.output_root or not cfg.key.exists():
        return ""
    if not run_is_safe(run_name):
        return ""
    manifest = f"{cfg.output_root}/{run_name}/docs_manifest.json"
    command = f"stat -c '%y' {shlex.quote(manifest)} 2>/dev/null | cut -d' ' -f1"
    result = ssh(command, cfg, timeout=5)
    date_text = result.stdout.strip()
    return date_text if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text) else ""


def source_root(run_name: str, config: Vid2KBConfig | None = None) -> str:
    cfg = config or Vid2KBConfig()
    if not cfg.host or not cfg.user or not cfg.output_root:
        return ""
    return f"ssh://{cfg.user}@{cfg.host}{cfg.output_root}/{run_name}"
