"""Deliberación (AGENT.md 3.12): la ley se anuncia, los convocados deciden, y después.

Lo que se fija acá es el contrato de la pausa:

- durante la pausa no hay ventana ni desafío, sólo la ley y su área;
- la dificultad se congela al anunciar, sobre el convocado más grande del área,
  y no se mueve aunque ese convocado después se baje;
- al terminar: con algún "sí" abre para los que aceptaron, sin ningún "sí" pero
  con algún "no" se descarta, y si nadie respondió vuelve a la cola.
"""

import json
import math

import pytest

from common.blockchain import n_zeros_for_action
from common.blockchain.difficulty import difficulty_for
from common.storage import LawStatus, WindowResult
from nct.coordinator import NCTCoordinator

PAUSA = 120


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_nct(bus, store, clock, **kw):
    params = dict(n_zeros=2, window_seconds_promulgacion=60,
                  window_seconds_derogacion=90, cooldown_new=0,
                  cooldown_reproposed=0, clock=clock, min_workers_for_window=1,
                  deliberation_seconds=PAUSA)
    params.update(kw)
    nct = NCTCoordinator(bus, store, **params)
    nct.wire()
    return nct


def vivo(store, worker_id, mode="standalone", **extra):
    store.r.set(f"worker:status:{worker_id}",
                json.dumps({"worker_id": worker_id, "mode": mode, **extra}), ex=15)


def con_equipo(store, team_id, coordinador, miembros=(), categories="",
               policy=None, owner="fundador"):
    store.r.sadd("teams", team_id)
    store.r.hset(f"team:{team_id}", mapping={
        "coordinator_worker_id": coordinador, "categories": categories,
        "name": team_id, "owner": owner})
    for m in miembros:
        store.r.sadd(f"team:members:{team_id}", m)
    if policy is not None:
        store.r.set(f"pool:policy:{coordinador}", json.dumps(policy))


def propose(bus, law_id="L1", author="A", category="general"):
    bus.publish_proposal({"law_id": law_id, "author_pubkey": author,
                          "text_hash": f"h-{law_id}", "created_at": "t0",
                          "category": category})


def capture(bus):
    challenges = []
    bus.on_challenge(challenges.append)
    return challenges


def responder(store, law_id, voter, decision):
    """Lo que hace el API cuando un convocado responde."""
    store.set_deliberation_decision(law_id, voter, decision)


@pytest.fixture
def red(bus, store):
    """Dos standalone convocables y un NCT con deliberación."""
    clock = Clock()
    challenges = capture(bus)
    vivo(store, "s1")
    vivo(store, "s2")
    nct = make_nct(bus, store, clock)
    return nct, clock, challenges


class TestElAnuncio:
    def test_la_ley_no_abre_ventana_hasta_que_termine_la_pausa(self, bus, store, red):
        nct, clock, challenges = red
        propose(bus)

        assert challenges == []
        assert store.get_active_window() is None
        assert store.get_law("L1")["status"] == LawStatus.IN_DELIBERATION
        assert store.queued_law_ids() == []

    def test_se_anuncia_solo_la_ley_y_su_area(self, bus, store, red):
        """Nada con qué empezar a minar: ni id de ventana ni desafío."""
        propose(bus, category="salud")
        anuncio = store.get_deliberation()

        assert anuncio["law_id"] == "L1" and anuncio["category"] == "salud"
        assert {c["voter_id"] for c in anuncio["convocados"]} == {"s1", "s2"}
        crudo = json.dumps(anuncio)
        assert "voting_window_id" not in crudo
        assert "partial_hash_base" not in crudo
        assert "h-L1" not in crudo

    def test_una_sola_cosa_a_la_vez(self, bus, store, red):
        nct, clock, challenges = red
        propose(bus, "L1", author="A")
        propose(bus, "L2", author="B")

        assert store.get_deliberation()["law_id"] == "L1"
        assert store.get_law("L2")["status"] == LawStatus.PENDING_QUEUE


