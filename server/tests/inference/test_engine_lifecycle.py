"""Real Windows process regression: exited PIDs must not block API restart."""
import os
import subprocess
import sys

import pytest

from aiserver.operations.engine import WindowsJob, process_identity


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object lifecycle")
def test_managed_job_termination_and_exited_process_identity():
    process = subprocess.Popen([sys.executable, "-c", "import time\ntime.sleep(60)"],
                               creationflags=subprocess.CREATE_NO_WINDOW)
    job = None
    try:
        identity = process_identity(process.pid)
        assert identity["executable"].lower() == sys.executable.lower()
        job = WindowsJob(process)
        job.terminate()
        process.wait(timeout=10)
        # Keep Popen's process handle open: this is the crash/restart failure window.
        with pytest.raises(ProcessLookupError):
            process_identity(process.pid)
    finally:
        if job:
            job.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
