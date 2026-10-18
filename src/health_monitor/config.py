# config.py
#
# RESPONSIBILITY:
#   Build a validated, immutable Settings object from CLI args and environment.
#
# PLANNED SETTINGS (CLI flag / env var):
#   - root directory       --root / HM_ROOT            (must exist and be a directory)
#   - scan interval (s)    --interval / HM_INTERVAL    (> 0)
#   - minimum severity     --min-severity / HM_MIN_SEVERITY   (INFO|WARN|ERROR|CRIT)
#   - state file path      --state-file / HM_STATE_FILE
#   - single scan mode     --once
#   - log level            --log-level / HM_LOG_LEVEL
#
# PLANNED FUNCTIONS:
#   - Settings (frozen dataclass)
#   - load_settings(argv, environ) -> Settings
#       * precedence: CLI flag > env var > default
#       * raise ConfigError with a clear message on any invalid value
#   - ConfigError (Exception)
#
# RULES:
#   - No scanning, no state handling.
#   - Invalid severity or non-positive interval must be rejected, not defaulted.


"""Validated settings from CLI flags and environment variables.

Precedence: CLI flag > environment variable > built-in default.

    flag              env var            default
    --root            HM_ROOT            org
    --interval        HM_INTERVAL        30 (seconds, must be > 0)
    --min-severity    HM_MIN_SEVERITY    ERROR (INFO | WARN | ERROR | CRIT)
    --state-file      HM_STATE_FILE      state/state.json
    --once            HM_ONCE            false (single scan, then exit)
    --log-level       HM_LOG_LEVEL       INFO

Empty or whitespace-only environment values are treated as "not set".
Invalid values raise ConfigError; they are never silently replaced.
"""

from __future__ import annotations

import argparse
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from health_monitor.models import Severity

DEFAULT_ROOT = "org"
DEFAULT_INTERVAL = 30.0
DEFAULT_MIN_SEVERITY = Severity.ERROR
DEFAULT_STATE_FILE = "state/state.json"
DEFAULT_LOG_LEVEL = "INFO"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

ENV_ROOT = "HM_ROOT"
ENV_INTERVAL = "HM_INTERVAL"
ENV_MIN_SEVERITY = "HM_MIN_SEVERITY"
ENV_STATE_FILE = "HM_STATE_FILE"
ENV_ONCE = "HM_ONCE"
ENV_LOG_LEVEL = "HM_LOG_LEVEL"

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class ConfigError(Exception):
    """Raised when configuration is invalid. The message is user-presentable."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable, fully validated runtime settings."""

    root: Path
    interval: float
    min_severity: Severity
    state_file: Path
    once: bool = False
    log_level: str = DEFAULT_LOG_LEVEL


class _ArgumentParser(argparse.ArgumentParser):
    """Raise ConfigError instead of printing usage and calling sys.exit."""

    def error(self, message: str) -> NoReturn:
        raise ConfigError(message)


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="health-monitor",
        description="Continuously monitor server log files for active faults.",
        allow_abbrev=False,
    )
    parser.add_argument("--root", help=f"organization directory ({ENV_ROOT})")
    parser.add_argument("--interval", help=f"scan interval in seconds ({ENV_INTERVAL})")
    parser.add_argument(
        "--min-severity",
        help=f"minimum severity that raises a fault: INFO|WARN|ERROR|CRIT ({ENV_MIN_SEVERITY})",
    )
    parser.add_argument("--state-file", help=f"checkpoint file path ({ENV_STATE_FILE})")
    parser.add_argument(
        "--once",
        action="store_true",
        default=None,
        help=f"scan once, print the report and exit ({ENV_ONCE})",
    )
    parser.add_argument("--log-level", help=f"{'|'.join(LOG_LEVELS)} ({ENV_LOG_LEVEL})")
    return parser


def _shown(raw: str) -> str:
    """Bounded repr so a huge bad value cannot flood an error message."""
    return repr(raw[:40])


def _choose(
    cli_value: str | None,
    environ: Mapping[str, str],
    env_name: str,
    flag: str,
) -> tuple[str | None, str]:
    """Return (raw value or None, source label) following the precedence rules."""
    if cli_value is not None:
        return cli_value, flag
    env_value = environ.get(env_name, "").strip()
    if env_value:
        return env_value, env_name
    return None, ""


def _parse_root(raw: str, source: str) -> Path:
    if not raw.strip():
        raise ConfigError(f"{source}: must not be empty")
    try:
        resolved = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise ConfigError(
            f"{source}: root directory {_shown(raw)} does not exist or is not accessible"
        ) from None
    if not resolved.is_dir():
        raise ConfigError(f"{source}: {_shown(raw)} is not a directory")
    return resolved


def _parse_interval(raw: str, source: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(
            f"{source}: interval must be a number of seconds, got {_shown(raw)}"
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(
            f"{source}: interval must be a positive number of seconds, got {_shown(raw)}"
        )
    return value


def _parse_severity(raw: str, source: str) -> Severity:
    try:
        return Severity.from_name(raw)
    except ValueError as exc:
        raise ConfigError(f"{source}: {exc}") from None


def _parse_state_file(raw: str, source: str, root: Path) -> Path:
    if not raw.strip():
        raise ConfigError(f"{source}: must not be empty")
    try:
        path = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError):
        raise ConfigError(f"{source}: invalid state file path {_shown(raw)}") from None
    if path.is_dir():
        raise ConfigError(f"{source}: state file {_shown(raw)} is a directory")
    if path == root or root in path.parents:
        # Otherwise the checkpoint itself would be discovered as a "server".
        raise ConfigError(f"{source}: state file must not be inside the monitored root directory")
    return path


def _parse_bool(raw: str, source: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise ConfigError(f"{source}: expected true/false, got {_shown(raw)}")


def _parse_log_level(raw: str, source: str) -> str:
    level = raw.strip().upper()
    if level not in LOG_LEVELS:
        raise ConfigError(f"{source}: log level must be one of {', '.join(LOG_LEVELS)}")
    return level


def load_settings(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Build Settings from argv (default: sys.argv[1:]) and environ (default: os.environ).

    Raises ConfigError for any invalid or unknown option.
    """
    env = os.environ if environ is None else environ
    args = _build_parser().parse_args(argv)

    raw, source = _choose(args.root, env, ENV_ROOT, "--root")
    root = _parse_root(DEFAULT_ROOT if raw is None else raw, source or "default root")

    raw, source = _choose(args.interval, env, ENV_INTERVAL, "--interval")
    interval = DEFAULT_INTERVAL if raw is None else _parse_interval(raw, source)

    raw, source = _choose(args.min_severity, env, ENV_MIN_SEVERITY, "--min-severity")
    min_severity = DEFAULT_MIN_SEVERITY if raw is None else _parse_severity(raw, source)

    raw, source = _choose(args.state_file, env, ENV_STATE_FILE, "--state-file")
    state_file = _parse_state_file(
        DEFAULT_STATE_FILE if raw is None else raw, source or "default state file", root
    )

    if args.once:
        once = True
    else:
        raw, source = _choose(None, env, ENV_ONCE, "--once")
        once = False if raw is None else _parse_bool(raw, source)

    raw, source = _choose(args.log_level, env, ENV_LOG_LEVEL, "--log-level")
    log_level = DEFAULT_LOG_LEVEL if raw is None else _parse_log_level(raw, source)

    return Settings(
        root=root,
        interval=interval,
        min_severity=min_severity,
        state_file=state_file,
        once=once,
        log_level=log_level,
    )
