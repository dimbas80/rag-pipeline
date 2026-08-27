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

try:  # supports both documented package and legacy module invocation
    from firmware.src.config_ui import read_env_raw
except ImportError:  # pragma: no cover - direct `uvicorn app:app` from firmware/src
    from config_ui import read_env_raw


def _child_umask_zero() -> None:
    """В дочернем процессе: umask 0 — файлы/каталоги пайплайнов под !База_ГОСТ
    (mkdir(0o777), файлы 0o666) не урезаются umask'ом (решение №22).

    Выполняется в ребёнке между fork и exec. os.umask — тривиальный syscall-
    враппер без Python-локов, поэтому для многопоточного FastAPI риск deadlock
    практически нулевой (стандартная идиома; shell=False и списки argv
    сохраняются). Родительский umask не меняется.
    """
    os.umask(0)


def build_env(env_file=None) -> dict[str, str]:
    """Окружение для subprocess пайплайнов (§8.2 архитектуры).

    Ключи из общего `.env` инжектируются поверх текущего окружения и имеют
    приоритет (пайплайны грузят свои `.env` через setdefault/override=False).
    """
    env = dict(os.environ)
    if env_file:
        env.update(read_env_raw(env_file))
    return env


@dataclass
class Job:
    id: str
    kind: str
    argv: list[str]
    status: str = "pending"
    exit_code: int | None = None
    log_buffer: list[str] = field(default_factory=list)
    pid: int | None = None
    phases: list[list[str]] | None = None

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

    def start_sequence(self, phases, cwd=None, env=None, kind="sequence") -> Job:
        if not phases or any(not isinstance(argv, list) or not argv for argv in phases):
            raise ValueError("phases должны быть непустым списком argv")
        if any(any(not isinstance(x, str) for x in argv) for argv in phases):
            raise ValueError("argv должен содержать только строки")
        job = Job(uuid.uuid4().hex, kind, list(phases[0]))
        job.phases = [list(argv) for argv in phases]
        with self._lock:
            self.jobs[job.id] = job
            self._queues[job.id] = []
        threading.Thread(target=self._run_sequence, args=(job, cwd, env), daemon=True).start()
        return job

    def _emit(self, job, line):
        with self._lock:
            job.log_buffer.append(line)
            queues = list(self._queues.get(job.id, []))
        for queue in queues:
            queue.put(line)

    def _run_sequence(self, job, cwd, env):
        with self._slots:
            with self._lock: job.status = "running"
            try:
                for index, argv in enumerate(job.phases, 1):
                    self._emit(job, f"[phase {index}/{len(job.phases)}] started")
                    process = subprocess.Popen(argv, cwd=cwd, env=env, shell=False,
                                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                               text=True, bufsize=1, preexec_fn=_child_umask_zero)
                    job.pid = process.pid; job._process = process
                    for line in iter(process.stdout.readline, ""):
                        self._emit(job, line.rstrip("\n"))
                    process.wait(); job.exit_code = process.returncode
                    if process.returncode != 0:
                        with self._lock: job.status = "error"
                        self._emit(job, f"[phase {index}/{len(job.phases)}] failed ({process.returncode})")
                        return
                    self._emit(job, f"[phase {index}/{len(job.phases)}] done")
                with self._lock: job.status = "done"
            except Exception as exc:
                self._emit(job, str(exc))
                with self._lock: job.status = "error"

    def _run(self, job, cwd, env):
        with self._slots:
            with self._lock: job.status = "running"
            try:
                process = subprocess.Popen(job.argv, cwd=cwd, env=env, shell=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, preexec_fn=_child_umask_zero)
                job.pid = process.pid
                job._process = process
                for line in iter(process.stdout.readline, ""):
                    line = line.rstrip("\n")
                    self._emit(job, line)
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
