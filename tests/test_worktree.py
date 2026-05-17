from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_plugin():
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "_oco_test_pkg", root / "__init__.py", submodule_search_locations=[str(root)]
    )
    pkg = importlib.util.module_from_spec(spec)
    pkg.__package__ = "_oco_test_pkg"
    pkg.__path__ = [str(root)]
    sys.modules.setdefault("_oco_test_pkg", pkg)
    spec.loader.exec_module(pkg)
    return pkg


PLUGIN = _load_plugin()
wt = sys.modules["_oco_test_pkg.worktree"]


class TestDeriveAbbrev:
    def test_two_part_label_uses_initials(self):
        assert wt.derive_abbrev("dodo-payments") == "dp"

    def test_three_part_label_uses_initials(self):
        assert wt.derive_abbrev("dodo-backend-bookings") == "dbb"

    def test_single_word_takes_first_three(self):
        assert wt.derive_abbrev("payments") == "pay"

    def test_short_single_word(self):
        assert wt.derive_abbrev("pay") == "pay"

    def test_abbrev_capped_at_five_chars(self):
        result = wt.derive_abbrev("a-b-c-d-e-f-g-h")
        assert len(result) <= 5

    def test_empty_falls_back(self):
        assert len(wt.derive_abbrev("")) >= 2


class TestSlugify:
    def test_kebab_case_lowercase(self):
        assert wt.slugify("Refund Flow", 20) == "refund-flow"

    def test_strips_special_chars(self):
        assert wt.slugify("foo!@#$bar", 20) == "foo-bar"

    def test_word_boundary_truncation(self):
        result = wt.slugify("implement-refund-webhook-handler", 20)
        assert len(result) <= 20
        assert not result.endswith("-")

    def test_returns_task_fallback_on_empty(self):
        assert wt.slugify("", 10) == "task"


class TestComposeAgentId:
    def test_basic_compose_under_twenty_chars(self):
        result = wt.compose_agent_id("dp", "refund-flow", set())
        assert result == "dp/refund-flow"
        assert len(result) <= 20

    def test_collision_appends_numeric_suffix(self):
        existing = {"dp/refunds"}
        result = wt.compose_agent_id("dp", "refunds", existing)
        assert result == "dp/refunds-2"

    def test_multiple_collisions_increment(self):
        existing = {"dp/refunds", "dp/refunds-2", "dp/refunds-3"}
        result = wt.compose_agent_id("dp", "refunds", existing)
        assert result == "dp/refunds-4"

    def test_total_length_capped_at_twenty(self):
        result = wt.compose_agent_id("dbb", "implement-very-long-task-name", set())
        assert len(result) <= 20
        assert result.startswith("dbb/")

    def test_long_abbrev_with_collision_raises(self):
        with pytest.raises(ValueError):
            wt.compose_agent_id("abcdefghijklmnopqr", "x", {"abcdefghijklmnopqr/x"})


class TestAgentIdFs:
    def test_slash_encoded_as_double_underscore(self):
        assert wt.agent_id_to_fs("dp/refunds") == "dp__refunds"
        assert wt.agent_id_to_fs("dbb/2fa-fix") == "dbb__2fa-fix"


class TestProjectKey:
    def test_local_fallback_when_no_remote(self, tmp_path: Path):
        import subprocess

        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
        (tmp_path / "README.md").write_text("x")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@l", "-c", "user.name=t",
             "commit", "-q", "-m", "init"],
            cwd=tmp_path, check=True,
        )

        key = wt.project_key_for(tmp_path)
        assert key.startswith("proj_local_")
        assert len(key) > len("proj_local_")
