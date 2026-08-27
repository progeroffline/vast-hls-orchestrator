"""Regression tests for remote/snapshot.py's SSH polling command.

fetch_remote_snapshot builds its remote command as a plain raw string, not
an f-string, so any double braces in it are literal double braces sent to
bash as-is -- not Python format-string escaping for a single brace. Bash
requires whitespace around a brace for the group-command syntax, so a
literal double-brace parses as a bogus command name ("bash: {{: command not
found") instead of grouping the two `tail`s, silently dropping the remote
log tail.
"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from vast_hls_orchestrator.remote import snapshot as snapshot_module


def _capture_command() -> str:
    """Capture the exact bash command fetch_remote_snapshot sends over SSH,
    without actually connecting anywhere."""
    captured: dict[str, str] = {}

    def fake_ssh_run(args, host, port, command, *, timeout=30, check=False):
        captured["command"] = command
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with patch.object(snapshot_module, "ssh_run", fake_ssh_run):
        snapshot_module.fetch_remote_snapshot(argparse.Namespace(), "host", 22)

    return captured["command"]


def test_snapshot_command_has_no_literal_double_braces():
    command = _capture_command()
    assert "{{" not in command
    assert "}}" not in command


def test_snapshot_command_is_valid_bash():
    command = _capture_command()
    result = subprocess.run(
        ["bash", "-n", "-c", command], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_snapshot_command_remote_log_group_runs_correctly():
    """The `{ ...; ...; } | tr ... | tail` group must actually combine both
    tails' output, not silently drop one of them (the exact symptom of the
    `{{`/`}}` bug: the first `tail` becomes an unknown command name and its
    whole output segment vanishes)."""
    with tempfile.TemporaryDirectory() as tmp:
        bootstrap_log = Path(tmp) / "bootstrap.log"
        job_log = Path(tmp) / "job.log"
        bootstrap_log.write_text("line-from-bootstrap\n")
        job_log.write_text("line-from-job\n")

        # Point the two hardcoded /workspace log paths at our temp files
        # instead of reproducing all of /workspace locally -- every other
        # command in the script already degrades gracefully (2>/dev/null,
        # || echo ..., || true) when /workspace and nvidia-smi don't exist.
        command = _capture_command().replace(
            "/workspace/bootstrap.log", str(bootstrap_log)
        ).replace("/workspace/job.log", str(job_log))

        result = subprocess.run(
            ["bash", "-c", command], capture_output=True, text=True, check=False
        )

        assert result.returncode == 0
        assert "line-from-bootstrap" in result.stdout
        assert "line-from-job" in result.stdout
        assert "command not found" not in result.stderr


def test_fetch_remote_snapshot_parses_stage_status_progress_and_log():
    fake_stdout = (
        "META_STAGE=encoding\n"
        "META_STATUS=DONE:0\n"
        "META_DOWNLOADED=123456\n"
        "META_DURATION=600.0\n"
        "META_GPU=45,80,60,4096,24576,150\n"
        "PROGRESS_BEGIN\n"
        "frame=100\n"
        "fps=25.0\n"
        "out_time_us=4000000\n"
        "speed=1.2x\n"
        "progress=continue\n"
        "PROGRESS_END\n"
        "REMOTE_LOG_BEGIN\n"
        "line-from-bootstrap\n"
        "line-from-job\n"
        "REMOTE_LOG_END\n"
    )

    def fake_ssh_run(args, host, port, command, *, timeout=30, check=False):
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=fake_stdout, stderr=""
        )

    with patch.object(snapshot_module, "ssh_run", fake_ssh_run):
        snap = snapshot_module.fetch_remote_snapshot(argparse.Namespace(), "host", 22)

    assert snap.stage == "encoding"
    assert snap.status == "DONE:0"
    assert snap.downloaded_bytes == 123456
    assert snap.duration_seconds == 600.0
    assert snap.remote_log_tail == "line-from-bootstrap\nline-from-job"
    assert snap.encode.frame == 100
    assert snap.encode.progress == "continue"
