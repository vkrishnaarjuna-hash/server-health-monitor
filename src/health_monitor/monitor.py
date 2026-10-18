# monitor.py
#
# RESPONSIBILITY:
#   Core engine: discover servers, read only NEW bytes, apply events to the
#   fault state, and produce counts. Returns data; does not print.
#
# PLANNED CONTENTS:
#   - Monitor(settings, checkpoint_store)
#       * scan_once() -> ScanReport
#       * discover_servers()
#           - recursive walk under root; regular files only
#           - skip symlinks (path traversal safety); store relative paths
#       * _process_file(path)
#           - compare inode/size with checkpoint:
#               unchanged         -> skip (no read)
#               grew              -> read from saved offset
#               shrank / replaced -> reset offset, re-read, KEEP unresolved faults
#           - stream in chunks; keep a bounded partial-line buffer
#           - only complete (newline-terminated) lines are parsed
#       * _apply_event(checkpoint, event)
#           - severity >= threshold and not a resolution -> open fault (ignore duplicates)
#           - RESOLVED id=X -> close only fault X (unknown id: log, no effect)
#           - INFO/WARN never clear anything
#       * file disappeared -> report as discovery error, not as healthy
#
# COMPLEXITY GOAL:
#   Common case per scan = O(number of files + newly appended bytes),
#   not O(total historical log size).
#
# RULES:
#   - Errors on one file must not stop the whole scan.
#   - No argparse, no printing, no sleeping.


