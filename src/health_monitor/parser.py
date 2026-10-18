# parser.py
#
# RESPONSIBILITY:
#   Convert ONE line of text into a LogEvent, or report why it is invalid.
#   Logs are untrusted input: never raise on bad content, never use eval.
#
# SUPPORTED SYNTAX:
#   Normal event:
#     YYYY-MM-DD HH:MM:SS <SEVERITY> [id=<FAULT_ID>] <message>
#   Resolution event:
#     YYYY-MM-DD HH:MM:SS INFO RESOLVED id=<FAULT_ID>
#   Fault id format: letters/digits/_/- , bounded length (e.g. 1..64)
#
# PLANNED FUNCTIONS:
#   - parse_line(line: str) -> LogEvent | ParseError
#       * validate timestamp, severity, id format, RESOLVED syntax
#       * enforce a max line length (oversized lines are rejected)
#       * a line that is alert-eligible but has no id: decided at implementation
#         (planned: derive a stable id from file + line offset, or reject with a diagnostic)
#   - ParseError (small dataclass: reason, truncated line preview)
#
# RULES:
#   - Pure function: no filesystem, no state, no logging of full line content.
#   - RESOLVED lines must be clearly distinguishable from plain INFO lines.


"""Parse ONE log line into a LogEvent. Logs are untrusted input.

Supported syntax
----------------
Normal event:
    YYYY-MM-DD HH:MM:SS <SEVERITY> [id=<FAULT_ID>] <message>
    (the [id=...] tag is optional; the monitor derives an id when it is absent)

Resolution event (explicit, distinguishable from ordinary INFO):
    YYYY-MM-DD HH:MM:SS INFO RESOLVED id=<FAULT_ID>

FAULT_ID: 1-64 characters from [A-Za-z0-9_.-].

Contract: parse_line never raises. It returns a LogEvent, a ParseError, or
None for blank lines.
"""

from __future__ import annotations

import re
from datetime import datetime

from health_monitor.models import LogEvent, ParseError, Severity

MAX_LINE_LENGTH = 4096
PREVIEW_LENGTH = 80
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

_ID = r"[A-Za-z0-9_.-]{1,64}"

_LINE_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) +(?P<sev>[A-Za-z]+)(?: +(?P<rest>.*))?",
    re.ASCII,
)
_RESOLVE_RE = re.compile(rf"RESOLVED id=(?P<id>{_ID})", re.ASCII)
_TAG_RE = re.compile(rf"\[id=(?P<id>{_ID})\](?: +(?P<msg>.*))?", re.ASCII)


def _preview(text: str) -> str:
    """Bounded, printable-only excerpt, safe to put into logs."""
    shown = "".join(ch if ch.isprintable() else "?" for ch in text[:PREVIEW_LENGTH])
    return shown + "..." if len(text) > PREVIEW_LENGTH else shown


def _error(reason: str, text: str) -> ParseError:
    return ParseError(reason=reason, preview=_preview(text))


def parse_line(line: str) -> LogEvent | ParseError | None:
    """Parse one line. Returns None for blank lines (nothing to report)."""
    text = line.strip()
    if not text:
        return None
    if len(text) > MAX_LINE_LENGTH:
        return _error(f"line exceeds {MAX_LINE_LENGTH} characters", text)

    match = _LINE_RE.fullmatch(text)
    if match is None:
        return _error("expected '<YYYY-MM-DD HH:MM:SS> <SEVERITY> <message>'", text)

    try:
        timestamp = datetime.strptime(match["ts"], TIMESTAMP_FORMAT)
    except ValueError:
        return _error("invalid timestamp", text)

    try:
        severity = Severity.from_name(match["sev"])
    except ValueError:
        return _error(f"unknown severity {match['sev'][:16]!r}", text)

    rest = match["rest"] or ""

    # Resolution lines are recognised by the literal first word RESOLVED.
    if rest.split(" ", 1)[0] == "RESOLVED":
        return _parse_resolution(timestamp, severity, rest, text)

    # Optional explicit fault id tag at the start of the message.
    if rest.startswith("[id="):
        tag = _TAG_RE.fullmatch(rest)
        if tag is None:
            return _error("invalid fault id (allowed: A-Z a-z 0-9 _ . -, max 64)", text)
        return LogEvent(
            timestamp=timestamp,
            severity=severity,
            message=tag["msg"] or "",
            fault_id=tag["id"],
        )

    return LogEvent(timestamp=timestamp, severity=severity, message=rest)


def _parse_resolution(
    timestamp: datetime, severity: Severity, rest: str, text: str
) -> LogEvent | ParseError:
    if severity is not Severity.INFO:
        return _error("resolution lines must use INFO severity", text)
    match = _RESOLVE_RE.fullmatch(rest)
    if match is None:
        return _error("malformed resolution; expected 'RESOLVED id=<FAULT_ID>'", text)
    return LogEvent(
        timestamp=timestamp,
        severity=severity,
        message=rest,
        fault_id=match["id"],
        is_resolution=True,
    )
