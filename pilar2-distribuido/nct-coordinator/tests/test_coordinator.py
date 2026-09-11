"""Tests de comportamiento del NCT (reglas de gobierno AGENT.md 3–4)."""

import hashlib

import pytest

from common.blockchain import validate_chain
from common.blockchain.challenge import build_partial_hash_base
from common.storage import CooldownReason, LawStatus, WindowResult
from nct.coordinator import NCTCoordinator


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_nct(bus, store, clock, **kw):
    params = dict(n_zeros=2, window_seconds_promulgacion=60,
                  window_seconds_derogacion=90, cooldown_new=2,
                  cooldown_reproposed=4, clock=clock)
    params.update(kw)
    nct = NCTCoordinator(bus, store, **params)
    nct.wire()
    return nct


def solve(base, n_zeros):
    prefix = "0" * n_zeros
    nonce = 0
    while not hashlib.md5(f"{base}{nonce}".encode()).hexdigest().startswith(prefix):
        nonce += 1
    return nonce


def capture_challenges(bus):
    challenges = []
    bus.on_challenge(challenges.append)
    return challenges


def test_propuesta_abre_ventana_y_publica_desafio(bus, store):
    clock = Clock()
    challenges = capture_challenges(bus)
    make_nct(bus, store, clock)
    bus.publish_proposal({"law_id": "L1", "author_pubkey": "A",
                          "text_hash": "h1", "created_at": "t0"})
    assert len(challenges) == 1
    assert challenges[0]["n_zeros_required"] == 2
    assert challenges[0]["action"] == "promulgacion"
    assert store.get_law("L1")["status"] == LawStatus.IN_WINDOW
    assert store.get_active_window() == challenges[0]["voting_window_id"]


def test_flujo_completo_sella_bloque_con_cadena_valida(bus, store):
    clock = Clock()
    challenges = capture_challenges(bus)
    make_nct(bus, store, clock)
    bus.publish_proposal({"law_id": "L1", "author_pubkey": "A",
                          "text_hash": "h1", "created_at": "t0"})
    ch = challenges[0]
    nonce = solve(ch["partial_hash_base"], ch["n_zeros_required"])
    bus.publish_nonce_response({"voting_window_id": ch["voting_window_id"],
                                "nonce": nonce, "winning_node_or_pool": "pool-X",
                                "block_hash_candidato": "x"})
    assert store.chain_length() == 1
    assert store.get_law("L1")["status"] == LawStatus.PROMULGATED
    assert store.get_active_window() is None
    assert store.get_window(ch["voting_window_id"])["result"] == WindowResult.SUCCESS
    assert validate_chain(store.get_chain()) is True


def test_descarta_nonce_de_otra_ventana(bus, store):
    clock = Clock()
    challenges = capture_challenges(bus)
    make_nct(bus, store, clock)
    bus.publish_proposal({"law_id": "L1", "author_pubkey": "A",
                          "text_hash": "h1", "created_at": "t0"})
    bus.publish_nonce_response({"voting_window_id": "ventana-falsa",
                                "nonce": 0, "winning_node_or_pool": "pool-X"})
    assert store.chain_length() == 0
    assert store.get_active_window() == challenges[0]["voting_window_id"]


def test_descarta_nonce_invalido(bus, store):
    clock = Clock()
    challenges = capture_challenges(bus)
    make_nct(bus, store, clock, n_zeros=5)
    bus.publish_proposal({"law_id": "L1", "author_pubkey": "A",
                          "text_hash": "h1", "created_at": "t0"})
    ch = challenges[0]
    bus.publish_nonce_response({"voting_window_id": ch["voting_window_id"],
                                "nonce": 1, "winning_node_or_pool": "pool-X"})
    assert store.chain_length() == 0


def test_descarta_nonce_tardio_post_deadline(bus, store):
    clock = Clock()
    challenges = capture_challenges(bus)
    make_nct(bus, store, clock)
    bus.publish_proposal({"law_id": "L1", "author_pubkey": "A",
                          "text_hash": "h1", "created_at": "t0"})
    ch = challenges[0]
    nonce = solve(ch["partial_hash_base"], ch["n_zeros_required"])
    clock.t += 1000  # pasó el deadline
    bus.publish_nonce_response({"voting_window_id": ch["voting_window_id"],
                                "nonce": nonce, "winning_node_or_pool": "pool-X"})
    assert store.chain_length() == 0


