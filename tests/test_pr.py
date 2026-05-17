from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

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


_load_plugin()
pr_mod = sys.modules["_oco_test_pkg.pr"]


def _completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


class TestExistingPrFromOutput:
    def test_matches_gh_already_exists_stderr(self):
        stderr = (
            'a pull request for branch "feat/x" into branch "main" already exists:\n'
            "https://github.com/acme/repo/pull/42\n"
        )
        out = pr_mod._existing_pr_from_output("", stderr)
        assert out == ("https://github.com/acme/repo/pull/42", 42)

    def test_matches_when_message_lands_in_stdout(self):
        stdout = (
            'a pull request for branch "x" into branch "main" already exists:\n'
            "https://github.com/acme/repo/pull/7"
        )
        out = pr_mod._existing_pr_from_output(stdout, "")
        assert out == ("https://github.com/acme/repo/pull/7", 7)

    def test_returns_none_for_unrelated_error(self):
        assert pr_mod._existing_pr_from_output("", "fatal: not a git repo") is None

    def test_returns_none_when_phrase_present_but_no_url(self):
        assert pr_mod._existing_pr_from_output("", "already exists: somewhere") is None

    def test_match_is_case_insensitive(self):
        stderr = (
            'A Pull Request For Branch "x" Into Branch "main" ALREADY EXISTS:\n'
            "https://github.com/acme/repo/pull/11\n"
        )
        out = pr_mod._existing_pr_from_output("", stderr)
        assert out == ("https://github.com/acme/repo/pull/11", 11)


class TestOpenPrAlreadyExists:
    def test_treats_already_exists_as_success(self, tmp_path: Path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        url = "https://github.com/acme/repo/pull/99"
        create_stderr = (
            'a pull request for branch "feat/x" into branch "main" already exists:\n'
            f"{url}\n"
        )
        view_stdout = (
            '{"number": 99, "url": "https://github.com/acme/repo/pull/99", '
            '"state": "OPEN", "mergedAt": null}'
        )

        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            if "create" in args:
                return _completed(args, returncode=1, stdout="", stderr=create_stderr)
            if "view" in args:
                return _completed(args, returncode=0, stdout=view_stdout, stderr="")
            raise AssertionError(f"unexpected subprocess call: {args}")

        with patch.object(pr_mod, "_gh", return_value="/usr/bin/gh"):
            with patch.object(pr_mod.subprocess, "run", side_effect=fake_run):
                info = pr_mod.open_pr(worktree, base_branch="main", title="t", body="b")

        assert info.number == 99
        assert info.url == url
        assert info.state == "OPEN"
        assert info.merged_at is None
        assert any("create" in c for c in calls)
        assert any("view" in c for c in calls)

    def test_unrelated_failure_still_raises(self, tmp_path: Path):
        worktree = tmp_path / "wt"
        worktree.mkdir()

        def fake_run(args, **kwargs):
            return _completed(args, returncode=1, stdout="", stderr="fatal: not a git repo")

        with patch.object(pr_mod, "_gh", return_value="/usr/bin/gh"):
            with patch.object(pr_mod.subprocess, "run", side_effect=fake_run):
                with pytest.raises(pr_mod.PrError, match="gh pr create failed"):
                    pr_mod.open_pr(worktree, base_branch="main")

    def test_falls_back_when_pr_state_flakes(self, tmp_path: Path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        url = "https://github.com/acme/repo/pull/17"
        create_stderr = (
            'a pull request for branch "x" into branch "main" already exists:\n'
            f"{url}\n"
        )

        def fake_run(args, **kwargs):
            if "create" in args:
                return _completed(args, returncode=1, stdout="", stderr=create_stderr)
            if "view" in args:
                return _completed(args, returncode=1, stdout="", stderr="rate limited")
            raise AssertionError(f"unexpected subprocess call: {args}")

        with patch.object(pr_mod, "_gh", return_value="/usr/bin/gh"):
            with patch.object(pr_mod.subprocess, "run", side_effect=fake_run):
                info = pr_mod.open_pr(worktree, base_branch="main")

        assert info.number == 17
        assert info.url == url
        assert info.state == "OPEN"
        assert info.merged_at is None
