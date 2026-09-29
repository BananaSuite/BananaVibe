"""Shared fixtures: a local Git-backed forge and deterministic clocks."""

import pytest

from bananavibe.state import StateStore
from fakes import LocalForge


@pytest.fixture
def forge(tmp_path):
    return LocalForge(tmp_path)


@pytest.fixture
def store(forge):
    return StateStore(forge, sleep=lambda *_: None)
