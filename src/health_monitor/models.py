# models.py
#
# RESPONSIBILITY:
#   Typed data containers shared by all modules. Pure data, no I/O.
#
# PLANNED CONTENTS:
#   - Severity (IntEnum): INFO=10, WARN=20, ERROR=30, CRIT=40
#       * helper to parse a name like "error" -> Severity.ERROR (case-insensitive)
#   - LogEvent (frozen dataclass): timestamp, severity, fault_id (optional),
#       message, is_resolution (bool)
#   - Fault (frozen dataclass): fault_id, severity, opened_at, message
#   - FileCheckpoint (dataclass): relative_path, inode, size_seen, offset,
#       partial_line_buffer, active_faults (dict id -> Fault),
#       seen_fault_ids (set; prevents duplicate / reused ids)
#   - ServerStatus (frozen dataclass): relative_path, active_faults (tuple),
#       readable (bool), error (optional str)
#   - ScanReport (frozen dataclass): total_servers, faulty_servers,
#       statuses, discovery_errors
#
# RULES:
#   - Keep everything serializable to plain JSON types (for checkpoint.py).
#   - No business logic here.


"""Typed data containers shared by all modules. Pure data: no I/O, no policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum


class Severity(IntEnum):
    """Log severities. A fault triggers when rank >= configured threshold."""

    INFO = 10
    WARN = 20
    ERROR = 30
    CRIT = 40

    @classmethod
    def from_name(cls, name: str) -> Severity:
        """Case-insensitive lookup. Raises ValueError with a clear message."""
        try:
            return cls[name.strip().upper()]
        except (KeyError, AttributeError):
            valid = ", ".join(member.name for member in cls)
            raise ValueError(
                f"Unknown severity {str(name)[:16]!r}; expected one of: {valid}"
            ) from None


@dataclass(frozen=True, slots=True)
class LogEvent:
    """One successfully parsed log line."""

    timestamp: datetime
    severity: Severity
    message: str
    fault_id: str | None = None  # None for lines without an explicit [id=...]
    is_resolution: bool = False  # True only for "INFO RESOLVED id=<ID>" lines

    def triggers_fault(self, threshold: Severity) -> bool:
        """Resolution lines never open faults, whatever their severity."""
        return not self.is_resolution and self.severity >= threshold


@dataclass(frozen=True, slots=True)
class ParseError:
    """A rejected line. Carries a bounded, sanitized preview, never the full line."""

    reason: str
    preview: str


@dataclass(frozen=True, slots=True)
class Fault:
    """An active fault on a server."""

    fault_id: str
    severity: Severity
    opened_at: datetime
    message: str


@dataclass(slots=True)
class FileCheckpoint:
    """Per-file monitoring state (mutable; persisted by checkpoint.py).

    `offset` only ever points just past the last COMPLETE line that was
    applied, so no partial-line buffer needs to be stored: an unfinished
    trailing line is simply re-read on the next scan.
    """

    relative_path: str
    inode: int
    size_seen: int = 0
    offset: int = 0
    active_faults: dict[str, Fault] = field(default_factory=dict)
    # Every fault id ever opened on this file (resolved ones included).
    # Makes replay idempotent and prevents resolved ids from being re-opened.
    seen_fault_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class ServerStatus:
    """Current view of one server, produced by the monitor for the report."""

    relative_path: str
    active_faults: tuple[Fault, ...] = ()
    readable: bool = True
    error: str | None = None

    @property
    def is_faulty(self) -> bool:
        return bool(self.active_faults)


@dataclass(frozen=True, slots=True)
class ScanReport:
    """Result of one scan, rendered by cli.py."""

    total_servers: int
    faulty_servers: int
    statuses: tuple[ServerStatus, ...] = ()
    discovery_errors: tuple[str, ...] = ()
