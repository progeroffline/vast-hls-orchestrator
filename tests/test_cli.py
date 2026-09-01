"""Tests for the new CLI flags introduced by the direct-push upload
architecture: --origin-ssh-host/-port/-user, --origin-authorized-keys,
--upload-workers, --upload-worker-stagger.
"""

from __future__ import annotations

import sys
from pathlib import Path

from vast_hls_orchestrator.cli import parse_args

REQUIRED_ARGS = [
    "--video-id",
    "test",
    "--source-url",
    "https://origin.example.com/video/test.mp4",
    "--ssh-key",
    "/root/.ssh/vast_encoder",
    "--origin-ssh-host",
    "203.0.113.7",
]


def _parse(extra: list[str] | None = None, argv0: str = "vast-hls-orchestrator"):
    old_argv = sys.argv
    sys.argv = [argv0, *REQUIRED_ARGS, *(extra or [])]
    try:
        return parse_args()
    finally:
        sys.argv = old_argv


def test_origin_ssh_host_is_required():
    old_argv = sys.argv
    sys.argv = [
        "vast-hls-orchestrator",
        "--video-id",
        "test",
        "--source-url",
        "https://origin.example.com/video/test.mp4",
        "--ssh-key",
        "/root/.ssh/vast_encoder",
    ]
    try:
        try:
            parse_args()
        except SystemExit as exc:
            assert exc.code != 0
        else:
            raise AssertionError("expected argparse to reject a missing --origin-ssh-host")
    finally:
        sys.argv = old_argv


def test_upload_flag_defaults():
    args = _parse()
    assert args.origin_ssh_port == 22
    assert args.upload_workers == 16
    assert args.upload_worker_stagger == 0.5
    assert args.origin_authorized_keys == Path.home() / ".ssh" / "authorized_keys"
    assert args.origin_ssh_user  # defaults to the current OS user, non-empty


def test_upload_flag_overrides():
    args = _parse(
        [
            "--origin-ssh-port",
            "2222",
            "--origin-ssh-user",
            "deployuser",
            "--origin-authorized-keys",
            "/home/deployuser/.ssh/authorized_keys",
            "--upload-workers",
            "8",
            "--upload-worker-stagger",
            "1.0",
        ]
    )
    assert args.origin_ssh_port == 2222
    assert args.origin_ssh_user == "deployuser"
    assert args.origin_authorized_keys == Path("/home/deployuser/.ssh/authorized_keys")
    assert args.upload_workers == 8
    assert args.upload_worker_stagger == 1.0
