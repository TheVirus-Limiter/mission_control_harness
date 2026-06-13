"""Test fixtures. Puts the project root on sys.path so tests import the
top-level modules (harness, materials, ...) exactly as harness.py does."""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# SAFETY: the dashboard server now loads .env on import (which carries DRY_RUN=0 +
# real X creds). Force the test process to dry-run BEFORE anything imports the
# server, so the suite can never make a real post. load_dotenv() never overrides a
# var already set, so this wins over .env.
os.environ["DRY_RUN"] = "1"


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def launch_mission():
    return os.path.join(ROOT, "missions", "launch.yaml")