"""Core engine: discover servers, read only NEW bytes, apply fault rules.

Returns data (ScanReport); never prints, sleeps or parses CLI arguments.

Totals policy
-------------
* A server is counted if its file was found this scan, OR it is missing but
  still has remembered active faults (disappearing never clears a fault).
* Unreadable files are counted but flagged (readable=False) and listed in
  discovery_errors; their known faults are kept. They are never "verified healthy".
* Missing files without active faults are dropped (unless a directory could
  not be read this scan, in which case their state is kept untouched).

Complexity
----------
Per scan: O(number of files + newly appended bytes). Unchanged files cost one
stat() and no read. A full re-read happens only after truncation/replacement.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from health_monitor.config import Settings
from health_monitor.models import (
    Fault,
    FileCheckpoint,
    LogEvent,
    ParseError,
    ScanReport,
    ServerStatus,
)
from health_monitor.parser import MAX_LINE_LENGTH, parse_line

logger = logging.getLogger(__name__)

MAX_LINE_BYTES = MAX_LINE_LENGTH * 4  # worst case UTF-8 bytes for a max-length line
MAX_DIAGNOSTICS_PER_FILE = 20


def _describe(exc: OSError) -> str:
    return f"{type(exc).__name__}: {exc.strerror or 'error'}"


def _iter_complete_lines(fh: BinaryIO, start: int) -> Iterator[tuple[bytes | None, int, int]]:
    """Yield (line, start_offset, end_offset) for COMPLETE lines only.

    `line` is None when the line was longer than MAX_LINE_BYTES (it is drained
    without being stored). A trailing line without a newline is not yielded.
    """
    pos = start
    while True:
        chunk = fh.readline(MAX_LINE_BYTES + 1)
        if not chunk:
            return
        length = len(chunk)
        oversized = False
        if not chunk.endswith(b"\n"):
            if length <= MAX_LINE_BYTES:
                return  # partial trailing line: wait for its newline
            oversized = True
            while True:  # drain the rest of the oversized line, bounded memory
                rest = fh.readline(MAX_LINE_BYTES)
                if not rest:
                    return  # EOF before newline: line still incomplete
                length += len(rest)
                if rest.endswith(b"\n"):
                    break
        yield (None if oversized else chunk), pos, pos + length
        pos += length


class Monitor:
    """Tracks every server file incrementally and keeps active-fault state."""

    def __init__(self, settings: Settings, state: dict[str, FileCheckpoint] | None = None) -> None:
        self._settings = settings
        self._state: dict[str, FileCheckpoint] = {} if state is None else state

    @property
    def state(self) -> dict[str, FileCheckpoint]:
        """Per-file state keyed by relative POSIX path (persisted by checkpoint.py)."""
        return self._state

    # ------------------------------------------------------------------ scan
    def scan_once(self) -> ScanReport:
        files, errors, dirs_ok = self._discover()
        statuses: list[ServerStatus] = []
        seen: set[str] = set()

        for rel, path in files:
            seen.add(rel)
            error = self._process_file(rel, path)
            if error is not None:
                errors.append(f"{rel}: {error}")
            statuses.append(self._status(rel, self._state.get(rel), error is None, error))

        for rel in [r for r in self._state if r not in seen]:
            cp = self._state[rel]
            if cp.active_faults:
                note = "file missing; showing last known faults"
                statuses.append(self._status(rel, cp, False, note))
                errors.append(f"{rel}: file missing (last known faults kept)")
            elif dirs_ok:
                logger.info("server file %r disappeared; dropping its state", rel)
                del self._state[rel]

        statuses.sort(key=lambda s: s.relative_path)
        return ScanReport(
            total_servers=len(statuses),
            faulty_servers=sum(1 for s in statuses if s.is_faulty),
            statuses=tuple(statuses),
            discovery_errors=tuple(errors),
        )

    # ------------------------------------------------------------- discovery
    def _discover(self) -> tuple[list[tuple[str, str]], list[str], bool]:
        """Walk the root. Returns (files as (relative, path), errors, dirs_ok)."""
        root = self._settings.root
        files: list[tuple[str, str]] = []
        errors: list[str] = []
        dirs_ok = True
        stack: list[Path] = [root]

        while stack:
            directory = stack.pop()
            try:
                with os.scandir(directory) as it:
                    entries = list(it)
            except OSError as exc:
                dirs_ok = False
                label = "<root>" if directory == root else (directory.relative_to(root).as_posix())
                errors.append(f"{label}: cannot read directory ({_describe(exc)})")
                continue

            for entry in entries:
                if entry.name.startswith("."):
                    continue  # hidden files/directories are not servers
                try:
                    if entry.is_symlink():
                        logger.debug("skipping symlink %r", entry.name)
                    elif entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        rel = Path(entry.path).relative_to(root).as_posix()
                        files.append((rel, entry.path))
                except OSError as exc:
                    dirs_ok = False
                    errors.append(f"{entry.name}: cannot inspect ({_describe(exc)})")
        return files, errors, dirs_ok

    # ------------------------------------------------------ incremental read
    def _process_file(self, rel: str, path: str) -> str | None:
        """Bring one file's state up to date. Returns an error text or None."""
        try:
            st = os.stat(path)  # os.stat (not DirEntry) so Windows gives an inode
        except OSError as exc:
            return _describe(exc)

        cp = self._state.get(rel)
        if cp is None:
            cp = FileCheckpoint(relative_path=rel, inode=st.st_ino)
            self._state[rel] = cp
        elif cp.inode != st.st_ino or st.st_size < cp.offset:
            logger.warning("file %r replaced or truncated; re-reading from start", rel)
            cp.inode = st.st_ino
            cp.offset = 0
            cp.size_seen = 0

        if st.st_size == cp.size_seen:
            return None  # nothing new since the last read

        try:
            self._read_new_lines(cp, path)
        except OSError as exc:
            return _describe(exc)
        return None

    def _read_new_lines(self, cp: FileCheckpoint, path: str) -> None:
        problems = 0
        with open(path, "rb") as fh:
            fh.seek(cp.offset)
            for raw, start, stop in _iter_complete_lines(fh, cp.offset):
                if raw is None:
                    problems += 1
                    self._note_problem(cp, start, f"line exceeds {MAX_LINE_BYTES} bytes", problems)
                else:
                    event = parse_line(raw.decode("utf-8", errors="replace"))
                    if isinstance(event, ParseError):
                        problems += 1
                        self._note_problem(cp, start, event.reason, problems, event.preview)
                    elif event is not None:
                        self._apply(cp, event, start)
                cp.offset = stop  # advance only after the line's effects are applied
                cp.size_seen = max(cp.size_seen, stop)  # invariant: offset <= size_seen
            cp.size_seen = fh.tell()
        if problems > MAX_DIAGNOSTICS_PER_FILE:
            logger.warning(
                "%d further malformed lines in %r suppressed",
                problems - MAX_DIAGNOSTICS_PER_FILE,
                cp.relative_path,
            )

    @staticmethod
    def _note_problem(
        cp: FileCheckpoint,
        offset: int,
        reason: str,
        count: int,
        preview: str = "",
    ) -> None:
        if count <= MAX_DIAGNOSTICS_PER_FILE:
            logger.warning("malformed line in %r at byte %d: %s", cp.relative_path, offset, reason)
            if preview:
                logger.debug("malformed line preview: %r", preview)

    # ------------------------------------------------------- fault lifecycle
    def _apply(self, cp: FileCheckpoint, event: LogEvent, line_start: int) -> None:
        if event.is_resolution:
            if cp.active_faults.pop(event.fault_id or "", None) is None:
                logger.info(
                    "resolution for unknown or inactive fault %r in %r ignored",
                    event.fault_id,
                    cp.relative_path,
                )
            return

        if not event.triggers_fault(self._settings.min_severity):
            return  # below threshold: ordinary entry, never clears anything

        fault_id = event.fault_id or f"auto-{line_start}"
        if fault_id in cp.seen_fault_ids:
            return  # duplicate, replay, or reuse of a resolved id: no new fault
        cp.seen_fault_ids.add(fault_id)
        cp.active_faults[fault_id] = Fault(
            fault_id=fault_id,
            severity=event.severity,
            opened_at=event.timestamp,
            message=event.message,
        )

    # ---------------------------------------------------------------- status
    @staticmethod
    def _status(
        rel: str, cp: FileCheckpoint | None, readable: bool, error: str | None
    ) -> ServerStatus:
        faults: tuple[Fault, ...] = ()
        if cp is not None and cp.active_faults:
            faults = tuple(
                sorted(cp.active_faults.values(), key=lambda f: (f.opened_at, f.fault_id))
            )
        return ServerStatus(relative_path=rel, active_faults=faults, readable=readable, error=error)
