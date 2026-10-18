# test_parser.py
#
# COVERS parser.py
#   - each severity (INFO, WARN, ERROR, CRIT) parses correctly
#   - valid RESOLVED line recognized as a resolution, not plain INFO
#   - malformed timestamp -> ParseError
#   - unknown severity -> ParseError
#   - invalid / missing / oversized fault id -> ParseError
#   - malformed RESOLVED syntax -> ParseError
#   - empty and whitespace-only lines handled
#   - overly long line rejected without crashing


"""Tests for models.Severity / LogEvent and parser.parse_line."""

from datetime import datetime

import pytest

from health_monitor.models import LogEvent, ParseError, Severity
from health_monitor.parser import MAX_LINE_LENGTH, PREVIEW_LENGTH, parse_line

TS = "2026-10-08 09:00:00"


def parsed(line: str) -> LogEvent:
    result = parse_line(line)
    assert isinstance(result, LogEvent), f"expected LogEvent, got {result!r}"
    return result


def rejected(line: str) -> ParseError:
    result = parse_line(line)
    assert isinstance(result, ParseError), f"expected ParseError, got {result!r}"
    return result


# --------------------------------------------------------------- valid lines
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("INFO", Severity.INFO),
        ("WARN", Severity.WARN),
        ("ERROR", Severity.ERROR),
        ("CRIT", Severity.CRIT),
    ],
)
def test_every_severity_parses_with_id(name, expected):
    event = parsed(f"{TS} {name} [id=F001] Something happened")
    assert event.severity is expected
    assert event.fault_id == "F001"
    assert event.message == "Something happened"
    assert event.timestamp == datetime(2026, 10, 8, 9, 0, 0)
    assert event.is_resolution is False


def test_severity_is_case_insensitive():
    assert parsed(f"{TS} error Disk full").severity is Severity.ERROR


def test_line_without_id_has_no_fault_id():
    event = parsed("2026-10-08 09:02:00 ERROR Database unavailable")
    assert event.fault_id is None
    assert event.message == "Database unavailable"


def test_severity_without_message_is_allowed():
    event = parsed(f"{TS} INFO")
    assert event.message == ""


def test_trailing_newline_and_carriage_return_are_ignored():
    event = parsed(f"{TS} WARN High memory usage\r\n")
    assert event.message == "High memory usage"


@pytest.mark.parametrize("line", ["", "   ", "\n", "\r\n", "\t"])
def test_blank_lines_return_none(line):
    assert parse_line(line) is None


# --------------------------------------------------------------- resolution
def test_resolution_line_is_recognised():
    event = parsed("2026-10-08 09:10:00 INFO RESOLVED id=F001")
    assert event.is_resolution is True
    assert event.fault_id == "F001"
    assert event.severity is Severity.INFO


def test_info_mentioning_resolved_mid_message_is_not_a_resolution():
    event = parsed(f"{TS} INFO Operator says issue RESOLVED id=F001 by hand")
    assert event.is_resolution is False
    assert event.fault_id is None


@pytest.mark.parametrize(
    ("line", "reason_part"),
    [
        (f"{TS} ERROR RESOLVED id=F001", "INFO"),
        (f"{TS} WARN RESOLVED id=F001", "INFO"),
        (f"{TS} INFO RESOLVED", "malformed resolution"),
        (f"{TS} INFO RESOLVED id=", "malformed resolution"),
        (f"{TS} INFO RESOLVED id=F 001", "malformed resolution"),
        (f"{TS} INFO RESOLVED id=F001 extra", "malformed resolution"),
        (f"{TS} INFO RESOLVED id=F001 id=F002", "malformed resolution"),
    ],
)
def test_malformed_resolutions_are_rejected(line, reason_part):
    assert reason_part in rejected(line).reason


