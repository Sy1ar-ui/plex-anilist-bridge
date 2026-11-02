#!/usr/bin/env python3
"""Refuse a release tag that does not match the version in the code.

The image workflow publishes ``ghcr.io/sy1ar-ui/plex-anilist-bridge:<version>``
from the git tag. A tag like ``v.0.3.0`` is not a version, so nothing would be
published under 0.3.0 and the build would still go green; a tag like
``v0.3.0`` on the 0.4.0 commit would publish 0.4.0 labelled 0.3.0. This check
runs before the build and fails it in both cases.

    python3 scripts/check_release_tag.py v0.4.0          # check a tag name here
    python3 scripts/check_release_tag.py                 # uses $GITHUB_REF_NAME

No dependencies, so it runs on a bare CI runner.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAG = re.compile(r"v(\d+\.\d+\.\d+)")


def app_version(root: Path = ROOT) -> str:
    text = (root / "app" / "config.py").read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("could not find APP_VERSION in app/config.py")
    return match.group(1)


def check(tag: str, version: str) -> str | None:
    """None if the tag is right for this version, else the reason it is not."""
    match = TAG.fullmatch(tag)
    if not match:
        return (f'tag "{tag}" is not a release tag: it must be "v" followed by the version, '
                f'e.g. "v{version}"')
    if match.group(1) != version:
        return (f'tag "{tag}" is on a commit whose version is {version}; '
                f'tag that commit "v{version}", or tag the {match.group(1)} commit instead')
    return None


def main(argv: list[str]) -> int:
    tag = argv[1] if len(argv) > 1 else os.environ.get("GITHUB_REF_NAME", "")
    if not tag:
        print("usage: check_release_tag.py <tag>", file=sys.stderr)
        return 2
    version = app_version()
    problem = check(tag, version)
    if problem:
        print(f"::error::{problem}" if os.environ.get("GITHUB_ACTIONS") else problem, file=sys.stderr)
        return 1
    print(f'tag "{tag}" matches app version {version}')
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
