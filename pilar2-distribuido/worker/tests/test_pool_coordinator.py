"""Tests para Pool Coordinator embebido."""

from __future__ import annotations

import json
import threading
import time
from unittest import mock

import pytest
from common.messaging import InMemoryBus, Messaging

from worker_pkg.pool_coordinator.coordinator import PoolCoordinator, fragment_range
from worker_pkg.pool_coordinator.election import (
    LEASE_RANK_DESIGNATED,
    LEASE_RANK_ELECTED,
    encode_lease,
    lease_holder,
)


class FakePipeline:
    def __init__(self, redis):
        self._redis = redis
        self._cmds = []

    def get(self, key):
        self._cmds.append(("get", key))
        return self

    def pttl(self, key):
        self._cmds.append(("pttl", key))
        return self

    def execute(self):
        results = []
        for cmd, key in self._cmds:
            if cmd == "get":
                results.append(self._redis._data.get(key))
            elif cmd == "pttl":
                results.append(5000 if key in self._redis._data else -2)
        return results


class FakeRedis:
    def __init__(self):
        self._data = {}
        self._list_data: dict = {}

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

    def pttl(self, key):
        return 5000 if key in self._data else -2

    def lindex(self, key, index):
        lst = self._list_data.get(key, [])
        if not lst:
            return None
        try:
            return lst[index]
        except IndexError:
            return None

    def pipeline(self):
        return FakePipeline(self)


class FakeMessaging(Messaging):
    def __init__(self):
        self.published = []
        self.challenges_registered = []
        self._healthy = True

    def on_challenge(self, handler):
        self.challenges_registered.append(handler)

    def publish_nonce_response(self, msg):
        self.published.append(("nonce", msg))

    def publish_keepalive(self, msg):
        self.published.append(("keepalive", msg))

    def connect(self):
        pass

    def start_consuming(self, tick=None, tick_interval=1.0):
        pass

    def close(self):
        pass

    def is_healthy(self):
        return self._healthy

    def unsubscribe(self, handler):
        if handler in self.challenges_registered:
            self.challenges_registered.remove(handler)


class TestFragmentation:
    def test_fragment_range_exacto(self):
        assert fragment_range(0, 100, 25) == [(0, 25), (25, 50), (50, 75), (75, 100)]

    def test_fragment_range_con_resto(self):
        assert fragment_range(0, 10, 3) == [(0, 3), (3, 6), (6, 9), (9, 10)]

    def test_fragment_range_vacio_o_invalido(self):
        assert fragment_range(10, 10, 5) == []
        with pytest.raises(ValueError):
            fragment_range(0, 10, 0)


