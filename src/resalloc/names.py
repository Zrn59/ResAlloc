"""Path pieces that are allowed to become directories on the hub."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import Path

# Job directories are created from this string. Anything else could escape jobs/.
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_DIGEST = re.compile(r"^[0-9a-fA-F]{64}$")


def safe_job_id(value: str) -> bool:
    return bool(_JOB_ID.fullmatch(value))


# Kaggle slugs are `resalloc-code-` plus this id, and the slug limit is 50.
_JOB_FOLDER_LIMIT = 36


def folder_stem(label: str) -> str:
    """Turn a script path or a title into a directory-friendly word."""

    text = Path(str(label).strip()).name
    if text.lower().endswith(".py"):
        text = text[:-3]
    cleaned = [char if char.isascii() and char.isalnum() else "-" for char in text.lower()]
    slug = "".join(cleaned).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "job"


def job_folder_name(label: str, now: datetime | None = None, suffix: str | None = None) -> str:
    """`train-2026-10-31-18-11-a3f9`. The clock is the machine that submits the job."""

    moment = now or datetime.now().astimezone()
    stamp = moment.strftime("%Y-%m-%d-%H-%M")
    tag = (suffix or uuid.uuid4().hex[:4]).lower()
    if not re.fullmatch(r"[0-9a-f]{4}", tag):
        raise ValueError("job suffix must be 4 hex characters")
    tail = f"{stamp}-{tag}"
    room = _JOB_FOLDER_LIMIT - len(tail) - 1
    stem = folder_stem(label)[:room].strip("-") or "job"
    name = f"{stem}-{tail}"
    if not safe_job_id(name):
        raise ValueError(f"unsafe job folder name: {name}")
    return name


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
