from __future__ import annotations
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from queue import Queue

@dataclass
class Job:
    id: str
    kind: str
    argv: list[str]
    status: str = "pending"
    exit_code: int | None = None
    log_buffer: list[str] = field(default_factory=list)
    pid: int | None = None

class JobRunner:
    def __init__(self, max_running: int = 1):
        self.jobs: dict[str, Job] = {}
        self._queues: dict[str, list[Queue]] = {}
        self._lock = threading.RLock()
        self._slots = threading.BoundedSemaphore(max_running)

    def start(self, argv, cwd=None, env=None, kind="convert") -> Job:
        if not isinstance(argv, list) or not argv or any(not isinstance(x, str) for x in argv):
            raise ValueError("argv должен быть непустым списком строк")
        job = Job(uuid.uuid4().hex, kind, list(argv))
        with self._lock:
            self.jobs[job.id] = job
            self._queues[job.id] = []
        threading.Thread(target=self._run, args=(job, cwd, env), daemon=True).start()
        return job

    def _run(self, job, cwd, env):
        with self._slots:
            with self._lock: job.status = "running"
            try:
                process = subprocess.Popen(job.argv, cwd=cwd, env=env, shell=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
                job.pid = process.pid
                job._process = process
                for line in iter(process.stdout.readline, ""):
                    line = line.rstrip("\n")
                    with self._lock: job.log_buffer.append(line); queues = list(self._queues.get(job.id, []))
                    for queue in queues: queue.put(line)
                process.wait()
                with self._lock:
                    job.exit_code = process.returncode
                    if job.status == "running": job.status = "done" if process.returncode == 0 else "error"
            except Exception as exc:
                with self._lock: job.log_buffer.append(str(exc)); job.status = "error"

    def stop(self, job_id):
        job = self.get(job_id)
        process = getattr(job, "_process", None)
        if not process or process.poll() is not None: return job
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill(); process.wait()
        with self._lock: job.status = "stopped"; job.exit_code = process.returncode
        return job

    def get(self, job_id):
        if job_id not in self.jobs: raise KeyError(job_id)
        return self.jobs[job_id]

    def subscribe(self, job_id):
        queue = Queue()
        with self._lock:
            self.get(job_id)
            self._queues[job_id].append(queue)
            for line in self.jobs[job_id].log_buffer: queue.put(line)
        return queue
