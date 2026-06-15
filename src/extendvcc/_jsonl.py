"""Vendored JSONL append helper."""

from __future__ import annotations

import json
import os
from pathlib import Path


def append_jsonl(path: Path, rows: list[dict], *, fsync: bool = False) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create the file owner-only (0600) regardless of umask. The ledger stores
    # card ids/names/last4 — no PAN/CVC — but on shared machines a world-readable
    # first write would still leak that local audit trail. Existing files are
    # opened append-only and never have their mode altered.
    is_new = not path.exists()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if is_new:
            # O_CREAT's mode is masked by umask; force 0600 so umask cannot widen it.
            os.chmod(path, 0o600)
        f = os.fdopen(fd, "a", encoding="utf-8")
    except BaseException:
        # os.fdopen/os.chmod failing leaves the raw fd open — close it before raising.
        os.close(fd)
        raise
    with f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True, ensure_ascii=False))
            f.write("\n")
        if fsync:
            f.flush()
            os.fsync(f.fileno())