class TestPoolCoordinator:
    @pytest.fixture
    def coordinator(self):
        m = FakeMessaging()
        r = FakeRedis()
        c = PoolCoordinator(m, pool_id="test-pool", redis=r, mine=lambda *a: (None, None))
        c._running = True
        c.try_acquire_leadership()
        return c

    def test_leadership(self, coordinator):
        assert coordinator.is_leader
        # El lease va namespaceado por pool, no en una clave global.
        assert lease_holder(coordinator.redis.get("pool:leader:test-pool")) == "test-pool"
        assert coordinator.redis.get("pool:leader") is None

    def test_register_miner(self, coordinator):
        mid = coordinator.register_miner(capacity=2, has_gpu=True)
        assert mid.startswith("test-pool-miner-")
        assert len(coordinator._miners) == 1

    def test_heartbeat(self, coordinator):
        mid = coordinator.register_miner()
        assert coordinator.handle_heartbeat(mid) is True
        assert coordinator.handle_heartbeat("nonexistent") is False

    def test_get_next_task_no_miners(self, coordinator):
        task = coordinator.get_next_task("nonexistent")
        assert task is None

    def test_get_next_task_no_fragments(self, coordinator):
        mid = coordinator.register_miner()
        task = coordinator.get_next_task(mid)
        assert task is None

    def test_handle_challenge_fragmenta(self, coordinator):
        mid = coordinator.register_miner(capacity=1)
        coordinator.fragment_size = 25
        coordinator.nonce_space = 100
        coordinator.handle_challenge({
            "voting_window_id": "win-1",
            "law_id": "law-1",
            "action": "promulgacion",
            "partial_hash_base": "abc",
            "n_zeros_required": 4,
        })
        assert len(coordinator._pending_fragments) == 4

    def test_get_next_task_asigna_fragmento(self, coordinator):
        mid1 = coordinator.register_miner(capacity=1)
        coordinator.fragment_size = 25
        coordinator.nonce_space = 100
        coordinator.handle_challenge({
            "voting_window_id": "win-1",
            "law_id": "law-1",
            "action": "promulgacion",
            "partial_hash_base": "abc",
            "n_zeros_required": 4,
            "range_min": "0",
            "range_max": "1000",
        })
        t1 = coordinator.get_next_task(mid1)
        assert t1 is not None
        assert "range_min" in t1
        assert "range_max" in t1

    def test_submit_result(self, coordinator):
        mid = coordinator.register_miner()
        result = {"voting_window_id": "win-1", "nonce": 42,
                   "block_hash_candidato": "0xdead"}
        ok = coordinator.submit_result(mid, result)
        assert ok is True
        nonce_published = any(
            msg_type == "nonce" and msg["nonce"] == 42
            for msg_type, msg in coordinator.m.published
        )
        assert nonce_published

    def _fragmentar(self, coordinator, wid: str, fragmentos: int = 4):
        coordinator.fragment_size = 25
        coordinator.nonce_space = 25 * fragmentos
        coordinator.handle_challenge({
            "voting_window_id": wid,
            "law_id": f"law-{wid}",
            "action": "promulgacion",
            "partial_hash_base": "abc",
            "n_zeros_required": 4,
        })

    def test_submit_result_descarta_fragmentos_de_la_ventana_ganada(self, coordinator):
        # Regresión: al ganar una ventana quedaban en la cola los fragmentos
        # sin repartir, y los mineros seguían barriéndolos antes de atender la
        # ventana siguiente. Con NONCE_SPACE grande eso agrega minutos de
        # trabajo inútil al sellado de cada ley.
        mid = coordinator.register_miner()
        self._fragmentar(coordinator, "win-1", fragmentos=10)
        assert len(coordinator._pending_fragments) == 10

        coordinator.submit_result(mid, {"voting_window_id": "win-1", "nonce": 7,
                                        "block_hash_candidato": "0xbeef"})

        assert len(coordinator._pending_fragments) == 0
        assert coordinator.get_next_task(mid) is None

    def test_nuevo_desafio_descarta_fragmentos_de_ventanas_previas(self, coordinator):
        # Cubre la ventana que vence sin ganador: no pasa por submit_result,
        # así que sus fragmentos se limpian al llegar el desafío siguiente.
        mid = coordinator.register_miner()
        self._fragmentar(coordinator, "win-1", fragmentos=10)
        self._fragmentar(coordinator, "win-2", fragmentos=10)

        assert len(coordinator._pending_fragments) == 10
        wids = {f["voting_window_id"] for f in coordinator._pending_fragments}
        assert wids == {"win-2"}

    def test_descartar_no_toca_fragmentos_de_otra_ventana(self, coordinator):
        self._fragmentar(coordinator, "win-1", fragmentos=4)
        # Se inyecta a mano una ventana ajena (el flujo normal no las mezcla).
        coordinator._pending_fragments.append({
            "voting_window_id": "win-9", "law_id": "law-9", "action": "promulgacion",
            "partial_hash_base": "zzz", "n_zeros_required": 4,
            "range_min": 0, "range_max": 25,
        })

        assert coordinator._discard_fragments("win-1") == 4

        restantes = list(coordinator._pending_fragments)
        assert len(restantes) == 1
        assert restantes[0]["voting_window_id"] == "win-9"

    def test_purge_stale_miners(self, coordinator):
        mid = coordinator.register_miner()
        coordinator._miners[mid]["last_seen"] = 0  # simular miner muerto
        assert len(coordinator._miners) == 1
        coordinator._purge_stale_miners()
        assert len(coordinator._miners) == 0

    def test_policy_accept(self, coordinator):
        coordinator.set_voting_policy({"decision": "accept"})
        assert coordinator._check_voting_policy({"action": "derogacion"}) is True
        assert coordinator._check_voting_policy({"action": "promulgacion"}) is True

    def test_policy_reject_action(self, coordinator):
        coordinator.set_voting_policy({"decision": "reject", "action": "derogacion"})
        assert coordinator._check_voting_policy({"action": "derogacion"}) is False
        assert coordinator._check_voting_policy({"action": "promulgacion"}) is True

    def test_policy_reject_all(self, coordinator):
        coordinator.set_voting_policy({"decision": "reject"})
        assert coordinator._check_voting_policy({"action": "promulgacion"}) is False
        assert coordinator._check_voting_policy({"action": "derogacion"}) is False

    def test_leader_renewal_failure(self, coordinator):
        coordinator.is_leader = True
        coordinator.redis._data["pool:leader:test-pool"] = "other-instance"
        ok = coordinator.renew_leadership()
        assert ok is False
        assert coordinator.is_leader is False