def test_autor_no_puede_ganar_su_propia_ventana(bus, store):
    clock = Clock()
    challenges = capture_challenges(bus)
    make_nct(bus, store, clock)
    bus.publish_proposal({"law_id": "L1", "author_pubkey": "A",
                          "text_hash": "h1", "created_at": "t0"})
    ch = challenges[0]
    nonce = solve(ch["partial_hash_base"], ch["n_zeros_required"])
    bus.publish_nonce_response({"voting_window_id": ch["voting_window_id"],
                                "nonce": nonce, "winning_node_or_pool": "A"})
    assert store.chain_length() == 0


def test_ventana_vencida_descarta_ley_y_no_reencola(bus, store):
    clock = Clock()
    challenges = capture_challenges(bus)
    nct = make_nct(bus, store, clock)
    bus.publish_proposal({"law_id": "L1", "author_pubkey": "A",
                          "text_hash": "h1", "created_at": "t0"})
    ch = challenges[0]
    clock.t += 1000
    nct.check_deadline()
    assert store.get_law("L1")["status"] == LawStatus.DISCARDED
    assert store.get_window(ch["voting_window_id"])["result"] == WindowResult.EXPIRED_PENDING
    assert store.get_active_window() is None
    assert "L1" not in store.queued_law_ids()  # no se reencola
    assert store.is_text_hash_discarded("h1")


def test_cooldown_bloquea_repropuesta_inmediata(bus, store):
    clock = Clock()
    challenges = capture_challenges(bus)
    make_nct(bus, store, clock)
    bus.publish_proposal({"law_id": "L1", "author_pubkey": "A",
                          "text_hash": "h1", "created_at": "t0"})
    # A intenta proponer otra ley estando en cooldown
    bus.publish_proposal({"law_id": "L2", "author_pubkey": "A",
                          "text_hash": "h2", "created_at": "t1"})
    assert store.get_law("L2") is None  # rechazada, ni siquiera se guarda
    assert store.queued_law_ids() == []  # L1 ya está en ventana, L2 rechazada


def test_reproposicion_identica_penaliza_mas_que_una_nueva(bus, store):
    clock = Clock()
    capture_challenges(bus)
    make_nct(bus, store, clock)
    # Descartamos h_repetida para simular una ley previa pendiente
    store.mark_text_hash_discarded("h_repetida")
    # Autor B repropone texto idéntico ya descartado
    bus.publish_proposal({"law_id": "Lrep", "author_pubkey": "B",
                          "text_hash": "h_repetida", "created_at": "t"})
    # Autor C propone texto nuevo
    bus.publish_proposal({"law_id": "Lnew", "author_pubkey": "C",
                          "text_hash": "h_nueva", "created_at": "t"})
    cd_b = store.get_cooldown("B")
    cd_c = store.get_cooldown("C")
    assert cd_b["cooldown_reason"] == CooldownReason.REPROPOSED_IDENTICAL
    assert cd_c["cooldown_reason"] == CooldownReason.PROPOSED_NEW
    assert int(cd_b["cooldown_until_window"]) > int(cd_c["cooldown_until_window"])


def test_derogacion_exige_n_mas_uno_ceros(bus, store):
    clock = Clock()
    challenges = capture_challenges(bus)
    make_nct(bus, store, clock)
    # Promulgar L1 primero
    bus.publish_proposal({"law_id": "L1", "author_pubkey": "A",
                          "text_hash": "h1", "created_at": "t0"})
    ch = challenges[0]
    nonce = solve(ch["partial_hash_base"], ch["n_zeros_required"])
    bus.publish_nonce_response({"voting_window_id": ch["voting_window_id"],
                                "nonce": nonce, "winning_node_or_pool": "pool-X"})
    assert store.get_law("L1")["status"] == LawStatus.PROMULGATED
    # Derogación de L1 por autor B
    bus.publish_proposal({"law_id": "L1", "author_pubkey": "B",
                          "text_hash": "h1", "created_at": "t2",
                          "action": "derogacion"})
    derog_ch = challenges[-1]
    assert derog_ch["action"] == "derogacion"
    assert derog_ch["n_zeros_required"] == 3  # n=2 → n+1
    nonce2 = solve(derog_ch["partial_hash_base"], 3)
    bus.publish_nonce_response({"voting_window_id": derog_ch["voting_window_id"],
                                "nonce": nonce2, "winning_node_or_pool": "pool-Y"})
    assert store.get_law("L1")["status"] == LawStatus.REPEALED
    assert store.chain_length() == 2
    chain = store.get_chain()
    # validación con resolver de base desde la ventana persistida en Redis
    resolver = lambda b: store.get_window(b.voting_window_id)["partial_hash_base"]
    assert validate_chain(chain, base_resolver=resolver) is True


