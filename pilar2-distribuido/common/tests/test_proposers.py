"""Quién puede proponer una ley (AGENT.md 3.2): fundador de equipo o standalone."""

import json

from common.blockchain.proposers import (
    STANDING_STANDALONE,
    STANDING_TEAM_OWNER,
    assess_proposer,
)

A = "pk-ana"
B = "pk-beto"


# ---- la regla, sobre datos ya leídos ----------------------------------------

def test_sin_minero_no_propone():
    st = assess_proposer(founded_team=False, worker_id=None,
                         worker_team=None, worker_mode="")
    assert not st.allowed
    assert "Registrá un minero" in st.reason


def test_fundador_propone_aunque_su_minero_este_en_el_equipo():
    """Su minero es el coordinador: mirando sólo el minero parecería un miembro."""
    st = assess_proposer(founded_team=True, worker_id="coord",
                         worker_team="Los Pibes", worker_mode="pool-coordinator")
    assert st.role == STANDING_TEAM_OWNER


def test_miembro_de_equipo_no_propone():
    st = assess_proposer(founded_team=False, worker_id="w1",
                         worker_team="Los Pibes", worker_mode="pool-worker")
    assert not st.allowed
    assert "Los Pibes" in st.reason


def test_standalone_propone_aunque_este_apagado():
    st = assess_proposer(founded_team=False, worker_id="w1",
                         worker_team=None, worker_mode="")
    assert st.role == STANDING_STANDALONE


def test_pool_worker_sin_equipo_no_propone():
    st = assess_proposer(founded_team=False, worker_id="w1",
                         worker_team=None, worker_mode="pool-worker")
    assert not st.allowed


def test_coordinador_sin_registro_de_equipo_propone_por_su_pool():
    st = assess_proposer(founded_team=False, worker_id="w1",
                         worker_team=None, worker_mode="pool-coordinator")
    assert st.role == STANDING_TEAM_OWNER


# ---- lectura desde Redis -----------------------------------------------------

def _registrar(r, worker_id, owner):
    r.sadd("registered_workers", worker_id)
    r.set(f"worker:owner:{worker_id}", owner)


def _equipo(r, team_id, owner, coordinador, miembros=()):
    r.hset(f"team:{team_id}", mapping={"team_id": team_id, "name": team_id.upper(),
                                       "owner": owner,
                                       "coordinator_worker_id": coordinador})
    r.sadd("teams", team_id)
    r.set(f"team:owner:{owner}", team_id)
    r.set(f"worker:team:{coordinador}", team_id)
    for m in miembros:
        r.sadd(f"team:members:{team_id}", m)
        r.set(f"worker:team:{m}", team_id)


def test_store_identidad_desconocida(store):
    assert not store.proposer_standing(A).allowed


def test_store_standalone_registrado(store):
    _registrar(store.r, "w-ana", A)
    assert store.proposer_standing(A).role == STANDING_STANDALONE


def test_store_fundador_y_miembro(store):
    _registrar(store.r, "coord", A)
    _registrar(store.r, "w-beto", B)
    _equipo(store.r, "t1", A, "coord", miembros=["w-beto"])
    assert store.proposer_standing(A).role == STANDING_TEAM_OWNER
    beto = store.proposer_standing(B)
    assert not beto.allowed
    assert "T1" in beto.reason  # nombra al equipo por su nombre, no por el id


def test_store_miembro_que_sale_vuelve_a_proponer(store):
    _registrar(store.r, "coord", A)
    _registrar(store.r, "w-beto", B)
    _equipo(store.r, "t1", A, "coord", miembros=["w-beto"])
    store.r.srem("team:members:t1", "w-beto")
    store.r.delete("worker:team:w-beto")
    assert store.proposer_standing(B).role == STANDING_STANDALONE


def test_store_indice_colgado_no_cuenta(store):
    """Si el hash del equipo se fue, los índices inversos no dan ni quitan lugar."""
    _registrar(store.r, "w-beto", B)
    store.r.set("team:owner:" + A, "fantasma")
    store.r.set("worker:team:w-beto", "fantasma")
    assert not store.proposer_standing(A).allowed
    assert store.proposer_standing(B).role == STANDING_STANDALONE


def test_store_usa_el_modo_que_reporta_el_minero(store):
    _registrar(store.r, "w-ana", A)
    store.r.set("worker:status:w-ana", json.dumps({"mode": "pool-worker"}))
    assert not store.proposer_standing(A).allowed