class TestPoolWorkerReregistration:
    """Verifica que el pool-worker se re-registra cuando el coordinator lo pierde."""

    def _make_worker(self, post_side_effect):
        from worker_pkg.pool_worker import PoolWorker

        calls = {"register": 0}

        def fake_mine(*a):
            return (None, None)

        worker = PoolWorker(
            "http://coordinator:9001",
            miner_id="",
            mine=fake_mine,
        )

        def fake_register():
            calls["register"] += 1
            worker.miner_id = f"miner-{calls['register']}"
            worker._registered = True
            return True

        worker.register = fake_register
        worker._post = post_side_effect
        worker._get = lambda path: None
        return worker, calls

    def test_reregistra_cuando_heartbeat_dice_desconocido(self):
        """Si el coordinator responde ok:false, el worker se re-registra."""
        responses = iter([
            {"ok": False},   # heartbeat 1 → coordinator no conoce al miner
            {"ok": True},    # heartbeat 2 → ya re-registrado
        ])

        iteration = {"n": 0}

        def fake_post(path, data):
            if "/heartbeat" in path:
                resp = next(responses, {"ok": True})
                iteration["n"] += 1
                if iteration["n"] >= 2:
                    worker._running = False  # detener tras segunda iteración
                return resp
            return None

        worker, calls = self._make_worker(fake_post)
        worker.heartbeat_interval = 0  # forzar heartbeat en cada tick

        worker.run()

        assert calls["register"] == 2  # se registró dos veces

    def test_no_reregistra_cuando_coordinator_esta_caido(self):
        """Si el HTTP falla (resp None), el worker espera sin re-registrarse."""
        tick = {"n": 0}

        def fake_post(path, data):
            if "/heartbeat" in path:
                tick["n"] += 1
                if tick["n"] >= 3:
                    worker._running = False
                return None  # coordinator caído → HTTP falla

        worker, calls = self._make_worker(fake_post)
        worker.heartbeat_interval = 0

        worker.run()

        assert calls["register"] == 1  # solo el registro inicial