def test_round_robin_entre_dos_autores_en_ventanas_sucesivas(bus, store):
    clock = Clock()
    challenges = capture_challenges(bus)
    nct = make_nct(bus, store, clock, cooldown_new=0)
    # A y B proponen; ventana 1 = A (más antigua)
    bus.publish_proposal({"law_id": "LA1", "author_pubkey": "A",
                          "text_hash": "ha1", "created_at": "t"})
    bus.publish_proposal({"law_id": "LA2", "author_pubkey": "A",
                          "text_hash": "ha2", "created_at": "t"})
    bus.publish_proposal({"law_id": "LB1", "author_pubkey": "B",
                          "text_hash": "hb1", "created_at": "t"})
    assert challenges[0]["law_id"] == "LA1"
    # resolver ventana 1
    ch = challenges[0]
    nonce = solve(ch["partial_hash_base"], ch["n_zeros_required"])
    bus.publish_nonce_response({"voting_window_id": ch["voting_window_id"],
                                "nonce": nonce, "winning_node_or_pool": "p"})
    # ventana 2 debe saltar a B (no a LA2), por round-robin
    assert challenges[1]["law_id"] == "LB1"


def test_seal_aborta_si_cas_falla_sin_fork(bus, store):
    """Si append_block devuelve False (tip cambió), _seal aborta sin fork (A-04).

    Simula el escenario de split-brain: un NCT-B intenta sellar su ventana pero
    NCT-A ya avanzó el tip. El bloque de NCT-B se rechaza, la ley se re-encola
    y la cadena queda intacta.
    """
    from unittest.mock import patch
    from common.blockchain.block import GENESIS_PREVIOUS_HASH

    clock = Clock()
    challenges = capture_challenges(bus)
    nct = make_nct(bus, store, clock)

    # Abrir ventana con L1
    bus.publish_proposal({"law_id": "L1", "author_pubkey": "A",
                          "text_hash": "h1", "created_at": "t0"})
    ch = challenges[0]
    nonce = solve(ch["partial_hash_base"], ch["n_zeros_required"])

    # Forzar que append_block devuelva False (otro NCT se adelantó)
    with patch.object(store, "append_block", return_value=False):
        bus.publish_nonce_response({"voting_window_id": ch["voting_window_id"],
                                    "nonce": nonce, "winning_node_or_pool": "pool-X"})

    # Cadena intacta: no se agregó ningún bloque → no hay fork
    assert store.chain_length() == 0
    # La ley no quedó marcada como promulgada (el sellado fue abortado)
    assert store.get_law("L1")["status"] != LawStatus.PROMULGATED
    # maybe_open_window() re-abre inmediatamente una nueva ventana con L1,
    # así que el NCT no queda con estado colgado del intento fallido
    assert nct._active is not None
    assert nct._active["law_id"] == "L1"


# ---------------------------------------------------------------------------
# Categorías de ley (AGENT.md 3.10)
# ---------------------------------------------------------------------------

