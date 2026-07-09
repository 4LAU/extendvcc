#!/usr/bin/env python3
"""Cut a release in one atomic step.

    uv run python scripts/release.py X.Y.Z [--yes] [--allow-empty]

What it does:
  1. Move the CHANGELOG "[Unreleased]" section into a dated "[X.Y.Z]" section
     and refresh the compare links at the bottom.
  2. Commit that change as `chore(release): X.Y.Z`.
  3. Tag `vX.Y.Z` and push the branch and the tag together with
     `git push --atomic` — so the tag can never be pushed without the commit,
     or forgotten after it.

The package version is NOT stored anywhere in the tree: hatch-vcs derives it from
the `vX.Y.Z` git tag at build time (see pyproject.toml `[tool.hatch.version]`).
That is why this script never edits pyproject.toml — the tag is the single source
of truth, and the wheel PyPI publishes always matches it.
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path

REPO_URL = "https://github.com/4LAU/extendvcc"
CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SECTION_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\]", re.MULTILINE)


def run(*args: str, capture: bool = False) -> str:
    """Run a git command, aborting the release on any non-zero exit."""
    result = subprocess.run(args, text=True, capture_output=capture)
    if result.returncode != 0:
        if capture and result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        sys.exit(f"error: command failed: {' '.join(args)}")
    return (result.stdout or "").strip()


def preflight() -> None:
    """Refuse to release from a dirty tree, the wrong branch, or a stale main."""
    branch = run("git", "branch", "--show-current", capture=True)
    if branch != "main":
        sys.exit(f"error: releases must be cut from main, not '{branch}'")
    if run("git", "status", "--porcelain", capture=True):
        sys.exit("error: working tree is dirty; commit or stash first")
    run("git", "fetch", "origin", "main", capture=True)
    if run("git", "rev-list", "HEAD..origin/main", capture=True):
        sys.exit("error: local main is behind origin/main; pull first")


def split_unreleased(text: str) -> tuple[str, str, str]:
    """Return (head, unreleased_body, tail) around the [Unreleased] section.

    `head` ends just after the "## [Unreleased]" line, `tail` starts at the first
    dated section, and `unreleased_body` is the content between them with the
    `---` separator and surrounding blank lines stripped.
    """
    marker = "## [Unreleased]"
    if marker not in text:
        sys.exit("error: no '## [Unreleased]' section in CHANGELOG.md")
    after = text.index(marker) + len(marker)
    match = SECTION_RE.search(text, after)
    if not match:
        sys.exit("error: no prior dated section found in CHANGELOG.md")
    head, tail = text[:after], text[match.start() :]
    # The "---" section separator sits at the end of the Unreleased span (an empty
    # section is just the separator); strip it from either side to get the body.
    body = text[after : match.start()].strip().removeprefix("---").removesuffix("---").strip()
    return head, body, tail


def rewrite_changelog(text: str, version: str, date: str, allow_empty: bool) -> str:
    """Move the Unreleased body into a dated section and refresh compare links."""
    head, body, tail = split_unreleased(text)
    if not body and not allow_empty:
        sys.exit("error: [Unreleased] section is empty; add notes or pass --allow-empty")
    prev = SECTION_RE.search(tail).group(1)
    section = f"## [{version}] - {date}\n\n"
    if body:
        section += f"{body}\n\n"
    section += "---\n\n"
    text = f"{head}\n\n---\n\n{section}{tail}"

    unreleased_link = f"[Unreleased]: {REPO_URL}/compare/v{version}...HEAD"
    version_link = f"[{version}]: {REPO_URL}/compare/v{prev}...v{version}"
    text, n = re.subn(r"^\[Unreleased\]:.*$", f"{unreleased_link}\n{version_link}", text, count=1, flags=re.MULTILINE)
    if n != 1:
        sys.exit("error: could not find the [Unreleased] compare link to update")
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description="Cut a release (changelog + commit + tag + atomic push).")
    parser.add_argument("version", help="new version, e.g. 0.3.0 (no leading 'v')")
    parser.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--allow-empty", action="store_true", help="permit an empty [Unreleased] section")
    args = parser.parse_args()

    version = args.version.lstrip("v")
    if not VERSION_RE.match(version):
        sys.exit(f"error: '{args.version}' is not a valid X.Y.Z version")

    preflight()
    existing = {m.group(1) for m in SECTION_RE.finditer(CHANGELOG.read_text(encoding="utf-8"))}
    if version in existing:
        sys.exit(f"error: version {version} already has a CHANGELOG section")

    date = datetime.date.today().isoformat()
    updated = rewrite_changelog(CHANGELOG.read_text(encoding="utf-8"), version, date, args.allow_empty)

    print(f"About to release v{version} ({date}):")
    print("  - rewrite CHANGELOG.md ([Unreleased] -> dated section + compare links)")
    print(f"  - commit 'chore(release): {version}'")
    print(f"  - tag v{version}")
    print("  - git push --atomic origin main v" + version)
    if not args.yes and input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        sys.exit("aborted")

    CHANGELOG.write_text(updated, encoding="utf-8")
    run("git", "add", "CHANGELOG.md")
    run("git", "commit", "-m", f"chore(release): {version}")
    run("git", "tag", f"v{version}")
    run("git", "push", "--atomic", "origin", "main", f"v{version}")
    print(f"Released v{version}. GitHub Actions will build binaries and publish to PyPI.")


if __name__ == "__main__":
    main()
