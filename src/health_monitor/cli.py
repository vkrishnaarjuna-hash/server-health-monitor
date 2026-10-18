# cli.py
#
# RESPONSIBILITY:
#   Program entry point and user-facing presentation.
#
# PLANNED CONTENTS:
#   - main(argv=None) -> int          (exit code; wired as console script "health-monitor")
#       * load settings (ConfigError -> message on stderr, exit code 2)
#       * configure logging to stderr (operational logs), separate from the report on stdout
#       * build Monitor + CheckpointStore
#       * --once : run one scan, print report, exit 0
#       * loop   : scan -> render -> save checkpoint -> sleep(interval) -> repeat
#       * unexpected exception in one cycle: log it, continue to the next cycle
#       * SIGINT/SIGTERM: stop cleanly, save last consistent checkpoint, exit 0
#   - render_report(report) -> str
#       * Total servers discovered: N
#       * Servers in fault: M
#       * per affected server: relative path + each active fault (id, severity, message)
#       * discovery errors listed separately (never counted as healthy)
#
# RULES:
#   - Report goes to stdout; logs go to stderr.
#   - No fault logic here.
#
# ENTRY: python -m health_monitor.cli   (or the installed console script)

"""Program entry point and presentation.

* Report -> stdout. Operational logs -> stderr. They never mix.
* --once : one scan, save checkpoint, print report, exit.
           exit 0 = ok, 1 = the scan itself crashed, 2 = invalid configuration.
* loop   : scan -> save -> print -> wait(interval) -> repeat until SIGINT/SIGTERM.
           A crash in one cycle is logged and the next cycle still runs.
* Shutdown is graceful: a final checkpoint is saved. A second signal aborts.
"""

from __future__ import annotations

import contextlib
import logging
import signal
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime

from health_monitor.checkpoint import CheckpointStore
from health_monitor.config import ConfigError, Settings, load_settings
from health_monitor.models import ScanReport
from health_monitor.monitor import Monitor

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_CONFIG = 2

MAX_DISPLAY_LENGTH = 200
WAIT_SLICE_SECONDS = 0.5  # keeps shutdown responsive (Windows can't interrupt long waits)


# ------------------------------------------------------------------ rendering
def _clean(text: str, limit: int = MAX_DISPLAY_LENGTH) -> str:
    """Make untrusted text safe for a terminal: no control characters, bounded."""
    shown = "".join(ch if ch.isprintable() else "?" for ch in text[:limit])
    return shown + "..." if len(text) > limit else shown


def render_report(report: ScanReport, generated_at: datetime | None = None) -> str:
    title = "Server Health Report"
    if generated_at is not None:
        title += f" ({generated_at:%Y-%m-%d %H:%M:%S})"
    lines = [
        f"=== {title} ===",
        f"Total servers discovered: {report.total_servers}",
        f"Servers in fault: {report.faulty_servers}",
        "",
    ]

    faulty = [status for status in report.statuses if status.is_faulty]
    if faulty:
        lines.append("Affected servers:")
        for status in faulty:
            count = len(status.active_faults)
            noun = "fault" if count == 1 else "faults"
            note = ""
            if not status.readable and status.error:
                note = f"  [{_clean(status.error)}]"
            lines.append(f"  {_clean(status.relative_path)}  ({count} active {noun}){note}")
            for fault in status.active_faults:
                lines.append(
                    f"    - {_clean(fault.fault_id)} [{fault.severity.name}] "
                    f"since {fault.opened_at:%Y-%m-%d %H:%M:%S}: "
                    f"{_clean(fault.message)}"
                )
    else:
        lines.append("Affected servers: none")

    if report.discovery_errors:
        lines.append("")
        lines.append("Discovery problems (these servers are NOT verified healthy):")
        lines.extend(f"  - {_clean(error)}" for error in report.discovery_errors)

    return "\n".join(lines)


# ------------------------------------------------------------- loop plumbing
def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _wait(stop: threading.Event, seconds: float) -> bool:
    """Sleep up to `seconds` in short slices. Returns True if stop was requested."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return stop.is_set()
        if stop.wait(min(WAIT_SLICE_SECONDS, remaining)):
            return True


@contextlib.contextmanager
def _graceful_shutdown(stop: threading.Event) -> Iterator[None]:
    """First SIGINT/SIGTERM requests a clean stop; a second one aborts."""

    def handler(_signum, _frame):
        if stop.is_set():
            raise KeyboardInterrupt
        stop.set()

    previous = {}
    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            previous[signum] = signal.signal(signum, handler)
        except ValueError:  # not running in the main thread
            continue
    try:
        yield
    finally:
        for signum, old_handler in previous.items():
            signal.signal(signum, old_handler)


def _scan_cycle(monitor: Monitor, store: CheckpointStore) -> bool:
    """One scan + save + print. Returns False if the scan itself failed."""
    try:
        report = monitor.scan_once()
    except Exception:
        logger.exception("scan failed; will retry on the next cycle")
        return False
    store.save(monitor.state)  # a failed save is logged by the store, never fatal
    print(render_report(report, datetime.now()), flush=True)
    return True


def run(settings: Settings, stop: threading.Event | None = None) -> int:
    stop = threading.Event() if stop is None else stop
    store = CheckpointStore(settings.state_file)
    monitor = Monitor(settings, store.load())
    exit_code = EXIT_OK
    logger.info(
        "monitoring %s (interval=%ss, min severity=%s, once=%s)",
        settings.root,
        settings.interval,
        settings.min_severity.name,
        settings.once,
    )

    with _graceful_shutdown(stop):
        try:
            while True:
                scanned = _scan_cycle(monitor, store)
                if settings.once:
                    exit_code = EXIT_OK if scanned else EXIT_RUNTIME
                    break
                if stop.is_set() or _wait(stop, settings.interval):
                    break
        except KeyboardInterrupt:
            logger.info("interrupted; shutting down")
        finally:
            store.save(monitor.state)  # latest consistent state, even after errors
            logger.info("shutdown complete")
    return exit_code


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    try:
        settings = load_settings(argv, environ)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    configure_logging(settings.log_level)
    return run(settings)


if __name__ == "__main__":
    raise SystemExit(main())