class TestPoolElection:
    """Tests para la elección Bully-by-effort del Pool Coordinator."""

    def _make_coordinator(self, pool_id="pool-A", election_n_zeros=1):
        m = FakeMessaging()
        r = FakeRedis()
        c = PoolCoordinator(
            m, pool_id=pool_id, redis=r,
            mine=lambda *a: (None, None),
            election_n_zeros=election_n_zeros,
        )
        c._running = True
        return c, r

    def test_run_pool_election_gana_cuando_no_hay_lider(self):
        from worker_pkg.pool_coordinator.election import run_pool_election

        r = FakeRedis()
        won = run_pool_election(r, "pool-A", n_zeros=1, lease_ttl=10)
        assert won is True
        assert lease_holder(r.get("pool:leader:pool-A")) == "pool-A"

    def test_run_pool_election_pierde_si_election_key_ya_existe(self):
        from worker_pkg.pool_coordinator.election import run_pool_election, ELECTION_EPOCH_SECONDS
        import time as _time

        r = FakeRedis()
        epoch = int(_time.time() / ELECTION_EPOCH_SECONDS)
        # Otro candidato **del mismo pool** ya ganó esta época.
        r._data[f"pool:election:pool-A:{epoch}"] = "pool-B"

        won = run_pool_election(r, "pool-A", n_zeros=1, lease_ttl=10)
        assert won is False
        assert r.get("pool:leader:pool-A") is None

    def test_run_pool_election_pierde_claim_atomico(self):
        """Simula dos candidatos: pool-A llega primero al SET NX (claim)."""
        from worker_pkg.pool_coordinator.election import run_pool_election, ELECTION_EPOCH_SECONDS
        import time as _time

        epoch = int(_time.time() / ELECTION_EPOCH_SECONDS)

        calls = {"n": 0}
        original_set = FakeRedis.set

        r = FakeRedis()

        def rigged_set(self_r, key, value, *, nx=False, ex=None):
            # El primer SET NX sobre la election_key lo gana pool-B (inyectado antes)
            if nx and f"pool:election:pool-A:{epoch}" in key:
                if calls["n"] == 0:
                    calls["n"] += 1
                    self_r._data[key] = "pool-B"  # pool-B ya lo puso
                    return None  # pool-A pierde
            return original_set(self_r, key, value, nx=nx, ex=ex)

        r.set = lambda *a, **kw: rigged_set(r, *a, **kw)

        won = run_pool_election(r, "pool-A", n_zeros=1, lease_ttl=10)
        assert won is False

    def test_tick_inicia_eleccion_cuando_no_hay_lider(self):
        """tick() dispara _maybe_start_election si no es líder y no hay lease."""
        c, r = self._make_coordinator(election_n_zeros=1)
        assert not c.is_leader

        c._last_lease_renew = 0  # forzar que tick() entre al bloque de lease
        c.tick()

        # Debe haberse iniciado un thread de elección
        assert c._election_thread is not None or c._election_in_progress or c.is_leader

    def test_tick_recoge_resultado_ganador(self):
        """tick() detecta que el thread terminó y actualiza is_leader."""
        c, r = self._make_coordinator(election_n_zeros=1)

        # Simular thread completado con éxito
        c._election_result = True
        c._election_in_progress = False
        dummy_thread = threading.Thread(target=lambda: None)
        dummy_thread.start()
        dummy_thread.join()  # asegurar que is_alive() == False
        c._election_thread = dummy_thread

        c.tick()
        assert c.is_leader is True

    def test_tick_no_inicia_segunda_eleccion_mientras_hay_una_en_curso(self):
        """_maybe_start_election no lanza otro thread si ya hay uno corriendo."""
        c, r = self._make_coordinator(election_n_zeros=1)
        c._election_in_progress = True

        c._last_lease_renew = 0
        c.tick()

        # No debe haber creado un nuevo thread (el existente sigue)
        assert c._election_thread is None

    def test_tick_no_inicia_eleccion_si_otro_es_lider(self):
        """Si ya hay un lider distinto en Redis, no se inicia elección."""
        c, r = self._make_coordinator(election_n_zeros=1)
        r._data["pool:leader:pool-A"] = "pool-B"

        c._last_lease_renew = 0
        c.tick()

        assert c._election_thread is None
        assert not c._election_in_progress

    def test_eleccion_end_to_end_con_thread_real(self):
        """El thread de elección se ejecuta y el coordinador asume liderazgo."""
        c, r = self._make_coordinator(election_n_zeros=1)

        c._last_lease_renew = 0
        c.tick()  # dispara el thread

        # Esperar a que el thread termine (n_zeros=1 es trivialmente rápido)
        if c._election_thread:
            c._election_thread.join(timeout=5)

        c.tick()  # recoger resultado
        assert c.is_leader is True
        assert lease_holder(r.get("pool:leader:pool-A")) == "pool-A"


