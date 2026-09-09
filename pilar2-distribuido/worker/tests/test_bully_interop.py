"""Interoperación entre las dos elecciones de coordinador de pool.

VoxChain tiene dos formas de elegir el coordinador de un pool, y son para
topologías distintas (ver `docs/workers.md` §5):

- `pool_coordinator/election.py` — arbitra por **Redis**, la usa el modo
  `pool-coordinator` (el coordinador designado de un equipo).
- `bully.py` — arbitra por **RabbitMQ**, la usa el modo `pool-auto` (N workers
  intercambiables que se auto-organizan, sin depender de Redis).

Elegir distinto está bien; lo que no puede pasar es que un pool mixto termine
con **dos coordinadores activos sin que ninguno lo detecte**. Estos tests fijan
el punto de encuentro: cuando hay Redis, ambas ramas se disputan el mismo lease
`pool:leader:<pool_id>`, y el que no lo consigue se retira.
"""

from __future__ import annotations

import time
from unittest import mock

import pytest

from worker_pkg.bully import PoolBully
from worker_pkg.pool_coordinator.election import lease_holder


class FakeMessaging:
    """Bus de `pool.election` en memoria; registra lo publicado."""

    def __init__(self):
        self.published: list[tuple[str, dict]] = []
        self.handler = None

    def on_pool_election(self, pool_id, handler):
        self.handler = handler

    def publish_pool_election(self, pool_id, msg):
        self.published.append((pool_id, msg))

    # El PoolCoordinator interno también se cablea contra este bus.
    def on_challenge(self, handler):
        pass

    def publish_keepalive(self, msg):
        self.published.append(("keepalive", msg))

    def publish_nonce_response(self, msg):
        self.published.append(("nonce", msg))


class FakeRedis:
    def __init__(self, data=None):
        self._data = dict(data or {})

    def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self._data:
            return None
        self._data[key] = value
        return True

    def get(self, key):
        return self._data.get(key)

    def delete(self, key):
        return 1 if self._data.pop(key, None) is not None else 0

    def setex(self, key, ttl, value):
        self._data[key] = value
        return True

    def pipeline(self):
        return _Pipe(self)


class _Pipe:
    def __init__(self, r):
        self._r = r
        self._cmds = []

    def get(self, key):
        self._cmds.append(("get", key)); return self

    def pttl(self, key):
        self._cmds.append(("pttl", key)); return self

    def execute(self):
        out = []
        for cmd, key in self._cmds:
            out.append(self._r._data.get(key) if cmd == "get"
                       else (5000 if key in self._r._data else -2))
        return out


@pytest.fixture
def sin_http():
    """El coordinador levanta un HTTP real; en tests no queremos ocupar el puerto."""
    with mock.patch("worker_pkg.bully.start_pool_http_server") as srv:
        srv.return_value = mock.MagicMock()
        yield srv


def _bully(redis, pool_id="mi-pool", worker_id="nodo-1"):
    b = PoolBully(worker_id, pool_id, FakeMessaging(), address="http://nodo-1:9001",
                  redis=redis)
    return b


class TestElLeaseSeComparte:
    def test_el_ganador_del_bully_toma_el_lease_del_pool(self, sin_http):
        r = FakeRedis()
        b = _bully(r)
        b._transition_to(PoolBully.COORDINATOR)
        try:
            b._coordinator._last_lease_renew = 0
            b._coordinator.tick()
            # Visible para la otra rama y para las métricas, que antes no lo veían.
            assert lease_holder(r.get("pool:leader:mi-pool")) == "nodo-1"
        finally:
            b.stop()

    def test_el_lease_va_por_pool_no_por_worker(self, sin_http):
        """Dos nodos del mismo pool compiten; dos pools distintos no."""
        r = FakeRedis()
        b = _bully(r, pool_id="mi-pool", worker_id="nodo-1")
        b._transition_to(PoolBully.COORDINATOR)
        try:
            b._coordinator._last_lease_renew = 0
            b._coordinator.tick()
            assert "pool:leader:mi-pool" in r._data
            assert "pool:leader:nodo-1" not in r._data
        finally:
            b.stop()

    def test_se_retira_si_otro_coordinador_ya_tiene_el_lease(self, sin_http):
        """El escenario del hallazgo B: un pod `pool-coordinator` en el mismo pool.

        El bully no lo oye —arbitra por mensajes— pero ahora lo ve por el lease.
        """
        r = FakeRedis({"pool:leader:mi-pool": "coordinador-de-la-otra-rama"})
        b = _bully(r)
        b._transition_to(PoolBully.COORDINATOR)
        try:
            b._coordinator._last_lease_renew = 0
            b._coordinator.tick()
            assert b.state == PoolBully.CANDIDATE
            assert b._coordinator is None      # dejó de servir HTTP y de fragmentar
            # y no le robó el lease al otro
            assert lease_holder(r.get("pool:leader:mi-pool")) == "coordinador-de-la-otra-rama"
        finally:
            b.stop()

    def test_retirarse_no_dispara_una_reeleccion_inmediata(self, sin_http):
        """Sin esto quedaría en bucle: gana por RabbitMQ, choca con el lease, repite.

        El dueño del lease es un líder, sólo que uno que no habla nuestro
        transporte, así que se le espera el mismo timeout que a cualquier otro.
        """
        r = FakeRedis({"pool:leader:mi-pool": "otro"})
        b = _bully(r)
        b._transition_to(PoolBully.COORDINATOR)
        try:
            b._coordinator._last_lease_renew = 0
            b._coordinator.tick()

            b.tick()  # un tick de candidato, inmediatamente después
            assert b._election_in_progress is False
            assert b.state == PoolBully.CANDIDATE
            # y el reloj de espera quedó armado, no en cero
            assert time.time() - b._last_heartbeat < 1.0
        finally:
            b.stop()


class TestSigueFuncionandoSinRedis:
    """La ventaja de `pool-auto` es no depender de Redis; eso no se toca."""

    def test_sin_redis_coordina_igual(self, sin_http):
        b = _bully(None)
        b._transition_to(PoolBully.COORDINATOR)
        try:
            assert b._coordinator.is_leader is True
            b._coordinator.tick()          # no debe explotar ni retirarse
            assert b.state == PoolBully.COORDINATOR
        finally:
            b.stop()

    def test_el_claim_sigue_publicandose_por_rabbitmq(self):
        b = _bully(None)
        b._solve_pow()
        assert b._pending_claim is not None
        b.tick()
        tipos = [msg.get("type") for _, msg in b.m.published]
        assert "claim" in tipos


def test_los_ceros_de_la_eleccion_salen_del_mismo_env_que_la_otra_rama(monkeypatch):
    """Estaban hardcodeados acá: subir la dificultad sólo afectaba a una rama."""
    import importlib

    monkeypatch.setenv("POOL_ELECTION_N_ZEROS", "3")
    import worker_pkg.bully as bully_mod
    importlib.reload(bully_mod)
    try:
        assert bully_mod.ELECTION_N_ZEROS == 3
    finally:
        monkeypatch.delenv("POOL_ELECTION_N_ZEROS")
        importlib.reload(bully_mod)