class TestElResultado:
    def test_con_algun_si_abre_solo_para_los_que_aceptaron(self, bus, store, red):
        nct, clock, challenges = red
        propose(bus)
        responder(store, "L1", "s1", "accept")
        responder(store, "L1", "s2", "reject")
        nct.tick()

        assert len(challenges) == 1
        assert challenges[0]["participants"] == ["s1"]
        assert store.get_law("L1")["status"] == LawStatus.IN_WINDOW
        ventana = store.get_window(challenges[0]["voting_window_id"])
        assert ventana["participants"] == ["s1"]
        assert store.get_deliberation() is None

    def test_si_faltan_respuestas_espera_al_final_de_la_pausa(self, bus, store, red):
        nct, clock, challenges = red
        propose(bus)
        responder(store, "L1", "s1", "accept")

        clock.t += PAUSA - 1
        nct.tick()
        assert challenges == []

        clock.t += 1
        nct.tick()
        assert [c["participants"] for c in challenges] == [["s1"]]

    def test_todos_vetan_se_descarta_sin_esperar(self, bus, store, red):
        nct, clock, challenges = red
        propose(bus)
        responder(store, "L1", "s1", "reject")
        responder(store, "L1", "s2", "reject")
        nct.tick()

        assert challenges == []
        assert store.get_law("L1")["status"] == LawStatus.DISCARDED
        assert store.is_text_hash_discarded("h-L1")
        assert store.get_deliberation_result("L1")["outcome"] == "discard"

    def test_veto_y_silencio_se_descarta(self, bus, store, red):
        nct, clock, challenges = red
        propose(bus)
        responder(store, "L1", "s1", "reject")
        clock.t += PAUSA
        nct.tick()

        assert store.get_law("L1")["status"] == LawStatus.DISCARDED
        resultado = store.get_deliberation_result("L1")
        assert resultado["rejected"] == ["s1"] and resultado["silent"] == ["s2"]

    def test_nadie_responde_vuelve_a_la_cola_y_se_vuelve_a_anunciar(self, bus, store, red):
        nct, clock, challenges = red
        propose(bus)
        clock.t += PAUSA
        nct.tick()

        assert challenges == []
        assert store.get_deliberation_result("L1")["outcome"] == "requeue"
        assert not store.is_text_hash_discarded("h-L1")
        # Es la única ley: le vuelve a tocar y se anuncia de nuevo.
        assert store.get_law("L1")["status"] == LawStatus.IN_DELIBERATION

    def test_la_siguiente_ley_se_anuncia_al_descartar(self, bus, store, red):
        nct, clock, challenges = red
        propose(bus, "L1", author="A")
        propose(bus, "L2", author="B")
        responder(store, "L1", "s1", "reject")
        responder(store, "L1", "s2", "reject")
        nct.tick()

        assert store.get_deliberation()["law_id"] == "L2"

    def test_el_veto_cargado_de_antemano_es_la_respuesta(self, bus, store):
        clock = Clock()
        challenges = capture(bus)
        vivo(store, "c", "pool-coordinator")
        con_equipo(store, "t", "c", policy={"decision": "reject", "law_id": "L1"})
        nct = make_nct(bus, store, clock)

        propose(bus)
        # Antes, con este veto la ley se quedaba esperando para siempre.
        assert store.get_law("L1")["status"] == LawStatus.IN_DELIBERATION
        nct.tick()
        assert store.get_law("L1")["status"] == LawStatus.DISCARDED
        assert challenges == []