class TestAgendaTematica:
    """Un equipo sólo aporta cómputo a las categorías que votó (AGENT.md 3.10).

    El filtro vive en el coordinador y en ningún otro lado: los mineros del
    equipo sólo pueden trabajar sobre los fragmentos que él reparte, así que no
    fragmentar es exactamente "el equipo entero no aporta un solo hash".
    """

    @pytest.fixture
    def coordinator(self):
        m = FakeMessaging()
        r = FakeRedis()
        c = PoolCoordinator(m, pool_id="test-pool", redis=r,
                            mine=lambda *a: (None, None))
        c._running = True
        c.try_acquire_leadership()
        return c

    def _challenge(self, category, wid="win-1"):
        return {
            "voting_window_id": wid,
            "law_id": "L1",
            "action": "promulgacion",
            "category": category,
            "n_zeros_required": 2,
            "partial_hash_base": "base",
        }

    def test_sin_agenda_vota_todas_las_categorias(self, coordinator):
        # El default tiene que seguir siendo el pool clásico: quien no eligió
        # agenda mina lo que venga.
        for category in ("economia", "salud", "general"):
            assert coordinator._check_voting_policy(self._challenge(category)) is True

    def test_con_agenda_ignora_las_demas(self, coordinator):
        coordinator.set_voting_policy({"decision": "accept",
                                       "categories": ["economia"]})
        assert coordinator._check_voting_policy(self._challenge("economia")) is True
        assert coordinator._check_voting_policy(self._challenge("salud")) is False

    def test_la_ventana_ajena_no_genera_ni_un_fragmento(self, coordinator):
        coordinator.set_voting_policy({"decision": "accept",
                                       "categories": ["economia"]})
        coordinator.handle_challenge(self._challenge("salud"))
        assert len(coordinator._pending_fragments) == 0

        coordinator.handle_challenge(self._challenge("economia", wid="win-2"))
        assert len(coordinator._pending_fragments) > 0
        assert coordinator._pending_fragments[0]["category"] == "economia"

    def test_la_agenda_convive_con_el_veto_por_accion(self, coordinator):
        # Son dos filtros independientes: la agenda dice de qué áreas, el veto
        # dice qué acciones. Ninguno debería anular al otro.
        coordinator.set_voting_policy({"decision": "reject", "action": "derogacion",
                                       "categories": ["economia"]})
        economia = self._challenge("economia")
        assert coordinator._check_voting_policy(economia) is True
        economia["action"] = "derogacion"
        assert coordinator._check_voting_policy(economia) is False
        # y fuera de la agenda no mina ni las promulgaciones
        assert coordinator._check_voting_policy(self._challenge("salud")) is False

    def test_una_categoria_desconocida_no_tira_la_politica(self, coordinator):
        # La política puede venir de un backend más nuevo. Quedarse sin minar por
        # un slug que no reconocemos sería peor que ignorarlo.
        coordinator.set_voting_policy({"decision": "accept",
                                       "categories": ["economia", "astrologia"]})
        assert coordinator.voting_categories() == ["economia"]

    def test_la_agenda_se_relee_de_redis(self, coordinator):
        coordinator.redis._data["pool:policy:test-pool"] = json.dumps(
            {"decision": "accept", "categories": ["salud"]})
        coordinator._sync_voting_policy()
        assert coordinator.voting_categories() == ["salud"]
        assert coordinator._check_voting_policy(self._challenge("economia")) is False

    def test_releer_la_misma_agenda_no_la_reescribe(self, coordinator):
        # Se compara ya normalizada: si comparáramos el JSON crudo, el orden de
        # las categorías haría parecer que cambió en cada tick (cada 3 s).
        coordinator.redis._data["pool:policy:test-pool"] = json.dumps(
            {"decision": "accept", "categories": ["salud", "economia"]})
        coordinator._sync_voting_policy()
        antes = coordinator._voting_policy
        coordinator._sync_voting_policy()
        assert coordinator._voting_policy is antes

    def test_la_agenda_se_sincroniza_aunque_no_tengamos_el_lease(self, coordinator):
        """El lease de líder no gobierna la agenda.

        `handle_challenge` fragmenta mire o no el lease, así que gatear la
        sincronización por liderazgo dejaba a un coordinador sin lease minando
        con el default (todas) en vez de con la agenda que su equipo eligió.
        """
        coordinator.is_leader = False
        coordinator.redis._data["pool:policy:test-pool"] = json.dumps(
            {"decision": "accept", "categories": ["ambiente"]})
        coordinator.tick()
        assert coordinator.voting_categories() == ["ambiente"]


