"""Builds the Vast.ai `onstart` bootstrap: watchdog, package install, job launch."""

from __future__ import annotations

import base64
import shlex


def build_onstart(job_script: str, failsafe_seconds: int, authorized_key: str) -> str:
    payload = base64.b64encode(job_script.encode()).decode()
    quoted_key = shlex.quote(authorized_key.strip())
    # Vast's account-level SSH key injection has been observed to report a key
    # as "attached" (confirmed via its own API) while the container's actual
    # authorized_keys never receives it -- a platform-side sync gap outside
    # this tool's control. Writing the key ourselves, here, with root access
    # inside the container, is a guarantee that doesn't depend on Vast's own
    # bookkeeping being in sync with reality. Done first, before anything else
    # that could fail or take a while.
    key_setup = f"""mkdir -p /root/.ssh
chmod 700 /root/.ssh
touch /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
grep -qxF {quoted_key} /root/.ssh/authorized_keys || printf '%s\n' {quoted_key} >> /root/.ssh/authorized_keys
"""
    bootstrap = f"""#!/usr/bin/env bash
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
{key_setup}
mkdir -p /workspace
rm -f /workspace/JOB_DONE /workspace/JOB_EXIT
printf 'bootstrap\n' > /workspace/JOB_STAGE
exec > >(tee -a /workspace/bootstrap.log) 2>&1

bootstrap_failed() {{
  rc=$?
  if [ "$rc" -ne 0 ] && [ ! -e /workspace/JOB_DONE ]; then
    printf '%s\n' "$rc" > /workspace/JOB_EXIT
    printf 'bootstrap-failed\n' > /workspace/JOB_STAGE
    touch /workspace/JOB_DONE
  fi
}}
trap bootstrap_failed EXIT

# Start the failsafe before package installation, which itself can hang or fail.
nohup bash -lc 'sleep {int(failsafe_seconds)}; if [ -z "${{CONTAINER_API_KEY:-}}" ] || [ -z "${{CONTAINER_ID:-}}" ]; then echo "Vast watchdog credentials are unavailable"; exit 1; fi; while true; do if command -v curl >/dev/null && curl -fsS --retry 5 --retry-all-errors -X DELETE -H "Authorization: Bearer $CONTAINER_API_KEY" "https://console.vast.ai/api/v0/instances/$CONTAINER_ID/"; then exit 0; fi; sleep 60; done' > /workspace/watchdog.log 2>&1 &

echo "=== Bootstrap started $(date -u +%FT%TZ) ==="
apt-get update -qq
apt-get install -y --no-install-recommends ffmpeg aria2 curl ca-certificates rsync
printf '%s' {payload!r} | base64 -d > /workspace/encode-job.sh
chmod 700 /workspace/encode-job.sh
echo "=== Starting encode job ==="
nohup /workspace/encode-job.sh > /workspace/job.log 2>&1 &
trap - EXIT
"""
    # Vast may initially evaluate onstart through /bin/sh. Keep that outer command
    # POSIX-compatible and explicitly feed the real script to bash.
    bootstrap_payload = base64.b64encode(bootstrap.encode()).decode()
    return f"printf '%s' {shlex.quote(bootstrap_payload)} | base64 -d | /bin/bash"
