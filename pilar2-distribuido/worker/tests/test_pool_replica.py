"""Réplica en espera del coordinador de un equipo.

El coordinador de un equipo lo designa una persona, así que ningún miembro puede
tomar su lugar: si su pod cae, el equipo no mina hasta que Kubernetes lo
reponga. Por eso corre en dos pods del mismo minero —mismo ``pool_id``— y el
lease ``pool:leader:<pool_id>`` decide cuál manda. El otro queda en espera:
fragmenta los desafíos como el líder pero no mina ni reparte, y si el líder cae
toma el lease y sigue la ventana en curso.

Lo que protegen estos tests:

- que las dos réplicas no se crean líderes a la vez (el lease guarda un id de
  instancia, no el ``pool_id``, que comparten);
- que la réplica en espera no trabaje en paralelo al líder;
- que al tomar el mando retome la ventana vigente y tire las que ya cerraron;
- que un coordinador que no sabe de nadie más —no ganó todavía, o no puede leer
  Redis— siga trabajando.
"""

from __future__ import annotations

import fakeredis
import pytest

from worker_pkg.pool_coordinator.coordinator import PoolCoordinator
from worker_pkg.pool_coordinator.election import (
    LEASE_RANK_DESIGNATED,
    encode_lease,
    lease_holder,
    run_pool_election,
)

POOL = "equipo-rojo"
LEASE = f"pool:leader:{POOL}"


class Reloj:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def avanzar(self, segundos: float) -> None:
        self.t += segundos


class BusMudo:
    """Lo mínimo que usa el coordinador del bus: suscribirse y publicar."""

    def __init__(self):
        self.publicado = []

    def on_challenge(self, handler):
        pass

    def on_window_closed(self, handler):
        pass

    def publish_nonce_response(self, msg):
        self.publicado.append(msg)

    def publish_keepalive(self, msg):
        pass


@pytest.fixture
def r():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def reloj():
    return Reloj()


def _replica(r, reloj, pod: str) -> PoolCoordinator:
    c = PoolCoordinator(BusMudo(), pool_id=POOL, redis=r,
                        mine=lambda *a: (None, None), election_n_zeros=1,
                        instance_id=f"{POOL}@{pod}", clock=reloj)
    c.nonce_space, c.fragment_size = 100, 25  # 4 fragmentos por ventana
    c._running = True  # sin arrancar el hilo de auto-minado
    return c


def _tick(c: PoolCoordinator, reloj: Reloj) -> None:
    """Un ciclo de lease completo: dispara la elección si hace falta y la recoge."""
    reloj.avanzar(3.1)
    c.tick()
    if c._election_thread is not None:
        c._election_thread.join(timeout=5)
    c.tick()


def _desafio(wid: str) -> dict:
    return {"voting_window_id": wid, "law_id": "ley-1", "action": "promulgacion",
            "category": "general", "partial_hash_base": f"base-{wid}",
            "n_zeros_required": 2, "nonce_space": 100}


def test_dos_replicas_del_mismo_minero_no_mandan_las_dos(r, reloj):
    a, b = _replica(r, reloj, "pod-a"), _replica(r, reloj, "pod-b")
    _tick(a, reloj)
    _tick(b, reloj)

    assert a.is_leader and not a.standby
    assert not b.is_leader and b.standby
    # El lease nombra a la instancia, no al pool: con el pool_id las dos
    # réplicas leían "su" nombre y se creían líderes.
    assert lease_holder(r.get(LEASE)) == f"{POOL}@pod-a"


def test_la_replica_en_espera_fragmenta_pero_no_reparte_ni_mina(r, reloj):
    a, b = _replica(r, reloj, "pod-a"), _replica(r, reloj, "pod-b")
    _tick(a, reloj)
    _tick(b, reloj)
    for c in (a, b):
        c.handle_challenge(_desafio("W1"))

    # Tiene la ventana armada, lista para seguirla si le toca...
    assert len(b._pending_fragments) == 4
    # ...pero no le da trabajo a nadie ni se lo toma para sí.
    minero = b.register_miner()
    assert b.get_next_task(minero) is None
    assert b._get_auto_miner_fragment() is None
    # El líder, en cambio, reparte normalmente.
    assert a.get_next_task(a.register_miner())["voting_window_id"] == "W1"


