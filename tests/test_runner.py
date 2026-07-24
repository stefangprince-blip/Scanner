import sys
import subprocess


def test_script_runs_successfully():
    # Run the script-style test harness and ensure it exits with code 0
    res = subprocess.run(
        [sys.executable, "test_scanner.py"], capture_output=True, text=True
    )
    # If it failed, show stdout/stderr for diagnostics
    if res.returncode != 0:
        print("STDOUT:\n", res.stdout)
        print("STDERR:\n", res.stderr)
    assert res.returncode == 0, f"test_scanner.py exited with {res.returncode}"
