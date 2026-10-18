# test_monitor.py
#
# COVERS monitor.py (discovery, counting, incremental reads)
#   - empty org -> 0 servers, 0 faulty
#   - multiple environments and nested directories discovered recursively
#   - configurable threshold: ERROR threshold ignores WARN, CRIT threshold ignores ERROR
#   - multiple active faults on one server -> server counted once
#   - newly appended entries picked up on the next scan
#   - unchanged file is not re-read (observable via counters or a spy)
#   - partial trailing line is held back until its newline arrives
#   - missing / unreadable file reported as error, not as healthy
#   - symlinks are skipped

"""Discovery, counting, thresholds and incremental reading."""

import builtins
import logging
import os
import shutil

import pytest

from health_monitor import monitor as monitor_module
from health_monitor.models import Severity
from health_monitor.monitor import MAX_LINE_BYTES, Monitor

INFO_LINE = "2026-10-08 09:00:00 INFO Service started"
ERR1 = "2026-10-08 09:02:00 ERROR [id=F001] Database unavailable"
CRIT2 = "2026-10-08 09:06:00 CRIT [id=F002] Disk failure"
RES1 = "2026-10-08 09:10:00 INFO RESOLVED id=F001"
ERR9 = "2026-10-08 09:20:00 ERROR [id=F9] Later failure"


def fault_ids(report, rel):
    status = next(s for s in report.statuses if s.relative_path == rel)
    return {fault.fault_id for fault in status.active_faults}


def deny_open_for(monkeypatch, name):
    """Make opening any file whose path ends with `name` raise PermissionError."""
    real_open = builtins.open

    def fake(file, *args, **kwargs):
        if str(file).endswith(name):
            raise PermissionError(13, "Permission denied")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(monitor_module, "open", fake, raising=False)


# ---------------------------------------------------------------- discovery
def test_empty_org_has_no_servers(make_monitor):
    report = make_monitor().scan_once()
    assert report.total_servers == 0
    assert report.faulty_servers == 0
    assert report.statuses == ()
    assert report.discovery_errors == ()


def test_discovers_environments_and_nested_directories(make_monitor, write_log):
    write_log("staging/team-a/server-5.log", INFO_LINE)
    write_log("prd/server-3.log", INFO_LINE)
    write_log("dev/server-1.log", INFO_LINE)
    report = make_monitor().scan_once()
    assert report.total_servers == 3
    assert [s.relative_path for s in report.statuses] == [
        "dev/server-1.log",
        "prd/server-3.log",
        "staging/team-a/server-5.log",
    ]


def test_hidden_files_and_directories_are_ignored(make_monitor, write_log):
    write_log("dev/server-1.log", INFO_LINE)
    write_log("dev/.hidden", ERR1)
    write_log(".git/objects.log", ERR1)
    report = make_monitor().scan_once()
    assert report.total_servers == 1
    assert report.faulty_servers == 0


def test_symlinks_are_not_followed(make_monitor, write_log, org, tmp_path):
    write_log("dev/real.log", INFO_LINE)
    target = tmp_path / "outside.log"
    target.write_text(ERR1 + "\n")
    try:
        os.symlink(target, org / "dev" / "link.log")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this system")
    report = make_monitor().scan_once()
    assert report.total_servers == 1
    assert report.faulty_servers == 0


# --------------------------------------------------------------- thresholds
ALL_SEVERITIES = [
    "2026-10-08 09:00:00 INFO [id=I1] info",
    "2026-10-08 09:01:00 WARN [id=W1] warn",
    "2026-10-08 09:02:00 ERROR [id=E1] error",
    "2026-10-08 09:03:00 CRIT [id=C1] crit",
]


@pytest.mark.parametrize(
    ("threshold", "expected"),
    [
        (Severity.INFO, {"I1", "W1", "E1", "C1"}),
        (Severity.WARN, {"W1", "E1", "C1"}),
        (Severity.ERROR, {"E1", "C1"}),
        (Severity.CRIT, {"C1"}),
    ],
)
def test_threshold_selects_which_severities_open_faults(
    make_monitor, write_log, threshold, expected
):
    write_log("dev/s.log", *ALL_SEVERITIES)
    report = make_monitor(min_severity=threshold).scan_once()
    assert fault_ids(report, "dev/s.log") == expected


