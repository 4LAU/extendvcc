"""Vendored JSONL append helper."""
from __future__ import annotations
import json
import os
from pathlib import Path

def append_jsonl(path: Path, rows: list[dict], *, fsync: bool = False) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True, ensure_ascii=False))
            f.write("\n")
        if fsync:
            f.flush()
            os.fsync(f.fileno())