class TestAislamientoEntrePools:
    """Cada pool tiene su propio lease; dos equipos no compiten por coordinar.

    El lease responde "¿soy yo el coordinador vivo de **mi** pool?", no "¿soy el
    único pool de la red". Dos equipos son organizaciones independientes: compiten
    por el nonce de la ventana, no por un lease. Con la clave global que había
    antes, el primer coordinador que arrancaba se la quedaba y los demás equipos
    nunca lograban tomar la suya — se quedaban sin emitir keepalive y sin figurar
    en las métricas.
    """

    def _coordinator(self, pool_id, redis):
        c = PoolCoordinator(FakeMessaging(), pool_id=pool_id, redis=redis,
                            mine=lambda *a: (None, None))
        c._running = True
        return c

    def test_dos_equipos_toman_su_lease_a_la_vez(self):
        r = FakeRedis()
        a = self._coordinator("equipo-a", r)
        b = self._coordinator("equipo-b", r)

        assert a.try_acquire_leadership() is True
        assert b.try_acquire_leadership() is True   # antes devolvía False
        assert a.is_leader and b.is_leader
        assert lease_holder(r.get("pool:leader:equipo-a")) == "equipo-a"
        assert lease_holder(r.get("pool:leader:equipo-b")) == "equipo-b"

    def test_el_lease_ajeno_no_desaloja(self):
        # Renovar mira sólo la clave propia: que otro equipo tenga la suya no
        # puede hacernos ceder el liderazgo del nuestro.
        r = FakeRedis()
        a = self._coordinator("equipo-a", r)
        a.try_acquire_leadership()
        r._data["pool:leader:equipo-b"] = "equipo-b"

        assert a.renew_leadership() is True
        assert a.is_leader is True

    def test_dos_candidatos_del_mismo_pool_si_compiten(self):
        """El caso que el lease **sí** tiene que arbitrar: HA dentro de un pool."""
        r = FakeRedis()
        primero = self._coordinator("equipo-a", r)
        suplente = self._coordinator("equipo-a", r)

        assert primero.try_acquire_leadership() is True
        assert suplente.try_acquire_leadership() is False

    def test_la_eleccion_de_un_equipo_no_le_gana_el_claim_a_otro(self):
        from worker_pkg.pool_coordinator.election import run_pool_election

        r = FakeRedis()
        assert run_pool_election(r, "equipo-a", n_zeros=1, lease_ttl=10) is True
        # El claim de A no bloquea la elección de B: están en épocas del mismo
        # instante pero en namespaces distintos.
        assert run_pool_election(r, "equipo-b", n_zeros=1, lease_ttl=10) is True
        assert lease_holder(r.get("pool:leader:equipo-a")) == "equipo-a"
        assert lease_holder(r.get("pool:leader:equipo-b")) == "equipo-b"

    def test_cada_equipo_lee_su_propia_agenda(self):
        """Cierra el lazo con las categorías: lease propio ⇒ política propia."""
        r = FakeRedis()
        a = self._coordinator("equipo-a", r)
        b = self._coordinator("equipo-b", r)
        r._data["pool:policy:equipo-a"] = json.dumps(
            {"decision": "accept", "categories": ["economia"]})
        r._data["pool:policy:equipo-b"] = json.dumps(
            {"decision": "accept", "categories": ["salud"]})

        a._sync_voting_policy()
        b._sync_voting_policy()
        assert a.voting_categories() == ["economia"]
        assert b.voting_categories() == ["salud"]

    def test_borrar_la_politica_vuelve_a_votar_todo(self):
        """Borrar la clave es la forma obvia de decir "este pool ya no tiene agenda".

        Conservar la última vista dejaba al coordinador filtrando por algo que ya
        nadie pidió, y sin ninguna forma de enterarse leyendo Redis.
        """
        r = FakeRedis()
        c = self._coordinator("equipo-a", r)
        r._data["pool:policy:equipo-a"] = json.dumps(
            {"decision": "accept", "categories": ["economia"]})
        c._sync_voting_policy()
        assert c.voting_categories() == ["economia"]

        del r._data["pool:policy:equipo-a"]
        c._sync_voting_policy()
        assert c.voting_categories() == []
        assert c._check_voting_policy({"category": "salud"}) is True