def test_server_with_many_faults_is_counted_once(make_monitor, write_log):
    write_log("dev/s.log", ERR1, CRIT2)
    report = make_monitor().scan_once()
    assert report.total_servers == 1
    assert report.faulty_servers == 1
    assert fault_ids(report, "dev/s.log") == {"F001", "F002"}


def test_alert_without_id_gets_a_stable_derived_id(make_monitor, write_log):
    write_log("dev/s.log", "2026-10-08 09:02:00 ERROR Database unavailable")
    monitor = make_monitor()
    first = fault_ids(monitor.scan_once(), "dev/s.log")
    second = fault_ids(monitor.scan_once(), "dev/s.log")
    fresh = fault_ids(make_monitor().scan_once(), "dev/s.log")
    assert len(first) == 1
    assert next(iter(first)).startswith("auto-")
    assert first == second == fresh


# -------------------------------------------------------------- incremental
def test_appended_entries_are_picked_up(make_monitor, write_log):
    write_log("dev/s.log", INFO_LINE)
    monitor = make_monitor()
    assert monitor.scan_once().faulty_servers == 0
    write_log("dev/s.log", ERR1, append=True)
    assert monitor.scan_once().faulty_servers == 1


def test_unchanged_file_is_not_reread(make_monitor, write_log, monkeypatch):
    path = write_log("dev/s.log", ERR1)
    monitor = make_monitor()
    monitor.scan_once()

    calls = []
    original = Monitor._read_new_lines

    def spy(self, cp, file_path):
        calls.append(cp.relative_path)
        return original(self, cp, file_path)

    monkeypatch.setattr(Monitor, "_read_new_lines", spy)
    monitor.scan_once()
    monitor.scan_once()
    assert calls == []

    write_log("dev/s.log", INFO_LINE, append=True)
    monitor.scan_once()
    assert calls == ["dev/s.log"]
    assert monitor.state["dev/s.log"].offset == path.stat().st_size


def test_partial_trailing_line_waits_for_its_newline(make_monitor, write_raw):
    write_raw("dev/s.log", "2026-10-08 09:02:00 ERROR [id=F1] Dis")
    monitor = make_monitor()
    report = monitor.scan_once()
    assert report.faulty_servers == 0
    assert monitor.state["dev/s.log"].offset == 0

    write_raw("dev/s.log", "k full\n", append=True)
    report = monitor.scan_once()
    status = report.statuses[0]
    assert [f.fault_id for f in status.active_faults] == ["F1"]
    assert status.active_faults[0].message == "Disk full"


def test_malformed_lines_do_not_stop_valid_ones(make_monitor, write_log, caplog):
    write_log(
        "dev/s.log",
        "garbage line",
        "2026-13-99 25:00:00 ERROR [id=X] bad timestamp",
        ERR1,
    )
    with caplog.at_level(logging.WARNING, logger="health_monitor.monitor"):
        report = make_monitor().scan_once()
    assert fault_ids(report, "dev/s.log") == {"F001"}
    assert report.discovery_errors == ()
    assert "malformed line" in caplog.text


def test_oversized_line_is_skipped_and_later_lines_still_apply(make_monitor, write_raw, write_log):
    huge = "2026-10-08 09:00:00 INFO " + "x" * (MAX_LINE_BYTES * 2)
    path = write_raw("dev/s.log", huge + "\n")
    write_log("dev/s.log", ERR9, append=True)
    monitor = make_monitor()
    report = monitor.scan_once()
    assert fault_ids(report, "dev/s.log") == {"F9"}
    assert monitor.state["dev/s.log"].offset == path.stat().st_size


