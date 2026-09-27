"""Revocación de identidades (AGENT.md 3.1): una clave revocada no firma nada más."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import fakeredis
import pytest
from fastapi import HTTPException

pytest.importorskip("cryptography", reason="requiere cryptography")

from common.identity import (  # noqa: E402
    generate_private_key,
    proposal_message,
    public_key_b64,
    revocation_message,
    sign,
)
from common.storage import VoxChainStore  # noqa: E402


class _Identidad:
    def __init__(self):
        self.key = generate_private_key()
        self.pubkey = public_key_b64(self.key)

    def firma(self, texto: str | bytes) -> str:
        return sign(self.key, texto if isinstance(texto, bytes) else texto.encode())

    def certificado(self) -> dict:
        return {"pubkey": self.pubkey, "signature": self.firma(revocation_message(self.pubkey))}


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def r():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def publicadas():
    return []


@pytest.fixture
def api(r, publicadas, monkeypatch):
    from fastapi.testclient import TestClient

    from voxchain_api.config import config
    from voxchain_api.main import app
    from voxchain_api.routers import identity as identity_router
    from voxchain_api.routers import laws as laws_router
    from voxchain_api.routers import workers as workers_router

    monkeypatch.setattr(config, "RESTRICT_PROPOSERS", False)

    class FakeReader:
        def __init__(self, client):
            self.store = VoxChainStore(client)

        def get_law(self, law_id):
            return self.store.get_law(law_id)

    class FakePublisher:
        def publish_law_proposal(self, **kw):
            publicadas.append(kw)
            return {"law_id": kw.get("law_id") or "L", "author_pubkey": kw["author_pubkey"],
                    "text_hash": "h", "status": "pending_queue", "action": kw["action"],
                    "category": kw["category"], "created_at": "t0"}

        def close(self):
            pass

    for router in (identity_router, laws_router, workers_router):
        app.dependency_overrides[router.get_redis_reader] = lambda: FakeReader(r)
    app.dependency_overrides[laws_router.get_rabbitmq_publisher] = FakePublisher
    yield TestClient(app)
    app.dependency_overrides.clear()


def _propuesta(autor: _Identidad) -> dict:
    text, action, category = "Artículo 1", "promulgacion", "general"
    law_id = f"ley-{uuid.uuid4().hex[:8]}"
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    created_at = _ahora()
    return {"author_pubkey": autor.pubkey, "text": text, "action": action,
            "category": category, "law_id": law_id, "text_hash": text_hash,
            "created_at": created_at,
            "signature": autor.firma(proposal_message(
                autor.pubkey, action, text_hash, law_id, created_at, category))}


# ── el endpoint ──────────────────────────────────────────────────────────────

def test_revocar_con_certificado_valido(api):
    ana = _Identidad()
    resp = api.post("/api/identity/revoke", json=ana.certificado())
    assert resp.status_code == 200, resp.text
    assert resp.json()["revoked"] is True
    estado = api.get(f"/api/identity/revocation/{ana.pubkey}").json()
    assert estado["revoked"] is True and estado["revoked_at"]


def test_nadie_revoca_a_otro(api):
    """El certificado lo firma la propia clave: con otra firma no se revoca."""
    ana, eva = _Identidad(), _Identidad()
    falso = {"pubkey": ana.pubkey, "signature": eva.firma(revocation_message(ana.pubkey))}
    assert api.post("/api/identity/revoke", json=falso).status_code == 401
    assert api.get(f"/api/identity/revocation/{ana.pubkey}").json()["revoked"] is False


def test_una_firma_de_otra_cosa_no_revoca(api):
    """Separación de dominios: firmar una propuesta no sirve como revocación."""
    ana = _Identidad()
    otra = _propuesta(ana)["signature"]
    assert api.post("/api/identity/revoke",
                    json={"pubkey": ana.pubkey, "signature": otra}).status_code == 401


def test_revocar_es_idempotente_y_conserva_el_momento(api):
    ana = _Identidad()
    cert = ana.certificado()
    primera = api.post("/api/identity/revoke", json=cert).json()["revoked_at"]
    segunda = api.post("/api/identity/revoke", json=cert).json()["revoked_at"]
    assert primera == segunda


def test_consultar_una_identidad_vigente(api):
    ana = _Identidad()
    assert api.get(f"/api/identity/revocation/{ana.pubkey}").json() == {
        "pubkey": ana.pubkey, "revoked": False, "revoked_at": None}


# ── una clave revocada no firma nada ─────────────────────────────────────────

def test_revocada_no_propone(api, publicadas):
    ana = _Identidad()
    assert api.post("/api/laws", json=_propuesta(ana)).status_code == 200
    api.post("/api/identity/revoke", json=ana.certificado())

    resp = api.post("/api/laws", json=_propuesta(ana))
    assert resp.status_code == 401
    assert "revocada" in resp.json()["detail"]
    assert len(publicadas) == 1


def test_revocada_no_registra_mineros(api, r, monkeypatch):
    monkeypatch.setattr("voxchain_api.routers.workers._verify_timestamp_freshness", lambda *a: None)
    ana = _Identidad()
    api.post("/api/identity/revoke", json=ana.certificado())
    ts = _ahora()
    resp = api.post("/api/workers/register", json={
        "worker_id": "de-ana", "pubkey": ana.pubkey, "timestamp": ts,
        "signature": ana.firma(f"de-ana|register|{ts}")})
    assert resp.status_code == 401
    assert not r.sismember("registered_workers", "de-ana")


def test_revocada_no_da_de_baja(api, r):
    ana = _Identidad()
    r.sadd("registered_workers", "de-ana")
    r.set("worker:owner:de-ana", ana.pubkey)
    api.post("/api/identity/revoke", json=ana.certificado())
    ts = _ahora()
    resp = api.delete("/api/workers/de-ana", headers={
        "X-Signature": ana.firma(f"de-ana|delete|{ts}"), "X-Timestamp": ts})
    assert resp.status_code == 401
    assert r.sismember("registered_workers", "de-ana")


def test_revocada_no_administra(r):
    """`require_signed_action` es por donde pasan equipos, modos y deliberación."""
    from voxchain_api.routers.workers import require_signed_action

    ana = _Identidad()
    VoxChainStore(r).revoke_identity(ana.pubkey, _ahora())
    ts = _ahora()
    with pytest.raises(HTTPException) as exc:
        require_signed_action(r, "w1", "switch-mode", ana.pubkey,
                              ana.firma(f"w1|switch-mode|{ts}"), ts)
    assert exc.value.status_code == 401 and "revocada" in exc.value.detail


def test_revocar_a_una_no_afecta_a_otra(api, publicadas):
    ana, eva = _Identidad(), _Identidad()
    api.post("/api/identity/revoke", json=ana.certificado())
    assert api.post("/api/laws", json=_propuesta(eva)).status_code == 200
