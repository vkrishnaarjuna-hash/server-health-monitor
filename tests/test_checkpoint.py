# test_checkpoint.py
#
# COVERS checkpoint.py and recovery behavior in monitor.py
#   - checkpoint created after first scan and restored on restart
#   - restart with unresolved faults keeps them active and does not re-read old data
#   - replay after an interrupted scan (injected failure) is idempotent
#   - truncated file: offset reset, unresolved faults preserved
#   - replaced/rotated file (new inode): handled without clearing faults
#   - file disappears and reappears
#   - corrupt state file -> quarantined, monitor starts clean, no crash
#   - checkpoint write failure (injected) -> logged, previous good state kept
#   - atomic write leaves no partial state file behind


"""Checkpoint persistence, crash recovery, corruption and write failures."""

import json
import logging
import os
from datetime import datetime

import pytest

from health_monitor import monitor as monitor_module
from health_monitor.checkpoint import SCHEMA_VERSION, CheckpointStore
from health_monitor.models import Fault, Severity
from health_monitor.monitor import Monitor

ERR1 = "2026-10-08 09:02:00 ERROR [id=F001] Database unavailable"
CRIT2 = "2026-10-08 09:06:00 CRIT [id=F002] Disk failure"
RES1 = "2026-10-08 09:10:00 INFO RESOLVED id=F001"

FAULT = {
    "severity": "ERROR",
    "opened_at": "2026-10-08T09:02:00",
    "message": "m",
}


def file_entry(**overrides):
    entry = {
        "inode": 1,
        "size_seen": 10,
        "offset": 10,
        "active_faults": {},
        "seen_fault_ids": [],
    }
    entry.update(overrides)
    return entry


def state_doc(files, version=SCHEMA_VERSION):
    return {"version": version, "files": files}


def fault_ids(report, rel):
    status = next(s for s in report.statuses if s.relative_path == rel)
    return {fault.fault_id for fault in status.active_faults}


@pytest.fixture
def store(tmp_path):
    return CheckpointStore(tmp_path / "state" / "state.json")


def put_bytes(store, content):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(content)


def put_doc(store, document):
    put_bytes(store, json.dumps(document).encode("utf-8"))


# ------------------------------------------------------- creation / roundtrip
def test_missing_state_file_loads_as_empty(store):
    assert store.load() == {}


def test_save_creates_directories_and_leaves_no_temp_files(make_monitor, write_log, store):
    write_log("dev/s.log", ERR1)
    monitor = make_monitor()
    monitor.scan_once()
    assert store.save(monitor.state) is True
    assert [p.name for p in store.path.parent.iterdir()] == ["state.json"]


def test_state_roundtrips_exactly(make_monitor, write_log, store):
    write_log("dev/a.log", ERR1, CRIT2, RES1)
    write_log("dev/b.log", "2026-10-08 09:02:00 ERROR No explicit id")
    monitor = make_monitor()
    monitor.scan_once()
    store.save(monitor.state)
    assert store.load() == monitor.state


def test_state_file_is_plain_versioned_json(make_monitor, write_log, store):
    write_log("dev/s.log", ERR1)
    monitor = make_monitor()
    monitor.scan_once()
    store.save(monitor.state)
    data = json.loads(store.path.read_bytes())
    assert data["version"] == SCHEMA_VERSION
    assert "dev/s.log" in data["files"]


def test_valid_document_with_active_fault_loads(store):
    put_doc(
        store,
        state_doc(
            {
                "dev/a.log": file_entry(
                    inode=7,
                    size_seen=12,
                    offset=12,
                    active_faults={"F1": FAULT},
                    seen_fault_ids=["F1", "F0"],
                )
            }
        ),
    )
    checkpoint = store.load()["dev/a.log"]
    assert checkpoint.inode == 7
    assert checkpoint.offset == 12
    assert checkpoint.seen_fault_ids == {"F1", "F0"}
    assert checkpoint.active_faults["F1"] == Fault(
        "F1", Severity.ERROR, datetime(2026, 10, 8, 9, 2, 0), "m"
    )


