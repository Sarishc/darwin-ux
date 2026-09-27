"""The real worker process: `python -m darwin.worker` starts, logs, and shuts down on signals."""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.integration


def _start_worker(test_database_url: str) -> subprocess.Popen[str]:
    env = {
        **os.environ,
        "DARWIN_DATABASE_URL": test_database_url,
        "DARWIN_LOG_LEVEL": "INFO",
        "DARWIN_WORKER_POLL_INTERVAL_SECONDS": "0.2",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "darwin.worker"],
        env=env,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        text=True,
    )


def _messages(stderr: str) -> list[str]:
    return [json.loads(line)["message"] for line in stderr.splitlines() if line.startswith("{")]


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
def test_worker_process_stops_gracefully_on_signal(
    test_database_url: str, migrated_engine: object, stop_signal: signal.Signals
) -> None:
    process = _start_worker(test_database_url)
    try:
        assert process.stderr is not None
        first_line = process.stderr.readline()  # blocks until the worker has started
        assert json.loads(first_line)["message"] == "worker started"
        time.sleep(0.5)  # let it go through a few idle polls

        process.send_signal(stop_signal)
        _, rest = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()

    assert process.returncode == 0
    messages = _messages(rest)
    assert messages[-2:] == ["worker stopping", "worker stopped"]
    stopping = [json.loads(line) for line in rest.splitlines() if '"worker stopping"' in line]
    assert stopping[0]["context"]["signal"] == stop_signal.name
