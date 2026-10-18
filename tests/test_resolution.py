# test_resolution.py
#
# COVERS the fault lifecycle rules
#   - RESOLVED id=F001 clears only F001, F002 stays active
#   - INFO / WARN lines never clear an active fault
#   - resolving an unknown id changes nothing
#   - duplicate fault ids do not create duplicate faults
#   - a resolved id is not silently re-opened (id reuse policy)
#   - resolution that appears in a later scan than the fault still clears it


"""Fault lifecycle rules: open, resolve, duplicates, replay, restart."""

from health_monitor.models import Severity

ERR1 = "2026-10-08 09:02:00 ERROR [id=F001] Database unavailable"
CRIT2 = "2026-10-08 09:06:00 CRIT [id=F002] Disk failure"
RES1 = "2026-10-08 09:10:00 INFO RESOLVED id=F001"
RES2 = "2026-10-08 09:11:00 INFO RESOLVED id=F002"


def fault_ids(report, rel):
    status = next(s for s in report.statuses if s.relative_path == rel)
    return {fault.fault_id for fault in status.active_faults}


def test_resolution_clears_only_the_referenced_fault(make_monitor, write_log):
    write_log("dev/s.log", ERR1, CRIT2, RES1)
    report = make_monitor().scan_once()
    assert fault_ids(report, "dev/s.log") == {"F002"}
    assert report.faulty_servers == 1


def test_server_is_healthy_once_every_fault_is_resolved(make_monitor, write_log):
    write_log("dev/s.log", ERR1, CRIT2, RES1, RES2)
    report = make_monitor().scan_once()
    assert report.total_servers == 1
    assert report.faulty_servers == 0


def test_info_and_warn_lines_never_clear_faults(make_monitor, write_log):
    write_log(
        "dev/s.log",
        ERR1,
        "2026-10-08 09:03:00 INFO Service recovered",
        "2026-10-08 09:04:00 WARN All good now",
        "2026-10-08 09:05:00 INFO [id=F001] recovered",
    )
    report = make_monitor().scan_once()
    assert fault_ids(report, "dev/s.log") == {"F001"}


def test_resolution_of_unknown_id_changes_nothing(make_monitor, write_log):
    write_log("dev/s.log", ERR1, "2026-10-08 09:10:00 INFO RESOLVED id=NOPE")
    report = make_monitor().scan_once()
    assert fault_ids(report, "dev/s.log") == {"F001"}


def test_duplicate_fault_ids_do_not_create_duplicate_faults(make_monitor, write_log):
    write_log(
        "dev/s.log",
        ERR1,
        "2026-10-08 09:03:00 ERROR [id=F001] Same id, different text",
    )
    report = make_monitor().scan_once()
    faults = report.statuses[0].active_faults
    assert len(faults) == 1
    assert faults[0].message == "Database unavailable"


def test_resolved_id_is_not_silently_reopened(make_monitor, write_log):
    write_log("dev/s.log", ERR1, RES1, ERR1)
    assert make_monitor().scan_once().faulty_servers == 0


def test_resolution_appended_later_clears_the_fault(make_monitor, write_log):
    write_log("dev/s.log", ERR1)
    monitor = make_monitor()
    assert monitor.scan_once().faulty_servers == 1
    write_log("dev/s.log", RES1, append=True)
    assert monitor.scan_once().faulty_servers == 0


def test_resolution_applies_regardless_of_threshold(make_monitor, write_log):
    write_log("dev/s.log", CRIT2, RES2)
    report = make_monitor(min_severity=Severity.CRIT).scan_once()
    assert report.faulty_servers == 0


def test_same_fault_id_on_different_servers_is_independent(make_monitor, write_log):
    write_log("dev/a.log", ERR1, RES1)
    write_log("dev/b.log", ERR1)
    report = make_monitor().scan_once()
    assert fault_ids(report, "dev/a.log") == set()
    assert fault_ids(report, "dev/b.log") == {"F001"}
    assert report.faulty_servers == 1


def test_active_faults_are_ordered_by_time(make_monitor, write_log):
    write_log("dev/s.log", CRIT2, ERR1)  # file order differs from time order
    faults = make_monitor().scan_once().statuses[0].active_faults
    assert [f.fault_id for f in faults] == ["F001", "F002"]


def test_restart_with_saved_state_keeps_faults_and_continues(make_monitor, write_log):
    write_log("dev/s.log", ERR1, CRIT2)
    first = make_monitor()
    first.scan_once()

    restarted = make_monitor(state=first.state)  # state restored, no re-read needed
    assert restarted.scan_once().faulty_servers == 1
    path = write_log("dev/s.log", RES1, append=True)
    report = restarted.scan_once()
    assert fault_ids(report, "dev/s.log") == {"F002"}
    assert restarted.state["dev/s.log"].offset == path.stat().st_size


def test_replay_from_start_is_idempotent(make_monitor, write_log):
    write_log("dev/s.log", ERR1, CRIT2, RES1)
    monitor = make_monitor()
    first = monitor.scan_once()

    checkpoint = monitor.state["dev/s.log"]
    checkpoint.offset = 0  # simulate re-processing the same bytes
    checkpoint.size_seen = 0
    second = monitor.scan_once()

    assert fault_ids(first, "dev/s.log") == {"F002"}
    assert fault_ids(second, "dev/s.log") == {"F002"}
    assert len(second.statuses[0].active_faults) == 1
