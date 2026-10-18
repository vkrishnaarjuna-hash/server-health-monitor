# test_cli.py
#
# COVERS cli.py
#   - --once prints the three required items and exits 0
#   - report lists affected servers with relative paths and fault ids
#   - invalid configuration -> message on stderr, exit code 2
#   - report goes to stdout, operational logs do not pollute it
#   - loop stops cleanly on interrupt and saves the checkpoint (sleep/loop mocked)
#   - an exception in one cycle does not terminate the loop


"""CLI: report rendering, exit codes, loop control and graceful shutdown."""

import builtins
import dataclasses
import logging
import signal
import threading
from datetime import datetime

import pytest

from health_monitor import cli as cli_module
from health_monitor import monitor as monitor_module
from health_monitor.checkpoint import CheckpointStore
from health_monitor.cli import main, render_report, run
from health_monitor.models import Fault, ScanReport, ServerStatus, Severity
from health_monitor.monitor import Monitor

INFO_LINE = "2026-10-08 09:00:00 INFO Service started"
ERR1 = "2026-10-08 09:02:00 ERROR [id=F001] Database unavailable"
CRIT2 = "2026-10-08 09:06:00 CRIT [id=F002] Disk failure"
RES1 = "2026-10-08 09:10:00 INFO RESOLVED id=F001"

FAULT = Fault("F1", Severity.ERROR, datetime(2026, 10, 8, 9, 2), "Database unavailable")


@pytest.fixture
def run_cli(org, tmp_path):
    state = tmp_path / "state" / "state.json"

    def _run(*extra, env=None):
        argv = ["--root", str(org), "--state-file", str(state), "--once", *extra]
        return main(argv, {} if env is None else env)

    return _run


def stop_after(monkeypatch, cycles, stop, on_wait=None):
    """Replace the inter-cycle wait: stop the loop after `cycles` waits."""
    calls = []

    def fake_wait(event, seconds):
        calls.append(seconds)
        if on_wait is not None:
            on_wait(len(calls))
        if len(calls) >= cycles:
            event.set()
            return True
        return False

    monkeypatch.setattr(cli_module, "_wait", fake_wait)
    return calls


def make_report(*statuses, errors=()):
    return ScanReport(
        total_servers=len(statuses),
        faulty_servers=sum(1 for s in statuses if s.is_faulty),
        statuses=tuple(statuses),
        discovery_errors=tuple(errors),
    )


# ------------------------------------------------------------ single-scan mode
def test_once_prints_the_required_items_and_exits_zero(run_cli, write_log, capsys):
    write_log("dev/server-1.log", INFO_LINE)
    write_log("dev/server-2.log", INFO_LINE, ERR1)
    write_log("staging/server-5.log", ERR1, CRIT2, RES1)

    assert run_cli() == 0
    out = capsys.readouterr().out
    assert "Total servers discovered: 3" in out
    assert "Servers in fault: 2" in out
    assert "dev/server-2.log" in out
    assert "F001" in out
    assert "staging/server-5.log" in out
    assert "F002" in out
    assert "dev/server-1.log" not in out  # healthy servers are not listed as affected


def test_empty_org_reports_zero(run_cli, capsys):
    assert run_cli() == 0
    out = capsys.readouterr().out
    assert "Total servers discovered: 0" in out
    assert "Servers in fault: 0" in out
    assert "Affected servers: none" in out


def test_min_severity_flag_reaches_the_monitor(run_cli, write_log, capsys):
    write_log("dev/s.log", ERR1)
    assert run_cli("--min-severity=CRIT") == 0
    assert "Servers in fault: 0" in capsys.readouterr().out


def test_environment_variables_configure_a_run(org, tmp_path, write_log, capsys):
    write_log("dev/s.log", ERR1)
    env = {
        "HM_ROOT": str(org),
        "HM_STATE_FILE": str(tmp_path / "state" / "env-state.json"),
        "HM_ONCE": "true",
    }
    assert main([], env) == 0
    assert "Servers in fault: 1" in capsys.readouterr().out