class TestCategorias:
    """La categoría viaja desde la propuesta hasta el desafío publicado.

    Es la cadena que hace posible que un equipo decida no aportar cómputo: si la
    categoría se pierde en cualquier eslabón, el coordinador la ve como
    ``general`` y su agenda deja de filtrar nada.
    """

    def test_la_categoria_declarada_llega_al_desafio(self, bus, store):
        challenges = capture_challenges(bus)
        make_nct(bus, store, Clock())
        bus.publish_proposal({"law_id": "L1", "author_pubkey": "A",
                              "text_hash": "h1", "created_at": "t0",
                              "category": "economia"})
        assert store.get_law("L1")["category"] == "economia"
        assert challenges[0]["category"] == "economia"
        assert store.get_window(challenges[0]["voting_window_id"])["category"] \
            == "economia"

    def test_sin_categoria_declarada_es_general(self, bus, store):
        # Todo el camino legacy (scripts sin --category, nodos viejos) sigue
        # funcionando: cae en general, que es un área como cualquier otra.
        challenges = capture_challenges(bus)
        make_nct(bus, store, Clock())
        bus.publish_proposal({"law_id": "L1", "author_pubkey": "A",
                              "text_hash": "h1", "created_at": "t0"})
        assert challenges[0]["category"] == "general"

    def test_categoria_inventada_no_se_encola(self, bus, store):
        # Se rechaza en vez de normalizar a general: el autor firmó "economia
        # popular" y encolar otra cosa dejaría su ley esperando a equipos que no
        # existen.
        challenges = capture_challenges(bus)
        make_nct(bus, store, Clock())
        bus.publish_proposal({"law_id": "L1", "author_pubkey": "A",
                              "text_hash": "h1", "created_at": "t0",
                              "category": "astrologia"})
        assert challenges == []
        assert store.get_law("L1") is None

    def test_la_derogacion_hereda_la_categoria_de_la_ley(self, bus, store):
        """Derogar convoca a los mismos equipos que promulgaron.

        Si el que deroga pudiera reetiquetar, elegiría el área donde su facción
        mina y la ajena no — y la asimetría n+1 dejaría de ser el único costo
        extra de derogar.
        """
        challenges = capture_challenges(bus)
        make_nct(bus, store, Clock())
        bus.publish_proposal({"law_id": "L1", "author_pubkey": "A",
                              "text_hash": "h1", "created_at": "t0",
                              "category": "salud"})
        ch = challenges[0]
        nonce = solve(ch["partial_hash_base"], ch["n_zeros_required"])
        bus.publish_nonce_response({"voting_window_id": ch["voting_window_id"],
                                    "nonce": nonce, "winning_node_or_pool": "pool-X"})
        assert store.get_law("L1")["status"] == LawStatus.PROMULGATED

        # B propone derogarla declarando otra área; se ignora.
        bus.publish_proposal({"law_id": "L1", "author_pubkey": "B",
                              "text_hash": "h1", "created_at": "t1",
                              "action": "derogacion", "category": "economia"})
        assert challenges[-1]["action"] == "derogacion"
        assert challenges[-1]["category"] == "salud"


class TestCuotaDeTurnos:
    """La cuota vista desde el NCT: historial en el store + selección real.

    `common/tests/test_turn_quota.py` cubre la regla pura; acá se prueba que el
    NCT efectivamente la alimenta (anota cada turno) y la consulta al elegir.
    """

    def test_el_nct_anota_cada_turno_en_el_historial(self, bus, store):
        clock = Clock()
        make_nct(bus, store, clock)
        bus.publish_proposal({"law_id": "L1", "author_pubkey": "A",
                              "text_hash": "h1", "created_at": "t0"})
        assert store.recent_window_authors(10) == ["A"]

    def test_una_identidad_monopolizadora_cede_el_turno(self, bus, store):
        clock = Clock()
        nct = make_nct(bus, store, clock, turn_quota_windows=4,
                       turn_quota_max_share=0.5)
        # "A" ya se llevó las últimas 4 ventanas (100% > 50%).
        for _ in range(4):
            store.push_window_author("A")

        # Con dos leyes en cola, la más antigua es de A: sin cuota le tocaría a él.
        store.save_law(law_id="LA", author_pubkey="A", text_hash="ha",
                       created_at="t0")
        store.enqueue_law("LA")
        store.save_law(law_id="LB", author_pubkey="B", text_hash="hb",
                       created_at="t1")
        store.enqueue_law("LB")

        challenges = capture_challenges(bus)
        nct._last_author = None
        nct.maybe_open_window()

        assert len(challenges) == 1
        assert challenges[0]["law_id"] == "LB"

    def test_la_cola_no_se_bloquea_si_el_unico_autor_excedio(self, bus, store):
        clock = Clock()
        nct = make_nct(bus, store, clock, turn_quota_windows=4,
                       turn_quota_max_share=0.5)
        for _ in range(4):
            store.push_window_author("A")
        store.save_law(law_id="LA", author_pubkey="A", text_hash="ha",
                       created_at="t0")
        store.enqueue_law("LA")

        challenges = capture_challenges(bus)
        nct._last_author = None
        nct.maybe_open_window()

        # Excedido y todo, abre: una cuota que frene el sistema entero sería un
        # DoS más barato que el ataque que intenta evitar.
        assert len(challenges) == 1
        assert challenges[0]["law_id"] == "LA"


