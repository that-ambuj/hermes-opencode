from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("hermes-home")
    monkeypatch.setenv("HERMES_HOME", str(home))
    yield home
