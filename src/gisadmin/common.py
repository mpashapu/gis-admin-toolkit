"""Shared helpers: status levels, exit codes, logging and report writers."""

from __future__ import annotations

import csv
import getpass
import json
import logging
import os
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path


class Status(IntEnum):
    """Ordered so the worst status wins. Values double as process exit codes,
    following the Nagios convention most monitoring tools understand."""

    OK = 0
    WARNING = 1
    CRITICAL = 2
    ERROR = 3  # couldn't check at all (connection refused, DNS, auth)


def worst(statuses: Iterable[Status]) -> Status:
    return max(statuses, default=Status.OK)


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    # urllib3 is noisy at DEBUG and can log full URLs
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp() -> str:
    return utc_now().strftime("%Y-%m-%d %H:%M UTC")


def write_csv(path: str | Path, rows: Sequence[dict], fieldnames: Sequence[str]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: str | Path, data: object) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, default=str)


def format_table(rows: Sequence[dict], columns: Sequence[str]) -> str:
    """Plain-text table for console output."""
    if not rows:
        return "(no rows)"
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    lines = ["  ".join(c.upper().ljust(widths[c]) for c in columns)]
    lines.append("  ".join("-" * widths[c] for c in columns))
    for row in rows:
        lines.append("  ".join(str(row.get(c, "")).ljust(widths[c]) for c in columns))
    return "\n".join(lines)


def read_lines(path: str | Path) -> list[str]:
    """Non-empty lines from a text file, ignoring # comments."""
    lines = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def resolve_password(env_var: str, username: str) -> str:
    """Read a password from an environment variable, else prompt.

    Passwords are never accepted as command-line arguments, because those end
    up in shell history and process listings.
    """
    password = os.environ.get(env_var)
    if password:
        return password
    return getpass.getpass(f"Password for {username}: ")
