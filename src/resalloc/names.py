"""Path pieces that are allowed to become directories on the hub."""

from __future__ import annotations

import re
from pathlib import Path

# Job directories are created from this string. Anything else could escape jobs/.
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_DIGEST = re.compile(r"^[0-9a-fA-F]{64}$")


def safe_job_id(value: str) -> bool:
    return bool(_JOB_ID.fullmatch(value))


def safe_digest(value: str) -> bool:
    return bool(_DIGEST.fullmatch(value))


def safe_segment(value: str) -> bool:
    """One directory name. No slashes, no parent segments, no control characters."""

    if not value or len(value) > 200 or value in {".", ".."}:
        return False
    if "/" in value or "\\" in value or "\x00" in value:
        return False
    return not any(ord(char) < 32 for char in value)


def safe_relative(value: str) -> bool:
    """A file inside a job bundle. Absolute paths and .. are refused."""

    if not value or len(value) > 300 or value.startswith("/") or "\\" in value or "\x00" in value:
        return False
    if any(ord(char) < 32 for char in value):
        return False
    return ".." not in Path(value).parts