class TestArbitrajeExterno:
    """`elect_leader=False`: el bully ya eligió, pero el lease se sostiene igual.

    Es lo que hace que las dos elecciones (RabbitMQ y Redis) se vean entre sí.
    El coordinador no compite por el puesto —eso ya se resolvió por mensajes—
    pero sí toma el lease del pool, que es el recurso del que hay uno solo.
    """

    def _coordinator(self, redis, **kw):
        kw.setdefault("elect_leader", False)
        kw.setdefault("lease_key", "pool:leader:mi-pool")
        c = PoolCoordinator(FakeMessaging(), pool_id="nodo-1", redis=redis,
                            mine=lambda *a: (None, None), **kw)
        c._running = True
        return c

    def test_manda_desde_el_arranque_sin_elegir(self):
        c = self._coordinator(FakeRedis())
        assert c.is_leader is True          # el bully ya arbitró
        assert c.elect_leader is False

    def test_toma_el_lease_del_pool_no_el_del_nodo(self):
        r = FakeRedis()
        c = self._coordinator(r)
        c._last_lease_renew = 0
        c.tick()
        # El lease se toma en nombre del pool; la identidad del nodo (pool_id) es
        # el valor, no la clave.
        assert lease_holder(r.get("pool:leader:mi-pool")) == "nodo-1"
        assert r.get("pool:leader:nodo-1") is None

    def test_nunca_arranca_una_eleccion_propia(self):
        r = FakeRedis()
        r._data["pool:leader:mi-pool"] = "otro-coordinador"
        c = self._coordinator(r)
        c._last_lease_renew = 0
        c.tick()
        # Perdió el lease, pero no compite por recuperarlo: el arbitraje de este
        # modo es del bully, no de Redis.
        assert c.is_leader is False
        assert c._election_thread is None
        assert c._election_in_progress is False

    def test_avisa_al_duenio_cuando_pierde_el_lease(self):
        avisos = []
        r = FakeRedis()
        r._data["pool:leader:mi-pool"] = "otro-coordinador"
        c = self._coordinator(r, on_lost_leadership=lambda: avisos.append(1))
        c._last_lease_renew = 0
        c.tick()
        # Sin este aviso el bully seguiría creyéndose coordinador, sirviendo HTTP
        # y fragmentando en paralelo al que sí tiene el lease.
        assert avisos == [1]

    def test_sin_redis_manda_igual(self):
        # `pool-auto` tiene que seguir funcionando en nodos federados sin Redis.
        c = self._coordinator(None)
        c._last_lease_renew = 0
        c.tick()
        assert c.is_leader is True


