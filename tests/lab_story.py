"""Explicit make-lab harness; not collected by the ordinary regression suite."""

from tempfile import TemporaryDirectory

import pytest

from lab.server import serve
from lab.session import LabSession
from lab.trace import TracedDatabase
from ledgerops.cli import _revision

pytestmark = pytest.mark.integration


def test_local_learning_lab(db, golden):
    revision, dirty = _revision()
    with TemporaryDirectory(prefix="ledgerops-lab-") as artifacts:
        serve(LabSession(TracedDatabase(db), golden, artifacts, revision, dirty))
