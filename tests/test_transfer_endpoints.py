from __future__ import annotations

from argparse import Namespace

import pytest

from vast_hls_orchestrator.core.console import console
from vast_hls_orchestrator.core.errors import VastError
from vast_hls_orchestrator.core.models import RemoteSnapshot
from vast_hls_orchestrator.orchestration import job_monitor, provisioning
from vast_hls_orchestrator.remote.ssh import ssh_base
from vast_hls_orchestrator.vast_api.client import VastClient
from vast_hls_orchestrator.vast_api.offers import choose_offers


def _instance_info() -> dict:
    return {
        "actual_status": "running",
        "public_ipaddr": "203.0.113.10",
        "ports": {"22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "40122"}]},
        "ssh_host": "ssh7.vast.ai",
        "ssh_port": 17022,
    }


def test_endpoint_candidates_keep_direct_and_proxy_fields_separate():
    endpoints = provisioning.ssh_endpoint_candidates(_instance_info())

    assert [(item.kind, item.host, item.port) for item in endpoints] == [
        ("direct", "203.0.113.10", 40122),
        ("proxy", "ssh7.vast.ai", 17022),
    ]
    assert all((item.host, item.port) != ("203.0.113.10", 17022) for item in endpoints)


@pytest.mark.parametrize(
    "ports",
    [None, {}, {"22/tcp": []}, {"22/tcp": [{}]}, {"22/tcp": [{"HostPort": "bad"}]}],
)
def test_malformed_direct_mapping_falls_back_to_proxy(ports):
    info = _instance_info()
    info["ports"] = ports

    endpoints = provisioning.ssh_endpoint_candidates(info)

    assert [(item.kind, item.host, item.port) for item in endpoints] == [
        ("proxy", "ssh7.vast.ai", 17022)
    ]


def test_readiness_tries_direct_before_proxy(monkeypatch, tmp_path):
    attempts: list[tuple[str, int, int]] = []

    def fake_wait(args, host, port, timeout_s):
        attempts.append((host, port, timeout_s))
        if host == "203.0.113.10":
            raise VastError("direct route blocked")

    monkeypatch.setattr(provisioning, "wait_for_ssh", fake_wait)
    args = Namespace(boot_timeout=60, known_hosts=tmp_path / "known_hosts")

    selected = provisioning.wait_for_ssh_with_recovery(
        args, object(), 123, _instance_info()
    )

    assert selected == ("ssh7.vast.ai", 17022)
    assert attempts == [
        ("203.0.113.10", 40122, provisioning.DIRECT_SSH_ATTEMPT_TIMEOUT_S),
        ("ssh7.vast.ai", 17022, provisioning.SSH_ATTEMPT_TIMEOUT_S),
    ]


def test_instance_creation_requests_ssh_direct():
    captured_body: dict = {}

    class Client:
        def create_instance(self, offer_id, body):
            captured_body.update(body)
            return {"success": True, "new_contract": 321}

    args = Namespace(image="worker:latest", disk_gb=150)
    instance_id, _, _ = provisioning.rent_instance(
        Client(),
        args,
        [{"id": 99, "gpu_name": "RTX 5090", "dph_total": 0.5}],
        "test-label",
        "echo onstart",
    )

    assert instance_id == 321
    assert captured_body["runtype"] == "ssh_direct"


def test_ssh_transport_explicitly_disables_compression(tmp_path):
    args = Namespace(ssh_key=tmp_path / "key", known_hosts=tmp_path / "known_hosts")

    command = ssh_base(args, "203.0.113.10", 40122)

    assert "Compression=no" in command


def test_monitoring_falls_back_without_reboot(monkeypatch):
    calls: list[tuple[str, int]] = []

    class Client:
        def show_instance(self, instance_id):
            return _instance_info()

    def fake_snapshot(args, host, port):
        calls.append((host, port))
        if host == "203.0.113.10":
            raise VastError("direct route dropped")
        return RemoteSnapshot(stage="complete", status="DONE:0")

    monkeypatch.setattr(job_monitor, "fetch_remote_snapshot", fake_snapshot)
    args = Namespace(
        job_timeout=10,
        ssh_reconnect_timeout=5,
        monitor_interval=0.01,
        upload_workers=16,
    )

    selected = job_monitor.wait_for_job(
        args,
        Client(),
        123,
        "203.0.113.10",
        40122,
        gpu_name="RTX 5090",
        hourly_price=0.5,
        expected_input_bytes=None,
        rental_started_at=0,
    )

    assert selected == ("ssh7.vast.ai", 17022)
    assert calls == [("203.0.113.10", 40122), ("ssh7.vast.ai", 17022)]


def test_offer_search_requires_and_displays_direct_ports(monkeypatch):
    captured_body: dict = {}

    def fake_request(self, method, path, *, body=None, **kwargs):
        captured_body.update(body)
        return {"offers": []}

    monkeypatch.setattr(VastClient, "request", fake_request)
    args = Namespace(
        min_reliability=0.98,
        min_cpu=4,
        min_ram_mb=16384,
        disk_gb=150,
        min_disk_bw=500,
        min_download_mbps=500,
        min_upload_mbps=500,
        max_hourly=0.8,
        boot_timeout=600,
        job_timeout=3600,
    )
    VastClient("test").search_offers("RTX 5090", args)
    assert captured_body["direct_port_count"] == {"gte": 1}

    class OfferClient:
        def search_offers(self, gpu, args):
            return [
                {
                    "id": 1,
                    "gpu_name": gpu,
                    "direct_port_count": 2,
                    "dph_total": 0.5,
                    "reliability": 0.99,
                    "inet_down": 1000,
                    "inet_up": 1000,
                    "disk_bw": 1000,
                }
            ]

    offer_args = Namespace(gpus=["L4"], expected_hours=0.5)
    old_width = console.width
    try:
        console.width = 180
        with console.capture() as capture:
            choose_offers(OfferClient(), offer_args, 1.0)
    finally:
        console.width = old_width
    rendered = capture.get()
    assert "Direct" in rendered
    assert "yes" in rendered
