"""API de la deliberación (AGENT.md 3.12): ver la ley anunciada y responder.

Lo que se protege: sólo responde quien habla por el convocado —el fundador por
su equipo, el dueño por su standalone—, probándolo con su firma sobre la ley y
la decisión, y sólo mientras dure la pausa.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone

import fakeredis
import pytest

from common.storage import VoxChainStore


class Identidad:
    def __init__(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        self.key = ec.generate_private_key(ec.SECP256R1())
        self.pubkey = base64.b64encode(self.key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo)).decode()

    def firma(self, mensaje: str) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils as asym

        der = self.key.sign(mensaje.encode(), ec.ECDSA(hashes.SHA256()))
        r, s = asym.decode_dss_signature(der)
        return base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()

    def headers(self, voter_id: str, law_id: str, decision: str) -> dict:
        ts = datetime.now(timezone.utc).isoformat()
        return {"X-Owner-Id": self.pubkey, "X-Timestamp": ts,
                "X-Signature": self.firma(f"{voter_id}|deliberate:{law_id}:{decision}|{ts}")}


FUNDADORA = Identidad()
DUENO = Identidad()
INTRUSO = Identidad()


@pytest.fixture
def r():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def api(r):
    from fastapi.testclient import TestClient

    from voxchain_api.main import app
    from voxchain_api.routers import workers as workers_router

    class FakeReader:
        def __init__(self, client):
            self.store = type("S", (), {"r": client})()

    app.dependency_overrides[workers_router.get_redis_reader] = lambda: FakeReader(r)
    yield TestClient(app)
    app.dependency_overrides.clear()


def anunciar(r, *, decide_en=60.0):
    """Lo que deja el NCT al anunciar L1: un equipo y un standalone convocados."""
    r.hset("team:t1", mapping={"team_id": "t1", "name": "Los Pibes",
                               "owner": FUNDADORA.pubkey,
                               "coordinator_worker_id": "coord"})
    r.set("worker:owner:solo", DUENO.pubkey)
    VoxChainStore(r).save_deliberation({
        "law_id": "L1", "action": "promulgacion", "category": "economia",
        "started_at": "t0", "decide_until": "t1",
        "decide_until_epoch": time.time() + decide_en,
        "n_zeros_required": 6, "window_seconds": 40, "nonce_space": 1000,
        "convocados": [
            {"voter_id": "coord", "kind": "equipo", "name": "Los Pibes",
             "owner": FUNDADORA.pubkey, "hashrate": 5000.0, "team_id": "t1"},
            {"voter_id": "solo", "kind": "standalone", "name": "solo",
             "owner": "", "hashrate": 900.0},
        ],
    })


def votar(api, quien, voter_id, decision, law_id="L1"):
    return api.post(f"/api/deliberation/{law_id}/decision",
                    json={"voter_id": voter_id, "decision": decision},
                    headers=quien.headers(voter_id, law_id, decision))


class TestVer:
    def test_sin_deliberacion_devuelve_null(self, api):
        assert api.get("/api/deliberation").json() is None

    def test_muestra_la_ley_los_convocados_y_quien_es_el_mas_grande(self, api, r):
        anunciar(r)
        VoxChainStore(r).set_deliberation_decision("L1", "solo", "reject")

        d = api.get("/api/deliberation").json()
        assert d["law_id"] == "L1" and d["category"] == "economia"
        assert 0 < d["seconds_left"] <= 60
        votos = {v["voter_id"]: v for v in d["voters"]}
        assert votos["coord"]["biggest"] and not votos["solo"]["biggest"]
        assert votos["coord"]["decision"] is None
        assert votos["solo"]["decision"] == "reject"
        assert votos["solo"]["owner"] == DUENO.pubkey
        assert "partial_hash_base" not in json.dumps(d)


class TestResponder:
    def test_la_fundadora_responde_por_su_equipo(self, api, r):
        anunciar(r)
        resp = votar(api, FUNDADORA, "coord", "accept")
        assert resp.status_code == 200, resp.text
        assert VoxChainStore(r).deliberation_decisions("L1") == {"coord": "accept"}

    def test_el_dueno_responde_por_su_standalone_y_puede_cambiar(self, api, r):
        anunciar(r)
        assert votar(api, DUENO, "solo", "accept").status_code == 200
        assert votar(api, DUENO, "solo", "reject").status_code == 200
        assert VoxChainStore(r).deliberation_decisions("L1") == {"solo": "reject"}

    @pytest.mark.parametrize("voter_id", ["coord", "solo"])
    def test_nadie_mas_responde_por_ellos(self, api, r, voter_id):
        anunciar(r)
        resp = votar(api, INTRUSO, voter_id, "reject")
        assert resp.status_code == 403
        assert VoxChainStore(r).deliberation_decisions("L1") == {}

    def test_la_firma_de_un_si_no_sirve_para_un_no(self, api, r):
        anunciar(r)
        headers = FUNDADORA.headers("coord", "L1", "accept")
        resp = api.post("/api/deliberation/L1/decision",
                        json={"voter_id": "coord", "decision": "reject"},
                        headers=headers)
        assert resp.status_code == 401

    def test_un_no_convocado_no_vota(self, api, r):
        anunciar(r)
        assert votar(api, DUENO, "otro", "accept").status_code == 403

    def test_otra_ley_o_pausa_vencida_es_409(self, api, r):
        anunciar(r)
        assert votar(api, FUNDADORA, "coord", "accept", law_id="L2").status_code == 409
        anunciar(r, decide_en=-1)
        assert votar(api, FUNDADORA, "coord", "accept").status_code == 409

    def test_decision_invalida_es_400(self, api, r):
        anunciar(r)
        assert votar(api, FUNDADORA, "coord", "quizas").status_code == 400


class TestResultado:
    def test_devuelve_como_termino(self, api, r):
        VoxChainStore(r).save_deliberation_result("L1", {
            "outcome": "discard", "accepted": [], "rejected": ["coord"],
            "silent": ["solo"], "decided_at": "t"})
        d = api.get("/api/deliberation/result/L1").json()
        assert d["outcome"] == "discard" and d["rejected"] == ["coord"]

    def test_sin_deliberacion_es_404(self, api):
        assert api.get("/api/deliberation/result/L9").status_code == 404


class TestRespuestaPorDefectoDelEquipo:
    def _headers(self, quien, decision):
        ts = datetime.now(timezone.utc).isoformat()
        return {"X-Owner-Id": quien.pubkey, "X-Timestamp": ts,
                "X-Signature": quien.firma(f"t1|set-default-decision|{ts}")}

    def test_la_fundadora_la_fija_y_se_ve_en_la_deliberacion(self, api, r):
        anunciar(r)
        resp = api.put("/api/teams/t1/default-decision",
                       json={"default_decision": "accept"},
                       headers=self._headers(FUNDADORA, "accept"))
        assert resp.status_code == 200, resp.text
        assert resp.json()["default_decision"] == "accept"
        assert r.hget("team:t1", "default_decision") == "accept"

    def test_nadie_mas_la_fija(self, api, r):
        anunciar(r)
        resp = api.put("/api/teams/t1/default-decision",
                       json={"default_decision": "reject"},
                       headers=self._headers(INTRUSO, "reject"))
        assert resp.status_code == 403

    def test_valor_invalido_es_400(self, api, r):
        anunciar(r)
        resp = api.put("/api/teams/t1/default-decision",
                       json={"default_decision": "quizas"},
                       headers=self._headers(FUNDADORA, "quizas"))
        assert resp.status_code == 400

    def test_la_deliberacion_muestra_la_por_defecto(self, api, r):
        anunciar(r)
        estado = VoxChainStore(r).get_deliberation()
        estado["convocados"][1]["default_decision"] = "reject"
        VoxChainStore(r).save_deliberation(estado)
        votos = {v["voter_id"]: v for v in api.get("/api/deliberation").json()["voters"]}
        assert votos["solo"]["default_decision"] == "reject"
        assert votos["coord"]["default_decision"] == ""
