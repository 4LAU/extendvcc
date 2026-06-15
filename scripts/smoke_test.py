"""Live smoke-test harness for extendvcc.

Drives the full card lifecycle against the REAL Extend API and cleans up after
itself. Run manually before a release. Never collected by pytest (it lives under
scripts/, not tests/) and never run in CI.

Usage:
    uv run python scripts/smoke_test.py [--yes] [--parent CARD_ID] [--bulk K] [--json]
"""

from __future__ import annotations

SMOKE_CARD_BALANCE_CENTS = 11001  # $110.01 — distinctive, easy to spot if cleanup fails
SMOKE_CARD_NAME_PREFIX = "extendvcc-smoke"
