"""Tests for the ephemeral per-job SSH deploy key used by the push-upload
architecture: generated on origin, its public half installed/removed from a
local authorized_keys file under a unique tag, its private half pushed to
Vast only over stdin (never logged, never in argv).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from vast_hls_orchestrator.orchestration import deploy_key


def test_generate_keypair_produces_a_valid_matched_pair():
    private_bytes, public_bytes = deploy_key.generate_keypair()

    assert private_bytes.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----")
    assert public_bytes.decode().startswith("ssh-ed25519 ")

    # Confirm they're actually a matched pair, not two independent keys.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        key_path = Path(tmp) / "k"
        key_path.write_bytes(private_bytes)
        key_path.chmod(0o600)
        result = subprocess.run(
            ["ssh-keygen", "-y", "-f", str(key_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        derived_pub = result.stdout.strip().split()[:2]
        actual_pub = public_bytes.decode().strip().split()[:2]
        assert derived_pub == actual_pub


def test_install_and_remove_public_key_round_trip(tmp_path: Path):
    authorized_keys = tmp_path / "authorized_keys"
    authorized_keys.write_text("ssh-ed25519 AAAAunrelated preexisting-key\n")
    _priv, pub = deploy_key.generate_keypair()

    tag = deploy_key.install_public_key_locally(authorized_keys, pub, "job-token-1")

    content = authorized_keys.read_text()
    assert "preexisting-key" in content
    assert tag in content
    assert content.count("\n") == 2  # preexisting line + our new line

    deploy_key.remove_public_key_locally(authorized_keys, tag)

    content_after = authorized_keys.read_text()
    assert tag not in content_after
    assert "preexisting-key" in content_after


def test_remove_only_matches_exact_tag_not_a_substring(tmp_path: Path):
    authorized_keys = tmp_path / "authorized_keys"
    _priv1, pub1 = deploy_key.generate_keypair()
    _priv2, pub2 = deploy_key.generate_keypair()

    tag_a = deploy_key.install_public_key_locally(authorized_keys, pub1, "job-a")
    tag_b = deploy_key.install_public_key_locally(authorized_keys, pub2, "job-a-2")

    deploy_key.remove_public_key_locally(authorized_keys, tag_a)

    content = authorized_keys.read_text()
    assert tag_a not in content
    assert tag_b in content


def test_remove_on_missing_file_is_a_noop(tmp_path: Path):
    authorized_keys = tmp_path / "does-not-exist" / "authorized_keys"
    deploy_key.remove_public_key_locally(authorized_keys, "whatever")  # must not raise


def test_sweep_removes_only_stale_tagged_entries(tmp_path: Path):
    authorized_keys = tmp_path / "authorized_keys"
    old_ts = int(time.time()) - 100_000
    recent_ts = int(time.time()) - 5
    authorized_keys.write_text(
        "\n".join(
            [
                "ssh-ed25519 AAAAunrelated some-other-tool",
                f"ssh-ed25519 AAAAold vast-deploy-job-x-{old_ts}",
                f"ssh-ed25519 AAAArecent vast-deploy-job-y-{recent_ts}",
                "",
            ]
        )
    )

    deploy_key.sweep_stale_keys(authorized_keys, max_age_seconds=3600)

    content = authorized_keys.read_text()
    assert "some-other-tool" in content
    assert f"vast-deploy-job-x-{old_ts}" not in content
    assert f"vast-deploy-job-y-{recent_ts}" in content


def test_sweep_on_missing_file_is_a_noop(tmp_path: Path):
    authorized_keys = tmp_path / "does-not-exist" / "authorized_keys"
    deploy_key.sweep_stale_keys(authorized_keys, max_age_seconds=3600)  # must not raise


def test_install_private_key_on_remote_sends_key_only_via_stdin():
    private_key = b"-----BEGIN OPENSSH PRIVATE KEY-----\nsecret-material\n-----END-----\n"
    captured = {}

    def fake_run(cmd, *, input=None, capture_output=True, timeout=20, check=False):
        captured["cmd"] = cmd
        captured["input"] = input
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"", stderr=b"")

    import argparse

    args = argparse.Namespace(ssh_key=Path("/root/.ssh/vast_encoder"), known_hosts=Path("/tmp/kh"))
    with patch.object(deploy_key.subprocess, "run", fake_run):
        deploy_key.install_private_key_on_remote(args, "host", 22, private_key)

    assert captured["input"] == private_key
    assert all(b"secret-material" not in str(part).encode() for part in captured["cmd"])
    assert "cat > /root/.ssh/vast_deploy_key" in " ".join(captured["cmd"])


def test_install_private_key_on_remote_raises_on_failure():
    def fake_run(cmd, *, input=None, capture_output=True, timeout=20, check=False):
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout=b"", stderr=b"permission denied"
        )

    import argparse

    from vast_hls_orchestrator.core.errors import VastError

    args = argparse.Namespace(ssh_key=Path("/root/.ssh/vast_encoder"), known_hosts=Path("/tmp/kh"))
    with patch.object(deploy_key.subprocess, "run", fake_run), pytest.raises(VastError):
        deploy_key.install_private_key_on_remote(args, "host", 22, b"key")