class TestDificultadDinamica:
    """`n` recalculado por ventana según el cómputo vivo (AGENT.md 3.6).

    Lo que se prueba acá es el cableado: que el NCT mida la población real del
    store, que el `n` resultante llegue al desafío, y que el espacio de nonces
    viaje con él. La matemática está en `common/tests/test_difficulty.py`.
    """

    def _con_mineros(self, store, cuantos, equipo=False, gpu=False):
        import json
        for i in range(cuantos):
            store.r.set(f"worker:status:w{i}", json.dumps(
                {"worker_id": f"w{i}", "has_gpu": gpu, "capacity": 1}))
        if equipo:
            store.r.sadd("teams", "faccion")
            store.r.hset("team:faccion", mapping={
                "coordinator_worker_id": "w0", "name": "F"})
            for i in range(1, cuantos):
                store.r.sadd("team:members:faccion", f"w{i}")

    def _nct(self, bus, store, clock, **kw):
        params = dict(dynamic_difficulty=True, difficulty_target_seconds=30.0,
                      hps_cpu=1_000_000.0, hps_gpu=1e8)
        params.update(kw)
        return make_nct(bus, store, clock, **params)

    def test_registrar_standalone_no_abarata_la_ley(self, bus, store):
        """El pedido original: 1 minero o 1000, la misma dificultad."""
        clock = Clock()
        self._con_mineros(store, 1)
        challenges = capture_challenges(bus)
        self._nct(bus, store, clock)
        bus.publish_proposal({"law_id": "L1", "author_pubkey": "A",
                              "text_hash": "h1", "created_at": "t0"})
        con_uno = challenges[0]["n_zeros_required"]

        store.r.flushdb()
        self._con_mineros(store, 1000)
        challenges2 = capture_challenges(bus)
        self._nct(bus, store, Clock())
        bus.publish_proposal({"law_id": "L2", "author_pubkey": "B",
                              "text_hash": "h2", "created_at": "t0"})
        assert challenges2[0]["n_zeros_required"] == con_uno

    def test_un_equipo_grande_si_sube_la_dificultad(self, bus, store):
        clock = Clock()
        self._con_mineros(store, 40, equipo=True)
        challenges = capture_challenges(bus)
        self._nct(bus, store, clock)
        bus.publish_proposal({"law_id": "L1", "author_pubkey": "A",
                              "text_hash": "h1", "created_at": "t0"})
        # 40 mineros fragmentando ⇒ 40x el cómputo de uno solo ⇒ n más alto.
        assert challenges[0]["n_zeros_required"] > 4

    def test_el_espacio_de_nonces_viaja_con_la_dificultad(self, bus, store):
        """Sin esto el minero barre un rango donde la solución no está."""
        from common.blockchain.difficulty import nonce_space_for
        clock = Clock()
        self._con_mineros(store, 30, equipo=True)
        challenges = capture_challenges(bus)
        self._nct(bus, store, clock)
        bus.publish_proposal({"law_id": "L1", "author_pubkey": "A",
                              "text_hash": "h1", "created_at": "t0"})
        ch = challenges[0]
        assert ch["nonce_space"] == nonce_space_for(ch["n_zeros_required"])

    def test_la_derogacion_sigue_costando_n_mas_uno(self, bus, store):
        """La regla del enunciado no cambia porque `n` sea dinámico."""
        clock = Clock()
        self._con_mineros(store, 1)
        challenges = capture_challenges(bus)
        nct = self._nct(bus, store, clock)
        bus.publish_proposal({"law_id": "L1", "author_pubkey": "A",
                              "text_hash": "h1", "created_at": "t0"})
        n_promulgacion = challenges[0]["n_zeros_required"]
        nonce = solve(challenges[0]["partial_hash_base"], n_promulgacion)
        bus.publish_nonce_response({"voting_window_id": challenges[0]["voting_window_id"],
                                    "nonce": nonce, "winning_node_or_pool": "w0"})
        bus.publish_proposal({"law_id": "L1", "author_pubkey": "B",
                              "text_hash": "h1", "created_at": "t1",
                              "action": "derogacion"})
        assert challenges[-1]["n_zeros_required"] == n_promulgacion + 1

    def test_con_dificultad_fija_n_no_se_mueve(self, bus, store):
        """El modo por defecto sigue siendo el del enunciado original."""
        clock = Clock()
        self._con_mineros(store, 500, equipo=True, gpu=True)
        challenges = capture_challenges(bus)
        make_nct(bus, store, clock)          # dynamic_difficulty=False
        bus.publish_proposal({"law_id": "L1", "author_pubkey": "A",
                              "text_hash": "h1", "created_at": "t0"})
        assert challenges[0]["n_zeros_required"] == 2   # el n_zeros del helper

    def test_si_falla_la_medicion_sigue_con_el_n_anterior(self, bus, store):
        """No abrir la ventana porque no se pudo medir sería peor."""
        clock = Clock()
        challenges = capture_challenges(bus)
        nct = self._nct(bus, store, clock)
        nct.store = type("Roto", (), {
            "live_workers": lambda self: (_ for _ in ()).throw(RuntimeError("redis")),
        })()
        assert nct._n_zeros_para_esta_ventana() == nct.n_zeros


