# test_config.py
#
# COVERS config.py
#   - defaults applied when nothing is set
#   - environment variables are read
#   - CLI flags override environment variables
#   - invalid severity rejected with a clear message
#   - interval <= 0 or non-numeric rejected
#   - non-existent root directory rejected


"""Tests for config.load_settings."""

import dataclasses

import pytest

from health_monitor.config import ConfigError, Settings, load_settings
from health_monitor.models import Severity


@pytest.fixture
def org(tmp_path):
    root = tmp_path / "org"
    root.mkdir()
    return root


def load(org, *args, env=None):
    """Load settings with an explicit root and an isolated environment."""
    return load_settings(["--root", str(org), *args], env or {})


# ------------------------------------------------------------------ defaults
def test_defaults_are_applied(tmp_path, org, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = load_settings([], {})
    assert settings.root == org.resolve()
    assert settings.interval == 30.0
    assert settings.min_severity is Severity.ERROR
    assert settings.state_file == (tmp_path / "state" / "state.json").resolve()
    assert settings.once is False
    assert settings.log_level == "INFO"


# --------------------------------------------------------- environment / CLI
def test_environment_variables_are_read(tmp_path, org):
    env = {
        "HM_ROOT": str(org),
        "HM_INTERVAL": "5",
        "HM_MIN_SEVERITY": "warn",
        "HM_STATE_FILE": str(tmp_path / "s.json"),
        "HM_ONCE": "true",
        "HM_LOG_LEVEL": "debug",
    }
    settings = load_settings([], env)
    assert settings.root == org.resolve()
    assert settings.interval == 5.0
    assert settings.min_severity is Severity.WARN
    assert settings.state_file == (tmp_path / "s.json").resolve()
    assert settings.once is True
    assert settings.log_level == "DEBUG"


def test_cli_flags_override_environment(tmp_path, org):
    env = {"HM_INTERVAL": "5", "HM_MIN_SEVERITY": "CRIT", "HM_LOG_LEVEL": "ERROR"}
    settings = load(org, "--interval=2.5", "--min-severity=info", "--log-level=debug", env=env)
    assert settings.interval == 2.5
    assert settings.min_severity is Severity.INFO
    assert settings.log_level == "DEBUG"


def test_once_flag_overrides_environment_false(org):
    assert load(org, "--once", env={"HM_ONCE": "false"}).once is True


def test_empty_environment_values_are_treated_as_unset(org):
    env = {"HM_INTERVAL": "  ", "HM_MIN_SEVERITY": "", "HM_ONCE": " "}
    settings = load(org, env=env)
    assert settings.interval == 30.0
    assert settings.min_severity is Severity.ERROR
    assert settings.once is False


def test_settings_are_immutable(org):
    settings = load(org)
    assert isinstance(settings, Settings)
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.interval = 1.0  # type: ignore[misc]


# ------------------------------------------------------------- invalid values
def test_invalid_severity_from_cli_is_rejected_with_clear_message(org):
    with pytest.raises(ConfigError, match=r"--min-severity.*expected one of"):
        load(org, "--min-severity=fatal")


def test_invalid_severity_from_env_names_the_variable(org):
    with pytest.raises(ConfigError, match="HM_MIN_SEVERITY"):
        load(org, env={"HM_MIN_SEVERITY": "fatal"})


@pytest.mark.parametrize("value", ["0", "-1", "abc", "nan", "inf", ""])
def test_invalid_interval_from_cli_is_rejected(org, value):
    with pytest.raises(ConfigError, match="--interval"):
        load(org, f"--interval={value}")


def test_invalid_interval_from_env_names_the_variable(org):
    with pytest.raises(ConfigError, match="HM_INTERVAL"):
        load(org, env={"HM_INTERVAL": "-3"})


def test_missing_root_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="does not exist"):
        load_settings(["--root", str(tmp_path / "nope")], {})


def test_root_that_is_a_file_is_rejected(tmp_path):
    file_path = tmp_path / "file.txt"
    file_path.write_text("x")
    with pytest.raises(ConfigError, match="not a directory"):
        load_settings(["--root", str(file_path)], {})


def test_empty_root_is_rejected(org):
    with pytest.raises(ConfigError, match="must not be empty"):
        load_settings(["--root="], {})


def test_state_file_that_is_a_directory_is_rejected(tmp_path, org):
    with pytest.raises(ConfigError, match="is a directory"):
        load(org, f"--state-file={tmp_path}")


@pytest.mark.parametrize("relative", ["state.json", "sub/state.json"])
def test_state_file_inside_root_is_rejected(org, relative):
    with pytest.raises(ConfigError, match="must not be inside"):
        load(org, f"--state-file={org / relative}")


def test_invalid_log_level_is_rejected(org):
    with pytest.raises(ConfigError, match="log level"):
        load(org, "--log-level=loud")


def test_invalid_boolean_environment_value_is_rejected(org):
    with pytest.raises(ConfigError, match="HM_ONCE"):
        load(org, env={"HM_ONCE": "maybe"})


def test_unknown_argument_raises_config_error_not_system_exit(org):
    with pytest.raises(ConfigError, match="unrecognized"):
        load(org, "--bogus")


def test_abbreviated_flags_are_not_accepted(org):
    with pytest.raises(ConfigError, match="unrecognized"):
        load(org, "--int=5")
