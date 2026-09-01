"""Regression tests for the onstart payload's size and gzip+base64 encoding.

A real PUT /api/v0/asks/<id>/ against the live Vast API rejected instance
creation with HTTP 400 "Invalid args: len(image) > 1024, or len(args) >
16384, or len(label) > 256" -- image and label were both tiny, so the actual
culprit was the onstart payload (plain base64, no compression) coming in at
16852 bytes, just over that 16384 boundary. docs.vast.ai's own
create-instance reference names gzip+base64 as the documented fix for a
large onstart, which is what build_onstart now does for both the inner job
script payload and the outer bootstrap wrapper.
"""

from __future__ import annotations

import subprocess

from vast_hls_orchestrator.remote.job_script import build_job_script
from vast_hls_orchestrator.remote.onstart import build_onstart

# The exact limit the live Vast API enforced in the 400 response above.
VAST_ONSTART_BYTE_LIMIT = 16384


def _build(url: str = "https://origin.example.com/video/test.mp4") -> tuple[str, str]:
    job_script = build_job_script(
        url,
        expected_input_bytes=9_430_000_000,
        staging_remote_path="/var/www/html/video/test/abr.staging.test-123-456",
        origin_ssh_user="root",
        origin_ssh_host="203.0.113.7",
        origin_ssh_port=22,
        upload_workers=16,
        upload_worker_stagger=0.5,
        upload_retries=4,
    )
    onstart = build_onstart(
        job_script, failsafe_seconds=14400, authorized_key="ssh-ed25519 AAAAtest test@host"
    )
    return job_script, onstart


def test_onstart_stays_under_vast_size_limit():
    _job_script, onstart = _build()
    size = len(onstart.encode("utf-8"))
    assert size < VAST_ONSTART_BYTE_LIMIT, (
        f"onstart is {size} bytes, at or over Vast's {VAST_ONSTART_BYTE_LIMIT}-byte "
        "limit on the field it ends up in (confirmed via a real 400 response)"
    )


def test_onstart_outer_layer_decodes_to_valid_bash():
    _job_script, onstart = _build()
    # onstart is "printf '%s' PAYLOAD | base64 -d | gunzip | /bin/bash" --
    # swap the final exec for `cat` to inspect the decoded bootstrap instead
    # of running it.
    inspect_cmd = onstart.rsplit("| /bin/bash", 1)[0] + "| cat"
    result = subprocess.run(
        ["bash", "-c", inspect_cmd], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    bootstrap = result.stdout
    assert bootstrap.startswith("#!/usr/bin/env bash")
    assert "bootstrap_failed" in bootstrap

    syntax = subprocess.run(
        ["bash", "-n"], input=bootstrap, capture_output=True, text=True, check=False
    )
    assert syntax.returncode == 0, syntax.stderr


def test_onstart_inner_layer_roundtrips_job_script_exactly():
    job_script, onstart = _build()
    inspect_cmd = onstart.rsplit("| /bin/bash", 1)[0] + "| cat"
    bootstrap = subprocess.run(
        ["bash", "-c", inspect_cmd], capture_output=True, text=True, check=False
    ).stdout

    inner_lines = [
        line for line in bootstrap.splitlines() if "base64 -d | gunzip >" in line
    ]
    assert inner_lines, "expected a gunzip-decode line writing encode-job.sh"
    inner_cmd = inner_lines[0].replace("> /workspace/encode-job.sh", "")

    result = subprocess.run(
        ["bash", "-c", inner_cmd], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == job_script