class TestTrinqueteConFailover:
    """El trinquete persistido, visto desde el NCT: un sucesor lo hereda."""

    def _pob(self, store, cuantos, equipo=True):
        import json
        for i in range(cuantos):
            store.r.set(f"worker:status:w{i}", json.dumps(
                {"worker_id": f"w{i}", "has_gpu": False, "capacity": 1}))
        if equipo and cuantos > 1:
            store.r.sadd("teams", "t")
            store.r.hset("team:t", mapping={"coordinator_worker_id": "w0"})
            for i in range(1, cuantos):
                store.r.sadd("team:members:t", f"w{i}")

    def _nct(self, bus, store, clock):
        return make_nct(bus, store, clock, dynamic_difficulty=True,
                        difficulty_target_seconds=30.0,
                        difficulty_decay_windows=3,
                        hps_cpu=1_000_000.0, hps_gpu=1e8)

    def test_el_estado_queda_en_redis(self, bus, store):
        self._pob(store, 40)
        nct = self._nct(bus, store, Clock())
        n = nct._n_zeros_para_esta_ventana()
        assert store.get_difficulty_state().get("current") == str(n)

    def test_un_nct_nuevo_hereda_la_dificultad_alta(self, bus, store):
        """El ataque que esto cierra: apagar cómputo y forzar un failover."""
        self._pob(store, 40)
        primero = self._nct(bus, store, Clock())
        alto = primero._n_zeros_para_esta_ventana()

        # Se apaga toda la población y cae el NCT.
        store.r.flushdb()
        self._pob(store, 1, equipo=False)
        store.save_difficulty_state({"current": alto, "low_streak": 0})

        sucesor = self._nct(bus, store, Clock())
        # Arranca de cero en memoria, pero lee el estado: no adopta el valor bajo.
        assert sucesor._n_zeros_para_esta_ventana() == alto

    def test_la_racha_continua_tras_el_failover(self, bus, store):
        self._pob(store, 1, equipo=False)
        store.save_difficulty_state({"current": 8, "low_streak": 2})
        nct = self._nct(bus, store, Clock())
        # Tercera medición baja consecutiva (2 heredadas + ésta) ⇒ baja un cero.
        assert nct._n_zeros_para_esta_ventana() == 7
