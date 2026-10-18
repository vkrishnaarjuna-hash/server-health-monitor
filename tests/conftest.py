# conftest.py
#
# RESPONSIBILITY: shared pytest fixtures. Tests must only use tmp_path, never real logs.
#
# PLANNED FIXTURES:
#   - org_root(tmp_path)      -> empty org directory
#   - write_log(org_root)     -> helper: write_log("dev/server-1.log", lines, append=False)
#   - settings_factory(...)   -> build Settings with test defaults (state file in tmp_path)
#   - monitor_factory(...)    -> Monitor + CheckpointStore wired to tmp paths

"""Shared fixtures. Tests only ever touch pytest's tmp_path, never real logs."""

import pytest

from health_monitor.config import Settings
from health_monitor.models import Severity
from health_monitor.monitor import Monitor


@pytest.fixture
def org(tmp_path):
    root = tmp_path / "org"
    root.mkdir()
    return root


@pytest.fixture
def make_settings(tmp_path, org):
    def _make(min_severity=Severity.ERROR):
        return Settings(
            root=org.resolve(),
            interval=1.0,
            min_severity=min_severity,
            state_file=tmp_path / "state" / "state.json",
        )

    return _make


@pytest.fixture
def make_monitor(make_settings):
    def _make(min_severity=Severity.ERROR, state=None):
        return Monitor(make_settings(min_severity), state=state)

    return _make


@pytest.fixture
def write_raw(org):
    """write_raw("dev/a.log", "text", append=False): exact bytes, no newline added."""

    def _write(rel, text, append=False):
        path = org / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab" if append else "wb") as fh:
            fh.write(text.encode("utf-8"))
        return path

    return _write


@pytest.fixture
def write_log(write_raw):
    """write_log("dev/a.log", line1, line2, append=False): one line each, LF-terminated."""

    def _write(rel, *lines, append=False):
        return write_raw(rel, "".join(f"{line}\n" for line in lines), append=append)

    return _write