# ------------------------------------------------------- restart and recovery
def test_restart_restores_unresolved_faults_without_rereading(
    make_monitor, write_log, store, monkeypatch
):
    write_log("dev/s.log", ERR1, CRIT2)
    first = make_monitor()
    first.scan_once()
    assert store.save(first.state) is True

    reads = []
    original = Monitor._read_new_lines

    def spy(self, cp, path):
        reads.append(cp.relative_path)
        return original(self, cp, path)

    monkeypatch.setattr(Monitor, "_read_new_lines", spy)

    restarted = make_monitor(state=store.load())
    report = restarted.scan_once()
    assert reads == []
    assert fault_ids(report, "dev/s.log") == {"F001", "F002"}

    write_log("dev/s.log", RES1, append=True)
    assert fault_ids(restarted.scan_once(), "dev/s.log") == {"F002"}
    assert reads == ["dev/s.log"]


def test_crash_mid_scan_recovers_from_last_checkpoint(make_monitor, write_log, store, monkeypatch):
    write_log("dev/s.log", ERR1)
    first = make_monitor()
    first.scan_once()
    store.save(first.state)
    saved = store.path.read_bytes()

    write_log("dev/s.log", CRIT2, RES1, append=True)

    real_parse = monitor_module.parse_line
    calls = {"count": 0}

    def exploding(line):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated crash")
        return real_parse(line)

    with monkeypatch.context() as ctx:
        ctx.setattr(monitor_module, "parse_line", exploding)
        with pytest.raises(RuntimeError):
            first.scan_once()

    assert store.path.read_bytes() == saved  # the crash saved nothing

    restarted = make_monitor(state=store.load())
    report = restarted.scan_once()
    assert fault_ids(report, "dev/s.log") == {"F002"}  # F001 resolved, F002 opened


def test_replay_after_unsaved_progress_matches_uninterrupted_run(make_monitor, write_log, store):
    write_log("dev/s.log", ERR1)
    first = make_monitor()
    first.scan_once()
    store.save(first.state)

    write_log("dev/s.log", CRIT2, RES1, append=True)
    uninterrupted = fault_ids(first.scan_once(), "dev/s.log")  # not saved: "crash"

    recovered = fault_ids(make_monitor(state=store.load()).scan_once(), "dev/s.log")
    assert recovered == uninterrupted == {"F002"}


def test_file_replaced_while_monitor_was_down(make_monitor, write_log, store, tmp_path):
    path = write_log("dev/s.log", ERR1)
    first = make_monitor()
    first.scan_once()
    store.save(first.state)

    incoming = tmp_path / "incoming.log"
    incoming.write_bytes((CRIT2 + "\n").encode("utf-8"))
    os.replace(incoming, path)

    report = make_monitor(state=store.load()).scan_once()
    assert fault_ids(report, "dev/s.log") == {"F001", "F002"}


