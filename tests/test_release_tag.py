"""The release-tag check that guards the image workflow."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app import config

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_release_tag.py"
spec = importlib.util.spec_from_file_location("check_release_tag", SCRIPT)
check_release_tag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_release_tag)


def test_it_reads_the_same_version_as_the_app():
    assert check_release_tag.app_version() == config.APP_VERSION


def test_the_right_tag_passes():
    assert check_release_tag.check("v0.4.0", "0.4.0") is None


@pytest.mark.parametrize("tag", ["v.0.3.0", "0.3.0", "v0.3", "v0.3.0-rc1", "release-0.3.0", "V0.3.0"])
def test_a_malformed_tag_is_refused(tag):
    assert "not a release tag" in check_release_tag.check(tag, "0.3.0")


def test_a_tag_on_the_wrong_commit_is_refused():
    """What happened to v0.3.0: placed on the 0.4.0 merge commit."""
    reason = check_release_tag.check("v0.3.0", "0.4.0")
    assert "version is 0.4.0" in reason


def test_the_command_line_exit_codes(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert check_release_tag.main(["x", f"v{config.APP_VERSION}"]) == 0
    assert check_release_tag.main(["x", "v.0.3.0"]) == 1
    assert "not a release tag" in capsys.readouterr().err


def test_the_version_can_be_printed_for_the_release_workflow(capsys):
    assert check_release_tag.main(["x", "--print-version"]) == 0
    assert capsys.readouterr().out.strip() == config.APP_VERSION


# --------------------------------------------------------------------------
# the workflows: a release can only be cut from main, never by a hand-pushed tag
# --------------------------------------------------------------------------
WORKFLOWS = SCRIPT.parent.parent / ".github" / "workflows"


def _workflow(name: str) -> dict:
    import yaml

    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    # YAML 1.1 reads the bare key `on` as the boolean True.
    data["on"] = data.pop(True, data.get("on"))
    return data


def test_no_workflow_publishes_on_a_pushed_tag():
    """GitHub runs a tagged commit's own workflow, so a tag trigger can never be trusted."""
    for path in WORKFLOWS.glob("*.yml"):
        triggers = _workflow(path.name)["on"]
        push = triggers.get("push") if isinstance(triggers, dict) else None
        assert not (push or {}).get("tags"), f"{path.name} triggers on tag pushes"


def test_the_release_workflow_runs_only_by_hand_and_only_on_main():
    release = _workflow("release.yml")
    assert set(release["on"]) == {"workflow_dispatch"}
    first_step = release["jobs"]["prepare"]["steps"][0]
    assert first_step["if"] == "github.ref != 'refs/heads/main'"
    assert "exit 1" in first_step["run"]


def test_the_release_tags_only_after_the_image_is_published():
    jobs = _workflow("release.yml")["jobs"]
    assert jobs["image"]["uses"] == "./.github/workflows/docker.yml"
    assert set(jobs["publish"]["needs"]) == {"prepare", "image"}
