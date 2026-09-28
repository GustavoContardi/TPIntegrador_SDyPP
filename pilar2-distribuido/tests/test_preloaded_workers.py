"""Mineros precargados del docker-compose: sí en local, no en Kubernetes.

worker-1, worker-2 y pool-coordinator-1 son servicios fijos del compose. En el
despliegue no existen, pero el API los listaba igual como "apagados" y los
autorizaba por cabecera con dueño `default`. PRELOADED_WORKER_IDS vacía (lo que
pone voxchain-config.yaml) los saca de las dos cosas.
"""

from __future__ import annotations

import fakeredis
import pytest
from fastapi import HTTPException

from voxchain_api.config import _as_ids
from voxchain_api.routers import workers as workers_router

DEFAULT = ("worker-1", "worker-2", "pool-coordinator-1")


@pytest.mark.parametrize("raw, esperado", [
    (None, DEFAULT),                      # sin la variable: los del compose
    ("", ()),                             # vacía: ninguno (Kubernetes)
    (" a , b ,", ("a", "b")),
])
def test_parseo_de_la_variable(raw, esperado):
    assert _as_ids(raw, DEFAULT) == esperado


@pytest.fixture
def r():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def sin_precargados(monkeypatch):
    monkeypatch.setattr(workers_router, "ALL_REGISTERED_WORKER_IDS", [])
    monkeypatch.setattr(workers_router, "OWNER_WORKERS_MAPPING", {})


@pytest.fixture
def api(r):
    from fastapi.testclient import TestClient

    from voxchain_api.main import app

    class FakeReader:
        def __init__(self, client):
            self.store = type("S", (), {"r": client})()

    app.dependency_overrides[workers_router.get_redis_reader] = lambda: FakeReader(r)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_sistema_recien_creado_sin_mineros(api, sin_precargados):
    assert api.get("/api/workers/status").json() == []


def test_en_local_los_precargados_figuran_apagados(api):
    ids = {w["worker_id"]: w["running"] for w in api.get("/api/workers/status").json()}
    assert ids == {w: False for w in DEFAULT}


def test_sin_precargados_la_cabecera_default_no_autoriza(r, sin_precargados):
    # Antes, `X-Owner-Id: default` alcanzaba para administrar worker-1 sin firma.
    with pytest.raises(HTTPException):
        workers_router.authorize_worker_action(
            r, "worker-1", workers_router.ADMIN_SWITCH_MODE, "default", None, None)
