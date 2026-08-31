import subprocess
import sys
import time

from firmware.src.jobs import JobRunner, build_env, _child_umask_zero

def test_sequence_streams_both_phases():
    runner=JobRunner(); job=runner.start_sequence([[sys.executable,'-c','print("A")'],[sys.executable,'-c','print("B")']])
    while runner.get(job.id).status not in {'done','error'}: pass
    assert runner.get(job.id).status=='done'
    assert runner.get(job.id).log_buffer[-1].endswith('done')
    assert any('A' in x for x in job.log_buffer) and any('B' in x for x in job.log_buffer)

def test_build_env_injects_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("INJECTED=from_file\nONLY_FILE=file_value\n", encoding="utf-8")
    monkeypatch.setenv("INJECTED", "from_os")
    monkeypatch.setenv("FROM_OS", "os_value")
    env = build_env(env_file)
    assert env["INJECTED"] == "from_file"      # файл имеет приоритет (инъекция)
    assert env["ONLY_FILE"] == "file_value"
    assert env["FROM_OS"] == "os_value"        # остальное окружение сохраняется
    assert "PATH" in env


class _FakeProcess:
    """Подмена subprocess.Popen для проверки аргументов (без реального запуска)."""
    pid = 4242
    returncode = 0

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.stdout = type("S", (), {"readline": lambda self: ""})()

    def wait(self):
        return 0

    def poll(self):
        return 0


def _wait_done(runner, job):
    for _ in range(200):
        if job.status in {"done", "error"}:
            return job.status
        time.sleep(0.01)
    return job.status


def test_run_passes_child_umask_zero(monkeypatch):
    from firmware.src import jobs
    captured = {}
    def fake_popen(argv, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeProcess(argv, **kwargs)
    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)
    runner = jobs.JobRunner()
    job = runner.start([sys.executable, "-c", "pass"])
    assert _wait_done(runner, job) == "done"
    assert captured["kwargs"].get("preexec_fn") is _child_umask_zero
    assert captured["kwargs"].get("shell") is False


def test_run_sequence_passes_child_umask_zero(monkeypatch):
    from firmware.src import jobs
    captured = []
    def fake_popen(argv, **kwargs):
        captured.append(kwargs)
        return _FakeProcess(argv, **kwargs)
    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)
    runner = jobs.JobRunner()
    job = runner.start_sequence([[sys.executable, "-c", "pass"], [sys.executable, "-c", "pass"]])
    assert _wait_done(runner, job) == "done"
    assert len(captured) == 2
    assert all(kw.get("preexec_fn") is _child_umask_zero for kw in captured)


def test_child_process_really_gets_umask_zero():
    """Реальный subprocess через JobRunner: в ребёнке umask == 0 (решение №22)."""
    runner = JobRunner()
    code = "import os; print(os.umask(0))"
    job = runner.start([sys.executable, "-c", code])
    assert _wait_done(runner, job) == "done"
    digits = [line.strip() for line in job.log_buffer if line.strip().isdigit()]
    assert digits and int(digits[-1]) == 0
