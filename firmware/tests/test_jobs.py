import subprocess, sys
from firmware.src.jobs import JobRunner

def test_sequence_streams_both_phases():
    runner=JobRunner(); job=runner.start_sequence([[sys.executable,'-c','print("A")'],[sys.executable,'-c','print("B")']])
    while runner.get(job.id).status not in {'done','error'}: pass
    assert runner.get(job.id).status=='done'
    assert runner.get(job.id).log_buffer[-1].endswith('done')
    assert any('A' in x for x in job.log_buffer) and any('B' in x for x in job.log_buffer)