def test_incomplete_oversized_line_does_not_advance_offset(make_monitor, write_raw, write_log):
    huge = "2026-10-08 09:00:00 INFO " + "x" * (MAX_LINE_BYTES * 2)
    write_raw("dev/s.log", huge)  # no newline yet
    monitor = make_monitor()
    assert monitor.scan_once().faulty_servers == 0
    assert monitor.state["dev/s.log"].offset == 0

    write_raw("dev/s.log", "\n", append=True)
    write_log("dev/s.log", ERR9, append=True)
    assert fault_ids(monitor.scan_once(), "dev/s.log") == {"F9"}


# --------------------------------------------------- unreadable and missing
def test_unreadable_file_is_counted_but_not_reported_healthy(make_monitor, write_log, monkeypatch):
    write_log("dev/bad.log", INFO_LINE)
    deny_open_for(monkeypatch, "bad.log")
    report = make_monitor().scan_once()
    status = report.statuses[0]
    assert report.total_servers == 1
    assert status.readable is False
    assert "PermissionError" in report.discovery_errors[0]
    assert "dev/bad.log" in report.discovery_errors[0]


def test_unreadable_file_keeps_previously_known_faults(make_monitor, write_log, monkeypatch):
    write_log("dev/s.log", ERR1)
    monitor = make_monitor()
    monitor.scan_once()

    write_log("dev/s.log", INFO_LINE, append=True)  # size changes -> read attempted
    deny_open_for(monkeypatch, "s.log")
    report = monitor.scan_once()
    assert report.faulty_servers == 1
    assert report.statuses[0].readable is False
    assert fault_ids(report, "dev/s.log") == {"F001"}


def test_missing_file_with_active_faults_stays_counted(make_monitor, write_log):
    path = write_log("dev/s.log", ERR1)
    monitor = make_monitor()
    monitor.scan_once()
    path.unlink()

    report = monitor.scan_once()
    assert report.total_servers == 1
    assert report.faulty_servers == 1
    assert report.statuses[0].readable is False
    assert "missing" in report.statuses[0].error
    assert any("missing" in e for e in report.discovery_errors)


def test_missing_file_without_faults_is_dropped(make_monitor, write_log):
    path = write_log("dev/s.log", INFO_LINE)
    monitor = make_monitor()
    monitor.scan_once()
    path.unlink()

    report = monitor.scan_once()
    assert report.total_servers == 0
    assert report.discovery_errors == ()
    assert "dev/s.log" not in monitor.state


def test_reappearing_file_replays_idempotently(make_monitor, write_log):
    path = write_log("dev/s.log", ERR1)
    monitor = make_monitor()
    monitor.scan_once()
    path.unlink()
    monitor.scan_once()

    write_log("dev/s.log", ERR1, RES1)  # file comes back, now with a resolution
    report = monitor.scan_once()
    assert report.faulty_servers == 0
    assert report.total_servers == 1


def test_vanished_root_keeps_known_faults_and_does_not_crash(make_monitor, write_log, org):
    write_log("dev/s.log", ERR1)
    monitor = make_monitor()
    monitor.scan_once()
    shutil.rmtree(org)

    report = monitor.scan_once()
    assert report.faulty_servers == 1
    assert any("<root>" in e for e in report.discovery_errors)


# ----------------------------------------------- truncation and replacement
def test_truncated_file_is_reread_and_keeps_unresolved_faults(make_monitor, write_log):
    write_log("dev/s.log", ERR1)
    monitor = make_monitor()
    monitor.scan_once()

    write_log("dev/s.log", "2026-10-08 10:00:00 INFO ok")  # shorter: truncates
    assert fault_ids(monitor.scan_once(), "dev/s.log") == {"F001"}

    write_log("dev/s.log", RES1, append=True)
    assert monitor.scan_once().faulty_servers == 0


def test_replaced_file_is_reread_without_clearing_faults(make_monitor, write_log, tmp_path):
    path = write_log("dev/s.log", ERR1)
    monitor = make_monitor()
    monitor.scan_once()

    incoming = tmp_path / "incoming.log"
    incoming.write_bytes((CRIT2 + "\n").encode("utf-8"))
    os.replace(incoming, path)  # rotation: new file at the same path

    report = monitor.scan_once()
    assert fault_ids(report, "dev/s.log") == {"F001", "F002"}