class TestRangoDelLease:
    """El designado desplaza al electo; el electo nunca al designado.

    Los dos coordinadores comparten el lease a propósito (así no coordinan en
    paralelo sin enterarse), pero no son intercambiables: a uno lo eligió una
    persona desde la pantalla de Equipos y al otro una elección entre nodos
    anónimos. Sin rango, quién se quedaba con el pool lo decidía el orden de
    arranque.
    """

    LEASE = "pool:leader:mi-pool"

    def _designado(self, redis, pool_id="coordinador-del-equipo"):
        # El del modo `pool-coordinator`: rango por default.
        c = PoolCoordinator(FakeMessaging(), pool_id=pool_id, redis=redis,
                            mine=lambda *a: (None, None), lease_key=self.LEASE)
        c._running = True
        return c

    def _electo(self, redis, pool_id="nodo-anonimo"):
        # El que arranca el bully: `elect_leader=False` y rango `elected`.
        c = PoolCoordinator(FakeMessaging(), pool_id=pool_id, redis=redis,
                            mine=lambda *a: (None, None), lease_key=self.LEASE,
                            lease_rank=LEASE_RANK_ELECTED, elect_leader=False)
        c._running = True
        return c

    def test_el_designado_le_saca_el_lease_al_electo(self):
        r = FakeRedis()
        electo = self._electo(r)
        electo._last_lease_renew = 0
        electo.tick()
        assert lease_holder(r.get(self.LEASE)) == "nodo-anonimo"

        # Llega el coordinador que el dueño del equipo eligió a dedo.
        designado = self._designado(r)
        assert designado.try_acquire_leadership() is True
        assert lease_holder(r.get(self.LEASE)) == "coordinador-del-equipo"

        # Y el electo se entera en su próxima renovación: cede y avisa.
        avisos = []
        electo._on_lost_leadership = lambda: avisos.append(1)
        assert electo.renew_leadership() is False
        assert electo.is_leader is False
        assert avisos == [1]

    def test_el_electo_no_le_saca_el_lease_al_designado(self):
        r = FakeRedis()
        designado = self._designado(r)
        assert designado.try_acquire_leadership() is True

        electo = self._electo(r)
        assert electo.try_acquire_leadership() is False
        assert lease_holder(r.get(self.LEASE)) == "coordinador-del-equipo"
        # Y tampoco se lo lleva por renovación, que es el camino del bully.
        electo.is_leader = True
        assert electo.renew_leadership() is False
        assert lease_holder(r.get(self.LEASE)) == "coordinador-del-equipo"

    def test_el_empate_no_desplaza(self):
        """Dos del mismo rango en un pool son HA, no un conflicto de modos.

        Si el empate desplazara, se robarían el lease en cada tick en bucle en
        vez de que el segundo espere su turno.
        """
        r = FakeRedis()
        primero = self._designado(r, "coord-a")
        suplente = self._designado(r, "coord-b")
        assert primero.try_acquire_leadership() is True
        assert suplente.try_acquire_leadership() is False

        r2 = FakeRedis()
        uno = self._electo(r2, "nodo-1")
        otro = self._electo(r2, "nodo-2")
        assert uno.try_acquire_leadership() is True
        assert otro.try_acquire_leadership() is False

    def test_un_lease_sin_rango_no_se_desplaza(self):
        """Compatibilidad durante un rollout: si no sabemos clasificarlo, no lo tocamos."""
        r = FakeRedis()
        r._data[self.LEASE] = "coordinador-viejo"   # formato previo al rango

        electo = self._electo(r)
        assert electo.try_acquire_leadership() is False
        assert lease_holder(r.get(self.LEASE)) == "coordinador-viejo"

    def test_el_designado_compite_por_un_pool_que_tiene_un_electo(self):
        """`_maybe_start_election` no puede frenarse ante un dueño de rango menor.

        Es el camino real por el que un coordinador de equipo recupera su pool:
        si el lease ocupado lo frenara siempre, nunca llegaría a elegirse.
        """
        r = FakeRedis()
        r._data[self.LEASE] = encode_lease("nodo-anonimo", LEASE_RANK_ELECTED)

        designado = self._designado(r)
        designado._election_n_zeros = 1        # PoW trivial: no es lo que se prueba
        designado._maybe_start_election()
        assert designado._election_thread is not None
        designado._election_thread.join(timeout=5)
        assert designado._election_result is True
        assert lease_holder(r.get(self.LEASE)) == "coordinador-del-equipo"

    def test_el_designado_no_compite_contra_otro_designado(self):
        r = FakeRedis()
        r._data[self.LEASE] = encode_lease("otro-equipo", LEASE_RANK_DESIGNATED)

        designado = self._designado(r)
        designado._maybe_start_election()
        assert designado._election_thread is None
        assert lease_holder(r.get(self.LEASE)) == "otro-equipo"


class TestSoltarElLease:
    def _coordinator(self, redis, pool_id="equipo-a"):
        c = PoolCoordinator(FakeMessaging(), pool_id=pool_id, redis=redis,
                            mine=lambda *a: (None, None))
        c._running = True
        return c

    def test_parar_libera_el_lease(self):
        """Un sucesor no debería esperar el TTL entero por un apagado ordenado."""
        r = FakeRedis()
        c = self._coordinator(r)
        c.try_acquire_leadership()
        assert lease_holder(r.get("pool:leader:equipo-a")) == "equipo-a"

        c.stop()
        assert r.get("pool:leader:equipo-a") is None
        assert c.is_leader is False

    def test_no_borra_el_lease_ajeno(self):
        r = FakeRedis()
        c = self._coordinator(r)
        c.try_acquire_leadership()
        # Mientras estábamos vivos, otro se quedó con el lease (nuestro TTL venció).
        r._data["pool:leader:equipo-a"] = "otro"

        c.stop()
        assert lease_holder(r.get("pool:leader:equipo-a")) == "otro"
