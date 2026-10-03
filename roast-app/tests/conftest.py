"""Pytest fixtures.

Defaults to a local SQLite file so ``pytest`` needs no infrastructure; point
DATABASE_URL at PostgreSQL to run the identical suite against it:

    DATABASE_URL=postgresql+psycopg2://roast:roast@localhost:5432/roast pytest
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

# Must be set BEFORE importing the app (engine is created at import time).
# Use a throwaway temp database so the tracked repo-level test.db (an old-schema
# fixture) is never mutated by a test run.
import tempfile

_fd, _path = tempfile.mkstemp(suffix=".db", prefix="roast-tests-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_path}")

from app import models  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    # deterministic start: recreate every table
    models.Base.metadata.drop_all(models.engine)
    models.Base.metadata.create_all(models.engine)
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_database():
    """Each test starts from an empty schema so batches/events/groups created
    (and corrected) by one test can never leak into another."""
    models.Base.metadata.drop_all(models.engine)
    models.Base.metadata.create_all(models.engine)
    yield
