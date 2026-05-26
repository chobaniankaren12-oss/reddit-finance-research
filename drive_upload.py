"""Upload a local file to Google Drive via the user's rclone gdrive remote.

Why rclone CLI rather than raw Drive API: rclone bundles client_id/secret,
handles token refresh, scope, retries, and chunked uploads. Reimplementing
that with `requests` adds fragility for no gain in our usage pattern.

Key property: re-uploading a file with the same name inside the same Drive
folder PATCHes the existing file by content — the Drive file-ID and any
shareable URL stay the same. So a daily report that always lands at
`gdrive:проекты/reddit_parser/reddit_research_<date>.xlsx` keeps its link
stable across runs on the same day.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

DRIVE_FOLDER = "gdrive:проекты/reddit_parser"
RCLONE_REMOTE = "gdrive"


def _ensure_rclone() -> None:
    if shutil.which("rclone") is None:
        raise RuntimeError(
            "rclone not in PATH. Install via `brew install rclone` "
            "and configure with `rclone config` (gdrive remote)."
        )


def _ensure_remote() -> None:
    """Confirm the gdrive remote exists in rclone config."""
    result = subprocess.run(
        ["rclone", "listremotes"],
        capture_output=True, text=True, check=True,
    )
    if f"{RCLONE_REMOTE}:" not in result.stdout:
        raise RuntimeError(
            f"rclone remote '{RCLONE_REMOTE}:' not configured. "
            "Run `rclone config` to set it up."
        )


def _ensure_folder() -> None:
    """Create the destination folder on Drive if missing. `mkdir` is idempotent."""
    result = subprocess.run(
        ["rclone", "mkdir", DRIVE_FOLDER],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip().lower()
        # Some rclone versions return non-zero with this message even when ok
        if "already exists" not in stderr:
            logger.warning("rclone mkdir non-fatal: %s", result.stderr.strip())


def push_to_drive(local_path: Path, replace: bool = True) -> str:
    """Upload `local_path` to the configured Drive folder.

    Returns: a Drive shareable URL on success, or the dest path string if
    no link could be generated.

    If `replace=True` and a file with the same name already exists, rclone
    PATCHes its content in-place (file-ID and URL preserved).
    """
    _ensure_rclone()
    _ensure_remote()
    _ensure_folder()

    if not local_path.exists():
        raise FileNotFoundError(local_path)

    dest = f"{DRIVE_FOLDER}/{local_path.name}"
    logger.info("Uploading %s → %s", local_path, dest)

    cmd = ["rclone", "copyto", str(local_path), dest, "--progress"]
    if replace:
        # Force re-upload even if rclone would skip due to identical size.
        cmd.append("--ignore-times")
    result = subprocess.run(cmd, capture_output=False, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"rclone copyto exited with code {result.returncode}")

    # Get shareable link
    link_result = subprocess.run(
        ["rclone", "link", dest],
        capture_output=True, text=True, check=False,
    )
    if link_result.returncode == 0 and link_result.stdout.strip():
        link = link_result.stdout.strip()
        logger.info("✓ Drive link: %s", link)
        return link
    logger.warning(
        "rclone link failed (%s) — file is uploaded at %s but no link returned",
        link_result.stderr.strip(), dest,
    )
    return dest