# ------------------------------------------------------------ invalid lines
@pytest.mark.parametrize(
    ("line", "reason_part"),
    [
        ("not a log line", "expected"),
        ("2026-10-08T09:00:00 INFO x", "expected"),
        ("2026-10-08 09:00 INFO x", "expected"),
        ("2026-13-08 09:00:00 INFO x", "timestamp"),
        ("2026-10-32 09:00:00 INFO x", "timestamp"),
        ("2026-10-08 25:00:00 INFO x", "timestamp"),
        (f"{TS} FATAL Boom", "unknown severity"),
        (f"{TS} 123 Boom", "expected"),
    ],
)
def test_malformed_lines_are_rejected_with_reason(line, reason_part):
    assert reason_part in rejected(line).reason


@pytest.mark.parametrize(
    "tag",
    ["[id=]", "[id=bad id]", "[id=F1", "[id=F/1]", "[id=" + "A" * 65 + "]"],
)
def test_invalid_fault_id_tags_are_rejected(tag):
    assert "invalid fault id" in rejected(f"{TS} ERROR {tag} msg").reason


def test_fault_id_at_max_length_is_accepted():
    event = parsed(f"{TS} ERROR [id={'A' * 64}] msg")
    assert event.fault_id == "A" * 64


def test_unicode_digits_in_timestamp_are_rejected():
    # Arabic-Indic digits must not sneak past the ASCII-only grammar.
    assert isinstance(parse_line("٢٠٢٦-١٠-٠٨ ٠٩:٠٠:٠٠ INFO x"), ParseError)


# ------------------------------------------------------ untrusted-input safety
def test_oversized_line_is_rejected():
    error = rejected(f"{TS} INFO " + "x" * MAX_LINE_LENGTH)
    assert "exceeds" in error.reason


def test_preview_is_bounded_and_sanitized():
    error = rejected("garbage\x1b[31m" + "y" * 500)
    assert "\x1b" not in error.preview
    assert error.preview.endswith("...")
    assert len(error.preview) <= PREVIEW_LENGTH + 3


@pytest.mark.parametrize(
    "line",
    ["\x00\x00\x00", "💥" * 50, f"{TS} ERROR [id=F1", f"{TS} \x00 x", "\ufeff" + TS],
)
def test_parser_never_raises_on_odd_input(line):
    result = parse_line(line)
    assert result is None or isinstance(result, (LogEvent, ParseError))


# ---------------------------------------------------------- Severity / models
def test_severity_ranks_and_ordering():
    assert [s.value for s in Severity] == [10, 20, 30, 40]
    assert Severity.INFO < Severity.WARN < Severity.ERROR < Severity.CRIT


@pytest.mark.parametrize(
    ("text", "expected"),
    [("info", Severity.INFO), (" Crit ", Severity.CRIT), ("WARN", Severity.WARN)],
)
def test_severity_from_name(text, expected):
    assert Severity.from_name(text) is expected


@pytest.mark.parametrize("bad", ["", "fatal", "ERR", "10"])
def test_severity_from_name_rejects_invalid(bad):
    with pytest.raises(ValueError, match="expected one of: INFO, WARN, ERROR, CRIT"):
        Severity.from_name(bad)


@pytest.mark.parametrize(
    ("severity", "threshold", "expected"),
    [
        (Severity.INFO, Severity.ERROR, False),
        (Severity.WARN, Severity.ERROR, False),
        (Severity.ERROR, Severity.ERROR, True),
        (Severity.CRIT, Severity.ERROR, True),
        (Severity.WARN, Severity.WARN, True),
        (Severity.ERROR, Severity.CRIT, False),
    ],
)
def test_triggers_fault_uses_rank_threshold(severity, threshold, expected):
    event = LogEvent(datetime(2026, 10, 8), severity, "m")
    assert event.triggers_fault(threshold) is expected


def test_resolution_event_never_triggers_a_fault():
    event = parsed("2026-10-08 09:10:00 INFO RESOLVED id=F001")
    assert event.triggers_fault(Severity.INFO) is False
