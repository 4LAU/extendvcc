"""Lazy path resolution with CLI override, env var, and default fallback."""

from __future__ import annotations

import os
from pathlib import Path

_state_dir_override = None
_ledger_path_override = None


def configure(*, state_dir=None, ledger_path=None):
    global _state_dir_override, _ledger_path_override
    _state_dir_override = Path(state_dir) if state_dir else None
    _ledger_path_override = Path(ledger_path) if ledger_path else None


def state_dir() -> Path:
    return _state_dir_override or Path(os.environ.get("EXTENDVCC_STATE_DIR") or (Path.home() / ".config" / "extendvcc"))


def ledger_path() -> Path:
    if _ledger_path_override:
        return _ledger_path_override
    env = os.environ.get("EXTENDVCC_LEDGER_PATH")
    if env:
        return Path(env)
    return Path.home() / ".local" / "share" / "extendvcc" / "cards.jsonl"
