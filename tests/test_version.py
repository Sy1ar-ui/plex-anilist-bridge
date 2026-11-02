"""The version is written in four places; a release must change all of them.

The image is published as ``ghcr.io/sy1ar-ui/plex-anilist-bridge:<version>``
when a ``v<version>`` tag is pushed, and the compose file and Unraid template
pin that exact tag (nothing here uses a floating ``:latest``). If one of them
is missed, installs pull an old image or a tag that does not exist.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from app import config

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "ghcr.io/sy1ar-ui/plex-anilist-bridge"


def _pinned_image_tag(path: Path) -> str:
    match = re.search(re.escape(IMAGE) + r":([^\s<\"']+)", path.read_text(encoding="utf-8"))
    assert match, f"{path.name} does not reference {IMAGE}"
    return match.group(1)


def test_the_app_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", config.APP_VERSION)


def test_pyproject_matches_the_app_version():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == config.APP_VERSION


def test_docker_compose_pins_the_app_version():
    assert _pinned_image_tag(ROOT / "docker-compose.yml") == config.APP_VERSION


def test_the_unraid_template_pins_the_app_version():
    assert _pinned_image_tag(ROOT / "unraid-template.xml") == config.APP_VERSION
