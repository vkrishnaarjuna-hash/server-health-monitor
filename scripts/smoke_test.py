#!/usr/bin/env python3
"""smoke_test.py.

End-to-end smoke test for the built Server Health Monitor Docker image.
Verifies runtime execution, incremental fault tracking, resolution, configuration
validation, non-root security constraints, and exit code contracts.

Usage:
    python scripts/smoke_test.py <image-ref>
"""

import contextlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_TIMEOUT = 30


def run_docker_cmd(args: list[str], timeout: int = DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    """Execute a docker command with timeout and output capture."""
    cmd = ["docker", *args]
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        print(f"[FAIL] Command timed out after {timeout}s: {' '.join(cmd)}")
        if exc.stdout:
            print(f"Stdout:\n{exc.stdout}")
        if exc.stderr:
            print(f"Stderr:\n{exc.stderr}")
        sys.exit(1)


def set_dir_permissions(path: Path) -> None:
    """Ensure directory is writable by container's non-root UID (10001).

    In Linux CI environments, the container runs under UID 10001 which does not match
    the CI host runner's UID. Making the state directory world-writable (0o777) allows
    the container user to write checkpoints and rename temp files safely.
    """
    with contextlib.suppress(OSError):
        os.chmod(path, 0o777)  # noqa: S103 - required for container non-root UID to write in bind mount


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/smoke_test.py <image-ref>", file=sys.stderr)
        sys.exit(1)

    image_ref = sys.argv[1]
    print(f"Starting smoke tests against image: {image_ref}")

    # Use a temporary directory for test artifacts
    with tempfile.TemporaryDirectory(prefix="hm-smoke-") as tmp_root:
        tmp_path = Path(tmp_root).resolve()
        org_dir = tmp_path / "org"
        state_dir = tmp_path / "state"

        dev_dir = org_dir / "dev"
        dev_dir.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)

        set_dir_permissions(state_dir)

        # -------------------------------------------------------------
        # Scenario A: Initial scan with single active fault
        # -------------------------------------------------------------
        log_file = dev_dir / "server-1.log"
        log_file.write_text(
            "2026-10-08 09:00:00 INFO Service started\n"
            "2026-10-08 09:02:00 ERROR [id=F001] Database unavailable\n",
            encoding="utf-8",
        )

        args_a = [
            "run",
            "--rm",
            "-v",
            f"{org_dir}:/data/org:ro",
            "-v",
            f"{state_dir}:/data/state:rw",
            image_ref,
            "--once",
        ]
        res_a = run_docker_cmd(args_a)
        combined_a = res_a.stdout + "\n" + res_a.stderr

        if res_a.returncode != 0:
            print(f"[FAIL] Scenario A exited with code {res_a.returncode}")
            print(f"Stdout:\n{res_a.stdout}\nStderr:\n{res_a.stderr}")
            sys.exit(1)

        expected_tokens_a = [
            "Total servers discovered: 1",
            "Servers in fault: 1",
            "dev/server-1.log",
            "F001",
        ]
        for token in expected_tokens_a:
            if token not in combined_a:
                print(f"[FAIL] Scenario A missing expected token: {token!r}")
                print(f"Stdout:\n{res_a.stdout}\nStderr:\n{res_a.stderr}")
                sys.exit(1)

        print("[PASS] Scenario A: Initial fault detected and reported")

        # -------------------------------------------------------------
        # Scenario B: Incremental resolution using existing state
        # -------------------------------------------------------------
        with log_file.open("a", encoding="utf-8") as f:
            f.write("2026-10-08 09:10:00 INFO RESOLVED id=F001\n")

        args_b = [
            "run",
            "--rm",
            "-v",
            f"{org_dir}:/data/org:ro",
            "-v",
            f"{state_dir}:/data/state:rw",
            image_ref,
            "--once",
        ]
        res_b = run_docker_cmd(args_b)
        combined_b = res_b.stdout + "\n" + res_b.stderr

        if res_b.returncode != 0:
            print(f"[FAIL] Scenario B exited with code {res_b.returncode}")
            print(f"Stdout:\n{res_b.stdout}\nStderr:\n{res_b.stderr}")
            sys.exit(1)

        expected_tokens_b = [
            "Total servers discovered: 1",
            "Servers in fault: 0",
        ]
        for token in expected_tokens_b:
            if token not in combined_b:
                print(f"[FAIL] Scenario B missing expected token: {token!r}")
                print(f"Stdout:\n{res_b.stdout}\nStderr:\n{res_b.stderr}")
                sys.exit(1)

        print("[PASS] Scenario B: Incremental resolution verified (0 faults)")

        # -------------------------------------------------------------
        # Scenario C: Configuration error exit code contract
        # -------------------------------------------------------------
        args_c = [
            "run",
            "--rm",
            image_ref,
            "--min-severity=bogus",
        ]
        res_c = run_docker_cmd(args_c)

        if res_c.returncode != 2:
            print(
                f"[FAIL] Scenario C expected exit code 2 for invalid config, got {res_c.returncode}"
            )
            print(f"Stdout:\n{res_c.stdout}\nStderr:\n{res_c.stderr}")
            sys.exit(1)

        if "error:" not in res_c.stderr.lower():
            print("[FAIL] Scenario C expected 'error:' in stderr")
            print(f"Stderr:\n{res_c.stderr}")
            sys.exit(1)

        print("[PASS] Scenario C: Invalid configuration rejected with exit code 2")

        # -------------------------------------------------------------
        # Scenario D: Verify container runs as non-root user
        # -------------------------------------------------------------
        args_d = [
            "run",
            "--rm",
            "--entrypoint",
            "id",
            image_ref,
            "-u",
        ]
        res_d = run_docker_cmd(args_d)

        if res_d.returncode != 0:
            print(f"[FAIL] Scenario D failed to check user ID: {res_d.returncode}")
            print(f"Stderr:\n{res_d.stderr}")
            sys.exit(1)

        uid_str = res_d.stdout.strip()
        if not uid_str.isdigit() or int(uid_str) == 0:
            print(f"[FAIL] Scenario D container ran as root or invalid UID: {uid_str!r}")
            sys.exit(1)

        print(f"[PASS] Scenario D: Container runs securely as non-root (UID: {uid_str})")

    print("All smoke test scenarios passed successfully!")


if __name__ == "__main__":
    main()
