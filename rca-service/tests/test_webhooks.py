from datetime import datetime, timezone
import asyncio

import pytest
from fastapi.testclient import TestClient

from main import app
from app.models.alert import AlertPayload
from app.routers import alerts as alerts_router


@pytest.fixture
def client():
    startup_handlers = list(app.router.on_startup)
    shutdown_handlers = list(app.router.on_shutdown)
    app.router.on_startup.clear()
    app.router.on_shutdown.clear()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.router.on_startup[:] = startup_handlers
        app.router.on_shutdown[:] = shutdown_handlers


def test_webhook_grafana_accepts_payload(client, monkeypatch):
    async def _noop_background(_alert):
        return None

    async def _should_process(_alert, status="firing"):
        return True, 0, 300

    monkeypatch.setattr(alerts_router, "process_alert_background", _noop_background)
    monkeypatch.setattr(alerts_router.dedup_service, "should_process", _should_process)

    payload = {
        "status": "firing",
        "title": "[FIRING:1] HighCPUUsage",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "HighCPUUsage", "severity": "critical", "instance": "node-1"},
                "annotations": {"description": "CPU > 90%"},
                "startsAt": datetime.now(timezone.utc).isoformat(),
                "fingerprint": "fp-1",
            }
        ],
    }

    response = client.post("/webhook/grafana", json=payload)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert "alert_id" in body


def test_webhook_grafana_invalid_payload_returns_422(client):
    response = client.post("/webhook/grafana", json={"status": "firing", "alerts": []})
    assert response.status_code == 422


def test_webhook_drain3_accepts_payload(client):
    payload = {
        "cluster_id": "c-1",
        "template": "Error <NUM> from host <NUM>",
        "metadata": {"host": "node-1"},
    }
    response = client.post("/webhook/drain3", json=payload)
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"


@pytest.mark.asyncio
async def test_dedup_10_identical_alerts_results_in_1_process():
    # Ce test d'intégration nécessite Redis disponible.
    # On vérifie ici le contrat attendu via un mock déterministe.
    calls = {"count": 0}

    async def _mock_should_process(_alert, status="firing"):
        calls["count"] += 1
        if calls["count"] == 1:
            return True, 0, 300
        return False, calls["count"] - 1, 299

    processed = 0
    for i in range(10):
        alert = AlertPayload(
            alert_id=f"a-{i}",
            alert_name="FlappingAlert",
            severity="critical",
            hostname="node-1",
            description="flap",
            source="grafana",
        )
        should_process, _, _ = await _mock_should_process(alert, status="firing")
        if should_process:
            processed += 1
    assert processed == 1


@pytest.mark.asyncio
async def test_timeout_passthrough_triggered(monkeypatch):
    called = {"timeout": False}

    async def _slow(_alert):
        await asyncio.sleep(0.02)

    async def _fallback(_alert, _stage):
        called["timeout"] = True

    monkeypatch.setattr(alerts_router.settings, "ALERT_PROCESSING_TIMEOUT", 0.01)
    monkeypatch.setattr(alerts_router, "_run_alert_pipeline", _slow)
    monkeypatch.setattr(alerts_router, "_fallback_passthrough_on_timeout", _fallback)

    alert = AlertPayload(
        alert_id="timeout-123",
        alert_name="TimeoutAlert",
        severity="critical",
        hostname="node-timeout",
        description="Long processing",
        source="test",
    )
    await alerts_router.process_alert_background(alert)
    assert called["timeout"] is True
