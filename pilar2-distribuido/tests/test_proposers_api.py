"""API de propuestas: sólo proponen el fundador de un equipo o un standalone (3.2).

El API responde 403 con el motivo —el NCT también lo rechaza, pero en silencio—
y expone la consulta para que el formulario lo avise antes de que se escriba la
ley.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import fakeredis
import pytest

from common.identity import generate_private_key, proposal_message, public_key_b64, sign
from common.storage import VoxChainStore

pytest.importorskip("cryptography", reason="requiere cryptography")

# Una pubkey base64 real trae '/' y '+': el endpoint de consulta tiene que
# aguantarlos en el path. (La consulta no firma nada, así que alcanza con un
# string; las propuestas sí van firmadas, con identidades reales.)
PK_SOLO = "MFkw+solo/abc=="
PK_NADIE = "MFkw+nadie/xyz=="


class _Identidad:
    """Par P-256 de un ciudadano: el API rechaza toda propuesta sin firma (A-01)."""

    def __init__(self):
        self.key = generate_private_key()
        self.pubkey = public_key_b64(self.key)


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


def _propuesta(autor: _Identidad) -> dict:
    """Propuesta firmada como la arma el frontend."""
    text, action, category = "Artículo 1", "promulgacion", "general"
    law_id = f"ley-{uuid.uuid4().hex[:8]}"
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    created_at = datetime.now(timezone.utc).isoformat()
    return {"author_pubkey": autor.pubkey, "text": text, "action": action,
            "category": category, "law_id": law_id, "text_hash": text_hash,
            "created_at": created_at,
            "signature": sign(autor.key, proposal_message(
                autor.pubkey, action, text_hash, law_id, created_at, category))}


def test_sin_minero_recibe_403_con_el_motivo(api, publicadas):
    resp = api.post("/api/laws", json=_propuesta(_Identidad()))
    assert resp.status_code == 403
    assert "Registrá un minero" in resp.json()["detail"]
    assert publicadas == []


def test_standalone_propone(api, r, publicadas):
    solo = _Identidad()
    r.sadd("registered_workers", "w1")
    r.set("worker:owner:w1", solo.pubkey)
    resp = api.post("/api/laws", json=_propuesta(solo))
    assert resp.status_code == 200
    assert len(publicadas) == 1


def test_sin_firma_401_antes_que_la_restriccion(api, r, publicadas):
    """Por default el API exige firma: sin ella ni siquiera se evalúa quién propone.

    Es el caso que el modo migración dejaba pasar: cualquiera podía proponer
    con la pubkey de otro con sólo omitir la firma.
    """
    solo = _Identidad()
    r.sadd("registered_workers", "w1")
    r.set("worker:owner:w1", solo.pubkey)
    sin_firma = {"author_pubkey": solo.pubkey, "text": "Artículo 1",
                 "action": "promulgacion", "category": "general"}
    resp = api.post("/api/laws", json=sin_firma)
    assert resp.status_code == 401
    assert publicadas == []


def test_require_signatures_default_true(monkeypatch):
    """Sin la variable de entorno, el API y el NCT arrancan exigiendo firma."""
    import importlib

    import common.config as common_config
    from voxchain_api.config import Config

    monkeypatch.delenv("REQUIRE_SIGNATURES", raising=False)
    assert Config.from_env().REQUIRE_SIGNATURES is True
    assert importlib.reload(common_config).REQUIRE_SIGNATURES is True
    monkeypatch.setenv("REQUIRE_SIGNATURES", "false")
    assert Config.from_env().REQUIRE_SIGNATURES is False
    assert importlib.reload(common_config).REQUIRE_SIGNATURES is False
    monkeypatch.delenv("REQUIRE_SIGNATURES")
    importlib.reload(common_config)


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
    assert api.post("/api/laws", json=_propuesta(_Identidad())).status_code == 200
    assert api.get(f"/api/laws/proposer/{quote(PK_NADIE, safe='')}").json()["allowed"]
