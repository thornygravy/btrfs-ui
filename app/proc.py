"""Running external commands. Nothing here ever goes through a shell."""
import os
import secrets
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque

ENV = {**os.environ, "LC_ALL": "C", "LANG": "C"}


class CmdError(Exception):
    """A command failed. `message` is safe to show to the user."""

    def __init__(self, message, rc=None):
        super().__init__(message)
        self.message = message
        self.rc = rc


def clean_error(text):
    lines = []
    for line in (text or "").strip().splitlines():
        line = line.strip()
        if line.startswith("ERROR: "):
            line = line[7:]
        if line:
            lines.append(line)
    return "; ".join(lines) or "command failed"


def run(args, timeout=60, check=True):
    """Run a command, return (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(
            args, capture_output=True, text=True, errors="replace",
            timeout=timeout, env=ENV, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise CmdError(f"'{args[0]}' is not installed in this container")
    except subprocess.TimeoutExpired:
        raise CmdError(f"Timed out after {timeout}s: {' '.join(args[:4])}")
    if check and p.returncode != 0:
        raise CmdError(clean_error(p.stderr or p.stdout), p.returncode)
    return p.returncode, p.stdout, p.stderr


def run_detached(args, wait=30):
    """Run a command that forks itself into the background (scrub start,
    balance start --background) and return its output once the launcher exits.

    Output goes to a temp file rather than a pipe, otherwise the background
    child would keep the pipe open and we would wait for it forever.
    """
    with tempfile.TemporaryFile() as out:
        try:
            p = subprocess.Popen(
                args, stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=ENV, start_new_session=True)
        except FileNotFoundError:
            raise CmdError(f"'{args[0]}' is not installed in this container")
        try:
            rc = p.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            threading.Thread(target=p.wait, daemon=True).start()  # reap later
            return None, ""
        out.seek(0)
        text = out.read().decode(errors="replace")
    if rc != 0:
        raise CmdError(clean_error(text), rc)
    return rc, text


class Job:
    def __init__(self, title, key, args):
        self.id = secrets.token_hex(4)
        self.title = title
        self.key = key
        self.args = args
        self.state = "running"          # running | done | failed | cancelled
        self.rc = None
        self.started = time.time()
        self.ended = None
        self.cancelled = False
        self.proc = None
        self._lines = deque(maxlen=400)
        self._lock = threading.Lock()

    def add_line(self, line):
        with self._lock:
            self._lines.append(line)

    def to_dict(self, full=False):
        with self._lock:
            lines = list(self._lines)
        return {
            "id": self.id,
            "title": self.title,
            "state": self.state,
            "rc": self.rc,
            "started": self.started,
            "ended": self.ended,
            "command": " ".join(shlex.quote(a) for a in self.args),
            "output": lines if full else lines[-6:],
        }


class JobManager:
    """Runs long commands (defrag, trim, snapper cleanup) in the background."""

    def __init__(self, keep=40):
        self._jobs = []
        self._lock = threading.Lock()
        self._keep = keep

    def start(self, title, key, args):
        with self._lock:
            for j in self._jobs:
                if j.key == key and j.state == "running":
                    raise CmdError("The same job is already running")
            job = Job(title, key, args)
            self._jobs.append(job)
            finished = [j for j in self._jobs if j.state != "running"]
            while len(self._jobs) > self._keep and finished:
                self._jobs.remove(finished.pop(0))
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _run(self, job):
        try:
            job.proc = subprocess.Popen(
                job.args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=ENV, text=True, errors="replace",
                bufsize=1, start_new_session=True)
        except OSError as exc:
            job.add_line(str(exc))
            job.state, job.rc, job.ended = "failed", -1, time.time()
            return
        for line in job.proc.stdout:
            job.add_line(line.rstrip("\n"))
        job.rc = job.proc.wait()
        job.ended = time.time()
        if job.cancelled:
            job.state = "cancelled"
        else:
            job.state = "done" if job.rc == 0 else "failed"

    def get(self, job_id):
        with self._lock:
            return next((j for j in self._jobs if j.id == job_id), None)

    def list(self):
        with self._lock:
            return list(reversed(self._jobs))

    def cancel(self, job_id):
        job = self.get(job_id)
        if not job or job.state != "running" or not job.proc:
            return False
        job.cancelled = True
        try:
            os.killpg(job.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        return True
