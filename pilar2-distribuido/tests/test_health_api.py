"""Endpoint público de estado: una clave por servicio (checklist §2).

Cubre las claves que se agregaron en septiembre (rabbitmq, frontend, clock) y
que las probes de Kubernetes van a /api/health/live, que no toca dependencias.
"""

from __future__ import annotations

import fakeredis
import pytest

from common.storage import VoxChainStore


class _Reader:
    def __init__(self, client):
        self.store = VoxChainStore(client)

    def ping(self):
        return self.store.ping()


@pytest.fixture
def health(monkeypatch):
    from voxchain_api.routers import health as h

    # Cada test arranca sin el resultado cacheado del anterior.
    h._rabbitmq_cache.update(at=0.0, status="unknown")
    monkeypatch.setattr(h.config, "NCT_HEALTH_URL", "http://nct.invalid/health")
    monkeypatch.setattr(h.config, "FRONTEND_HEALTH_URL", "")
    return h


@pytest.fixture
def api(health):
    from fastapi.testclient import TestClient

    from voxchain_api.main import app

    r = fakeredis.FakeRedis(decode_responses=True)
    app.dependency_overrides[health.get_redis_reader] = lambda: _Reader(r)
    yield TestClient(app), r
    app.dependency_overrides.clear()


def _broker(monkeypatch, health, vivo: bool, llamadas: list):
    class _Fake:
        def ping(self, timeout=2.0):
            llamadas.append(1)
            return vivo

    monkeypatch.setattr(health, "build_rabbitmq", lambda url: _Fake())


def test_reporta_rabbitmq_y_reloj(api, health, monkeypatch):
    client, _ = api
    _broker(monkeypatch, health, True, [])

    body = client.get("/api/health").json()

    assert body["rabbitmq"] == "ok"
    assert body["redis"] == "ok"
    # fakeredis responde TIME con el reloj local: el desfase es ~0.
    assert body["clock"] == "ok"
    assert abs(body["clock_skew_ms"]) < 100
    # Sin URL configurada no se inventa un estado.
    assert body["frontend"] == "unknown"


def test_broker_caido_es_error(api, health, monkeypatch):
    client, _ = api
    _broker(monkeypatch, health, False, [])

    assert client.get("/api/health").json()["rabbitmq"] == "error"


def test_rabbitmq_se_cachea(api, health, monkeypatch):
    """El endpoint es público: no puede abrir una conexión AMQP por request."""
    client, _ = api
    llamadas: list = []
    _broker(monkeypatch, health, True, llamadas)

    for _ in range(5):
        client.get("/api/health")

    assert len(llamadas) == 1


def test_desfase_de_reloj_se_reporta(api, health, monkeypatch):
    client, r = api
    _broker(monkeypatch, health, True, [])
    real = r.time
    # Redis va 2 s adelantado respecto de la API.
    monkeypatch.setattr(r, "time", lambda: (real()[0] + 2, real()[1]))

    body = client.get("/api/health").json()

    assert body["clock"] == "skew"
    assert body["clock_skew_ms"] > 1500


def test_live_no_toca_dependencias(api, health, monkeypatch):
    client, _ = api
    llamadas: list = []
    _broker(monkeypatch, health, False, llamadas)

    resp = client.get("/api/health/live")

    assert resp.status_code == 200
    assert resp.json() == {"api": "ok"}
    assert llamadas == []