class TestLaVentanaQueSale:
    def test_el_id_de_ventana_no_se_puede_anticipar(self, bus, store, red):
        """El contador es público: sin la parte aleatoria se podría pre-minar."""
        nct, clock, challenges = red
        propose(bus)
        responder(store, "L1", "s1", "accept")
        responder(store, "L1", "s2", "accept")
        nct.tick()

        wid = challenges[0]["voting_window_id"]
        assert wid.startswith("W1-L1-") and len(wid) > len("W1-L1-")

    def test_si_los_que_aceptaron_no_llegan_la_ley_cae(self, bus, store, red):
        nct, clock, challenges = red
        propose(bus)
        responder(store, "L1", "s1", "accept")
        clock.t += PAUSA
        nct.tick()
        clock.t += 61
        nct.tick()

        wid = challenges[0]["voting_window_id"]
        assert store.get_window(wid)["result"] == WindowResult.EXPIRED_PENDING
        assert store.get_law("L1")["status"] == LawStatus.DISCARDED

    def test_el_turno_se_consume_al_anunciar(self, bus, store, red):
        nct, clock, challenges = red
        propose(bus, author="A")
        assert store.get_last_author() == "A"
        assert store.recent_window_authors(10) == ["A"]
        responder(store, "L1", "s1", "accept")
        nct.tick()
        # Abrir la ventana no vuelve a contar el turno.
        assert store.recent_window_authors(10) == ["A"]


class TestDificultadCongelada:
    HPS = 1000.0

    def _nct(self, bus, store, clock, **kw):
        return make_nct(bus, store, clock, dynamic_difficulty=True,
                        difficulty_target_seconds=60, hps_cpu=self.HPS,
                        window_deadline_factor=2, window_min_seconds=1,
                        window_seconds_promulgacion=10_000, **kw)

    def test_se_calcula_sobre_el_mas_grande_que_vota_el_area(self, bus, store):
        """El gigante de economía no encarece una ley de salud."""
        clock = Clock()
        vivo(store, "eco", "pool-coordinator", hashrate_hps=5_000_000)
        vivo(store, "sal", "pool-coordinator", hashrate_hps=20_000)
        vivo(store, "s", hashrate_hps=1_000)
        con_equipo(store, "t-eco", "eco", categories="economia")
        con_equipo(store, "t-sal", "sal", categories="salud")
        self._nct(bus, store, clock)

        propose(bus, category="salud")

        anuncio = store.get_deliberation()
        assert anuncio["n_zeros_required"] == difficulty_for(20_000, target_seconds=60)
        assert anuncio["biggest_hashrate"] == 20_000
        assert [c["voter_id"] for c in anuncio["convocados"]] == ["sal", "s"]

    def test_si_el_grande_se_baja_la_ley_sale_con_su_dificultad(self, bus, store):
        clock = Clock()
        challenges = capture(bus)
        vivo(store, "grande", "pool-coordinator", hashrate_hps=5_000_000)
        vivo(store, "chico", hashrate_hps=1_000)
        con_equipo(store, "t", "grande", categories="economia")
        nct = self._nct(bus, store, clock)

        propose(bus, category="economia")
        n_grande = difficulty_for(5_000_000, target_seconds=60)
        responder(store, "L1", "grande", "reject")
        responder(store, "L1", "chico", "accept")
        # Aunque el grande ya no esté, la ventana sale con lo congelado.
        store.r.delete("worker:status:grande")
        nct._poblacion_cache = None
        nct.tick()

        assert challenges[0]["participants"] == ["chico"]
        assert challenges[0]["n_zeros_required"] == n_zeros_for_action(n_grande, "promulgacion")
        # Plazo: 2 × lo que tarda el grande en promedio, no el chico.
        plazo = math.ceil(2 * 16 ** n_grande / 5_000_000)
        assert nct._active["deadline_epoch"] == clock.t + plazo

    def test_cada_area_tiene_su_trinquete(self, bus, store):
        """Con uno solo, el `n` del área fuerte quedaría sostenido en las demás."""
        clock = Clock()
        vivo(store, "eco", "pool-coordinator", hashrate_hps=5_000_000)
        vivo(store, "sal", "pool-coordinator", hashrate_hps=20_000)
        con_equipo(store, "t-eco", "eco", categories="economia")
        con_equipo(store, "t-sal", "sal", categories="salud")
        nct = self._nct(bus, store, clock)

        propose(bus, "L1", author="A", category="economia")
        responder(store, "L1", "eco", "reject")
        nct.tick()
        propose(bus, "L2", author="B", category="salud")

        assert store.get_deliberation()["n_zeros_required"] == \
            difficulty_for(20_000, target_seconds=60)
        assert store.get_difficulty_state("economia")["current"] == \
            str(difficulty_for(5_000_000, target_seconds=60))