def test_si_el_lider_cae_la_replica_toma_el_mando_y_sigue_la_ventana(r, reloj):
    a, b = _replica(r, reloj, "pod-a"), _replica(r, reloj, "pod-b")
    _tick(a, reloj)
    _tick(b, reloj)
    for c in (a, b):
        c.handle_challenge(_desafio("W1"))
    r.set("active_window", "W1")

    a.stop()  # suelta el lease al apagarse, en vez de dejarlo vencer
    # La elección es por épocas de 30 s y a ganó la actual.
    reloj.avanzar(31)
    _tick(b, reloj)

    assert b.is_leader and not b.standby
    assert lease_holder(r.get(LEASE)) == f"{POOL}@pod-b"
    # La ventana en curso no se pierde: la réplica ya la tenía fragmentada.
    tarea = b.get_next_task(b.register_miner())
    assert tarea["voting_window_id"] == "W1"
    assert len(b._pending_fragments) == 3


def test_al_asumir_tira_las_ventanas_que_ya_cerraron(r, reloj):
    # En espera no se ve el sellado: el nonce ganador lo entregó el líder.
    a, b = _replica(r, reloj, "pod-a"), _replica(r, reloj, "pod-b")
    _tick(a, reloj)
    _tick(b, reloj)
    b.handle_challenge(_desafio("W1"))
    # W1 ya se selló: el NCT borró `active_window` y no abrió otra todavía.
    r.delete("active_window")

    a.stop()
    reloj.avanzar(31)
    _tick(b, reloj)

    assert b.is_leader
    assert len(b._pending_fragments) == 0


def test_un_reinicio_del_mismo_pod_reconoce_su_propio_lease(r, reloj):
    # El contenedor se reinicia dentro del pod: mismo hostname, mismo id de
    # instancia. El lease que dejó el proceso anterior es suyo.
    r.set(LEASE, encode_lease(f"{POOL}@pod-a", LEASE_RANK_DESIGNATED), ex=10)
    nuevo = _replica(r, reloj, "pod-a")
    reloj.avanzar(31)  # otra época de elección
    _tick(nuevo, reloj)

    assert nuevo.is_leader and not nuevo.standby


def test_sin_poder_leer_el_lease_sigue_trabajando(r, reloj):
    c = _replica(r, reloj, "pod-a")

    def explota(*_a, **_kw):
        raise ConnectionError("Redis caído")

    c.redis.get = explota
    c.standby = True
    c._maybe_start_election()
    # No sabe de nadie más: frenarlo dejaría al equipo sin coordinador por una
    # falla de observación. Dos activos a la vez sólo duplican trabajo.
    assert c.standby is False


def test_el_lider_que_pierde_el_lease_pasa_a_espera(r, reloj):
    a = _replica(r, reloj, "pod-a")
    _tick(a, reloj)
    assert a.is_leader
    # Otra réplica tomó el lease (p. ej. tras una partición de red).
    r.set(LEASE, encode_lease(f"{POOL}@pod-b", LEASE_RANK_DESIGNATED), ex=10)
    _tick(a, reloj)

    assert not a.is_leader and a.standby


def test_la_eleccion_escribe_la_instancia_en_el_lease():
    r = fakeredis.FakeRedis(decode_responses=True)
    assert run_pool_election(r, POOL, n_zeros=1, instance_id=f"{POOL}@pod-x")
    assert lease_holder(r.get(LEASE)) == f"{POOL}@pod-x"


def test_sin_instancia_el_lease_sigue_siendo_el_pool():
    # Un solo proceso por pool (pool-auto, el compose local): nada cambia.
    r = fakeredis.FakeRedis(decode_responses=True)
    assert run_pool_election(r, POOL, n_zeros=1)
    assert lease_holder(r.get(LEASE)) == POOL