def test_second_invocation_sees_resolution_through_saved_state(run_cli, write_log, capsys):
    write_log("dev/s.log", ERR1)
    assert run_cli() == 0
    assert "Servers in fault: 1" in capsys.readouterr().out

    write_log("dev/s.log", RES1, append=True)
    assert run_cli() == 0
    assert "Servers in fault: 0" in capsys.readouterr().out


def test_malformed_lines_do_not_pollute_the_report(run_cli, write_log, capsys):
    write_log("dev/s.log", "garbage", ERR1)
    assert run_cli() == 0
    out = capsys.readouterr().out
    assert "Servers in fault: 1" in out
    assert "malformed" not in out
    assert "WARNING" not in out


def test_unreadable_file_is_listed_as_a_discovery_problem(run_cli, write_log, monkeypatch, capsys):
    write_log("dev/bad.log", INFO_LINE)
    real_open = builtins.open

    def fake(file, *args, **kwargs):
        if str(file).endswith("bad.log"):
            raise PermissionError(13, "Permission denied")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(monitor_module, "open", fake, raising=False)

    assert run_cli() == 0
    out = capsys.readouterr().out
    assert "Total servers discovered: 1" in out
    assert "NOT verified healthy" in out
    assert "dev/bad.log" in out
    assert "PermissionError" in out


# ------------------------------------------------------------ invalid configs
@pytest.mark.parametrize(
    "bad_args",
    [["--interval=0"], ["--min-severity=fatal"], ["--log-level=loud"], ["--bogus"]],
)
def test_invalid_configuration_exits_2_with_message_on_stderr(run_cli, bad_args, capsys):
    assert run_cli(*bad_args) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("error:")
    assert captured.out == ""


def test_missing_root_exits_2(tmp_path, capsys):
    argv = ["--root", str(tmp_path / "nope"), "--once"]
    assert main(argv, {}) == 2
    assert "does not exist" in capsys.readouterr().err


# ------------------------------------------------------------------ loop mode
def test_loop_scans_repeatedly_until_stopped(make_settings, write_log, monkeypatch, capsys):
    write_log("dev/s.log", ERR1)
    stop = threading.Event()
    calls = stop_after(monkeypatch, 3, stop)

    assert run(make_settings(), stop) == 0
    assert capsys.readouterr().out.count("=== Server Health Report") == 3
    assert calls == [1.0, 1.0, 1.0]  # waits use the configured interval


def test_loop_picks_up_changes_between_cycles(make_settings, write_log, monkeypatch, capsys):
    write_log("dev/s.log", ERR1)
    stop = threading.Event()

    def on_wait(count):
        if count == 1:
            write_log("dev/s.log", RES1, append=True)

    stop_after(monkeypatch, 2, stop, on_wait)
    assert run(make_settings(), stop) == 0

    out = capsys.readouterr().out
    assert out.count("Servers in fault: 1") == 1
    assert out.count("Servers in fault: 0") == 1
    assert out.index("Servers in fault: 1") < out.index("Servers in fault: 0")


def test_exception_in_one_cycle_does_not_end_the_loop(
    make_settings, write_log, monkeypatch, capsys, caplog
):
    write_log("dev/s.log", ERR1)
    real_scan = Monitor.scan_once
    attempts = {"count": 0}

    def flaky(self):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("boom")
        return real_scan(self)

    monkeypatch.setattr(Monitor, "scan_once", flaky)
    stop = threading.Event()
    stop_after(monkeypatch, 2, stop)

    with caplog.at_level(logging.ERROR, logger="health_monitor.cli"):
        assert run(make_settings(), stop) == 0

    assert capsys.readouterr().out.count("Total servers discovered") == 1
    assert "scan failed" in caplog.text


