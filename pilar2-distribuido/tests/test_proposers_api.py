"""API de propuestas: sólo proponen el fundador de un equipo o un standalone (3.2).

El API responde 403 con el motivo —el NCT también lo rechaza, pero en silencio—
y expone la consulta para que el formulario lo avise antes de que se escriba la
ley.
"""

from __future__ import annotations

from urllib.parse import quote

import fakeredis
import pytest

from common.storage import VoxChainStore

# Una pubkey base64 real trae '/' y '+': el endpoint de consulta tiene que
# aguantarlos en el path.
PK_SOLO = "MFkw+solo/abc=="
PK_NADIE = "MFkw+nadie/xyz=="


@pytest.fixture
def r():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def publicadas():
    return []


@pytest.fixture
def api(r, publicadas):
    from fastapi.testclient import TestClient

    from voxchain_api.main import app
    from voxchain_api.routers import laws as laws_router

    class FakeReader:
        def __init__(self, client):
            self.store = VoxChainStore(client)

        def get_law(self, law_id):
            return self.store.get_law(law_id)

    class FakePublisher:
        def publish_law_proposal(self, **kw):
            publicadas.append(kw)
            return {"law_id": kw.get("law_id") or "L-nueva",
                    "author_pubkey": kw["author_pubkey"], "text_hash": "h",
                    "status": "pending_queue", "action": kw["action"],
                    "category": kw["category"], "created_at": "t0"}

        def close(self):
            pass

    app.dependency_overrides[laws_router.get_redis_reader] = lambda: FakeReader(r)
    app.dependency_overrides[laws_router.get_rabbitmq_publisher] = FakePublisher
    yield TestClient(app)
    app.dependency_overrides.clear()


def _propuesta(author):
    return {"author_pubkey": author, "text": "Artículo 1", "action": "promulgacion",
            "category": "general"}


def test_sin_minero_recibe_403_con_el_motivo(api, publicadas):
    resp = api.post("/api/laws", json=_propuesta(PK_NADIE))
    assert resp.status_code == 403
    assert "standalone" in resp.json()["detail"]
    assert publicadas == []


def test_standalone_propone(api, r, publicadas):
    r.sadd("registered_workers", "w1")
    r.set("worker:owner:w1", PK_SOLO)
    resp = api.post("/api/laws", json=_propuesta(PK_SOLO))
    assert resp.status_code == 200
    assert len(publicadas) == 1


def test_consulta_con_pubkey_con_barras(api, r):
    r.sadd("registered_workers", "w1")
    r.set("worker:owner:w1", PK_SOLO)
    ok = api.get(f"/api/laws/proposer/{quote(PK_SOLO, safe='')}").json()
    assert ok == {"allowed": True, "role": "standalone", "reason": ""}
    no = api.get(f"/api/laws/proposer/{quote(PK_NADIE, safe='')}").json()
    assert no["allowed"] is False and no["reason"]


def test_restriccion_apagada(api, publicadas, monkeypatch):
    from voxchain_api.config import config

    monkeypatch.setattr(config, "RESTRICT_PROPOSERS", False)
    assert api.post("/api/laws", json=_propuesta(PK_NADIE)).status_code == 200
    assert api.get(f"/api/laws/proposer/{quote(PK_NADIE, safe='')}").json()["allowed"]