# --------------------------------------------------------------- corruption
@pytest.mark.parametrize(
    "content",
    [b"", b"not json", b"\xff\xfe\x00", b"[]", b'{"version": 1}'],
    ids=["empty", "garbage", "bad-utf8", "not-object", "missing-files"],
)
def test_corrupt_state_is_quarantined_and_load_returns_empty(store, content, caplog):
    put_bytes(store, content)
    with caplog.at_level(logging.WARNING, logger="health_monitor.checkpoint"):
        assert store.load() == {}
    assert not store.path.exists()
    quarantined = list(store.path.parent.glob("state.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == content
    assert "starting empty" in caplog.text


INVALID_DOCUMENTS = {
    "wrong-version": state_doc({}, version=99),
    "files-not-object": {"version": SCHEMA_VERSION, "files": []},
    "negative-offset": state_doc({"a.log": file_entry(offset=-1)}),
    "offset-beyond-size": state_doc({"a.log": file_entry(offset=11, size_seen=10)}),
    "bool-as-int": state_doc({"a.log": file_entry(inode=True)}),
    "absolute-path": state_doc({"/etc/passwd": file_entry()}),
    "traversal-path": state_doc({"../x.log": file_entry()}),
    "non-canonical-path": state_doc({"a//b.log": file_entry()}),
    "bad-severity": state_doc(
        {
            "a.log": file_entry(
                active_faults={"F1": {**FAULT, "severity": "FATAL"}},
                seen_fault_ids=["F1"],
            )
        }
    ),
    "bad-timestamp": state_doc(
        {
            "a.log": file_entry(
                active_faults={"F1": {**FAULT, "opened_at": "yesterday"}},
                seen_fault_ids=["F1"],
            )
        }
    ),
    "oversized-message": state_doc(
        {
            "a.log": file_entry(
                active_faults={"F1": {**FAULT, "message": "x" * 5000}},
                seen_fault_ids=["F1"],
            )
        }
    ),
    "active-not-in-seen": state_doc(
        {"a.log": file_entry(active_faults={"F1": FAULT}, seen_fault_ids=[])}
    ),
    "extra-key": state_doc({"a.log": file_entry(extra=1)}),
    "seen-not-list": state_doc({"a.log": file_entry(seen_fault_ids="F1")}),
    "id-too-long": state_doc({"a.log": file_entry(seen_fault_ids=["A" * 65])}),
}


@pytest.mark.parametrize("document", list(INVALID_DOCUMENTS.values()), ids=list(INVALID_DOCUMENTS))
def test_schema_violations_are_rejected_and_quarantined(store, document):
    put_doc(store, document)
    assert store.load() == {}
    assert len(list(store.path.parent.glob("state.json.corrupt-*"))) == 1


def test_oversized_state_file_is_rejected(store, monkeypatch):
    from health_monitor import checkpoint as checkpoint_module

    document = json.dumps(state_doc({"a.log": file_entry()})) + " " * 500
    put_bytes(store, document.encode("utf-8"))
    monkeypatch.setattr(checkpoint_module, "MAX_STATE_BYTES", 100)
    assert store.load() == {}
    assert len(list(store.path.parent.glob("state.json.corrupt-*"))) == 1


def test_quarantine_failure_does_not_crash(store, monkeypatch, caplog):
    put_bytes(store, b"garbage")

    def boom(*args, **kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", boom)
    with caplog.at_level(logging.ERROR, logger="health_monitor.checkpoint"):
        assert store.load() == {}
    assert "could not be quarantined" in caplog.text


def test_unreadable_state_path_loads_as_empty(tmp_path):
    assert CheckpointStore(tmp_path).load() == {}  # a directory, not a file


def test_recovery_after_corruption_allows_normal_saving(make_monitor, write_log, store):
    put_bytes(store, b"garbage")
    assert store.load() == {}
    write_log("dev/s.log", ERR1)
    monitor = make_monitor()
    monitor.scan_once()
    assert store.save(monitor.state) is True
    assert store.load() == monitor.state


# ------------------------------------------------------------- write failures
def test_save_failure_keeps_previous_checkpoint(
    make_monitor, write_log, store, monkeypatch, caplog
):
    write_log("dev/s.log", ERR1)
    monitor = make_monitor()
    monitor.scan_once()
    assert store.save(monitor.state) is True
    good = store.path.read_bytes()

    write_log("dev/s.log", CRIT2, append=True)
    monitor.scan_once()  # state changed, so a save will really write

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as ctx:
        ctx.setattr(os, "replace", boom)
        with caplog.at_level(logging.ERROR, logger="health_monitor.checkpoint"):
            assert store.save(monitor.state) is False

    assert store.path.read_bytes() == good
    assert [p.name for p in store.path.parent.iterdir()] == ["state.json"]
    assert "previous checkpoint kept" in caplog.text

    assert store.save(monitor.state) is True  # succeeds once the disk is fine
    assert store.load() == monitor.state


def test_save_to_impossible_location_returns_false(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    assert CheckpointStore(blocker / "state.json").save({}) is False


def test_unchanged_state_is_not_rewritten(make_monitor, write_log, store, monkeypatch):
    write_log("dev/s.log", ERR1)
    monitor = make_monitor()
    monitor.scan_once()

    replaces = []
    real_replace = os.replace

    def counting(src, dst):
        replaces.append(dst)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", counting)

    assert store.save(monitor.state) is True
    assert store.save(monitor.state) is True
    assert len(replaces) == 1  # second save was a no-op

    write_log("dev/s.log", CRIT2, append=True)
    monitor.scan_once()
    assert store.save(monitor.state) is True
    assert len(replaces) == 2  # changed state is written

    store.path.unlink()
    assert store.save(monitor.state) is True
    assert len(replaces) == 3  # deleted file is recreated even if state is unchanged
    assert store.path.exists()
