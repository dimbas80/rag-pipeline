import subprocess, sys
from firmware.src.jobs import JobRunner, build_env

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