def test_interrupt_saves_final_checkpoint_and_exits_cleanly(make_settings, write_log, monkeypatch):
    write_log("dev/s.log", ERR1)
    settings = make_settings()

    def interrupted(event, seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "_wait", interrupted)
    assert run(settings, threading.Event()) == 0

    saved = CheckpointStore(settings.state_file).load()
    assert set(saved["dev/s.log"].active_faults) == {"F001"}


def test_once_exits_1_when_the_scan_crashes(run_cli, monkeypatch, capsys):
    def boom(self):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(Monitor, "scan_once", boom)
    assert run_cli() == 1
    assert capsys.readouterr().out == ""


def test_state_saved_after_a_failed_scan_is_still_loadable(make_settings, write_log, monkeypatch):
    write_log("dev/s.log", ERR1, CRIT2)
    settings = dataclasses.replace(make_settings(), once=True)
    real_parse = monitor_module.parse_line
    calls = {"count": 0}

    def exploding(line):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("crash while parsing the second line")
        return real_parse(line)

    monkeypatch.setattr(monitor_module, "parse_line", exploding)
    assert run(settings) == 1

    loaded = CheckpointStore(settings.state_file).load()
    assert set(loaded["dev/s.log"].active_faults) == {"F001"}
    assert not list(settings.state_file.parent.glob("*.corrupt-*"))


# ---------------------------------------------------------------- helpers
def test_wait_returns_false_after_the_timeout():
    assert cli_module._wait(threading.Event(), 0.01) is False


def test_wait_returns_true_immediately_when_stop_is_set():
    stop = threading.Event()
    stop.set()
    assert cli_module._wait(stop, 60) is True


def test_signal_requests_stop_and_handlers_are_restored():
    stop = threading.Event()
    before = signal.getsignal(signal.SIGINT)
    with cli_module._graceful_shutdown(stop):
        signal.raise_signal(signal.SIGINT)
        assert stop.is_set()
    assert signal.getsignal(signal.SIGINT) == before


# ---------------------------------------------------------------- rendering
def test_render_lists_affected_servers_with_fault_details():
    report = make_report(ServerStatus("dev/a.log", (FAULT,)), ServerStatus("dev/b.log"))
    text = render_report(report)
    assert "Total servers discovered: 2" in text
    assert "Servers in fault: 1" in text
    assert "dev/a.log  (1 active fault)" in text
    assert "F1 [ERROR] since 2026-10-08 09:02:00: Database unavailable" in text
    assert "dev/b.log" not in text


def test_render_uses_plural_for_several_faults():
    second = Fault("F2", Severity.CRIT, datetime(2026, 10, 8, 9, 6), "Disk failure")
    text = render_report(make_report(ServerStatus("dev/a.log", (FAULT, second))))
    assert "(2 active faults)" in text


def test_render_header_includes_timestamp_when_given():
    text = render_report(make_report(), datetime(2026, 10, 17, 13, 5, 0))
    assert "(2026-10-17 13:05:00)" in text.splitlines()[0]


def test_render_neutralises_terminal_escape_sequences():
    nasty = Fault("F1", Severity.ERROR, datetime(2026, 10, 8), "\x1b[31mred\x1b[0m\x07")
    text = render_report(make_report(ServerStatus("dev/\x1b[2Jx.log", (nasty,))))
    assert "\x1b" not in text
    assert "\x07" not in text


def test_render_truncates_very_long_messages():
    long_fault = Fault("F1", Severity.ERROR, datetime(2026, 10, 8), "x" * 1000)
    text = render_report(make_report(ServerStatus("dev/a.log", (long_fault,))))
    fault_line = next(line for line in text.splitlines() if "F1 [ERROR]" in line)
    assert fault_line.endswith("...")
    assert len(fault_line) < 400


def test_render_shows_discovery_problems_and_unreadable_note():
    status = ServerStatus("dev/a.log", (FAULT,), readable=False, error="PermissionError: denied")
    text = render_report(make_report(status, errors=("dev/a.log: PermissionError",)))
    assert "[PermissionError: denied]" in text
    assert "Discovery problems" in text
    assert "NOT verified healthy" in text