class TestFailover:
    def test_la_ley_en_deliberacion_vuelve_a_la_cola_con_el_nuevo_lider(self, bus, store, red):
        nct, clock, challenges = red
        propose(bus)
        nct.step_down()

        sucesor = make_nct(bus, store, clock, is_leader=False)
        sucesor.become_leader()

        # Vuelve a la cola y, como hay red, el sucesor la anuncia de nuevo.
        assert store.get_law("L1")["status"] == LawStatus.IN_DELIBERATION
        assert store.get_deliberation()["started_at"]
        assert store.deliberation_decisions("L1") == {}


class TestRespuestaPorDefecto:
    def test_si_no_responde_cuenta_la_por_defecto_al_final_de_la_pausa(self, bus, store):
        clock = Clock()
        challenges = capture(bus)
        vivo(store, "c", "pool-coordinator")
        vivo(store, "s")
        con_equipo(store, "t", "c")
        store.r.hset("team:t", "default_decision", "accept")
        nct = make_nct(bus, store, clock)

        propose(bus)
        nct.tick()
        assert challenges == []  # la por defecto no adelanta el cierre

        clock.t += PAUSA
        nct.tick()
        assert challenges[0]["participants"] == ["c"]
        assert store.get_deliberation_result("L1")["defaulted"] == ["c"]

    def test_la_respuesta_de_la_pausa_gana(self, bus, store):
        clock = Clock()
        challenges = capture(bus)
        vivo(store, "s", default_decision="accept")
        nct = make_nct(bus, store, clock)

        propose(bus)
        responder(store, "L1", "s", "reject")
        nct.tick()

        assert challenges == []
        assert store.get_law("L1")["status"] == LawStatus.DISCARDED


class TestRondasEnSilencio:
    def _pausa_en_silencio(self, nct, clock):
        clock.t += PAUSA
        nct.tick()

    def test_a_la_tercera_se_descarta_sin_penalidad(self, bus, store, red):
        nct, clock, challenges = red
        propose(bus)
        for _ in range(2):
            self._pausa_en_silencio(nct, clock)
            assert store.get_law("L1")["status"] == LawStatus.IN_DELIBERATION

        self._pausa_en_silencio(nct, clock)

        assert store.get_law("L1")["status"] == LawStatus.DISCARDED
        assert store.get_deliberation_result("L1")["outcome"] == "unanswered"
        # Nadie la juzgó: reproponerla no paga el cooldown largo.
        assert not store.is_text_hash_discarded("h-L1")
        assert store.get_deliberation() is None

    def test_una_ventana_abierta_reinicia_la_cuenta(self, bus, store, red):
        nct, clock, challenges = red
        propose(bus)
        self._pausa_en_silencio(nct, clock)
        self._pausa_en_silencio(nct, clock)
        assert store.silent_deliberations("L1") == 2

        responder(store, "L1", "s1", "accept")
        self._pausa_en_silencio(nct, clock)
        assert store.silent_deliberations("L1") == 0

    def test_el_tope_es_configurable(self, bus, store):
        clock = Clock()
        vivo(store, "s")
        nct = make_nct(bus, store, clock, max_silent_deliberations=1)

        propose(bus)
        clock.t += PAUSA
        nct.tick()

        assert store.get_deliberation_result("L1")["outcome"] == "unanswered"
