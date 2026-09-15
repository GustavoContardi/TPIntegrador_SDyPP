"""El NCT no somete una ley al desafío si no hay red que pueda resolverla.

Sin este gate el sistema fallaba de la peor manera posible: en silencio. La
ventana se abría con la red vacía, vencía, la ley quedaba ``discarded`` y su
texto anotado como descartado —así que reproponerla costaba el cooldown largo de
reproposición idéntica (AGENT.md 3.5)—, y en los logs se veía igual que una ley
que nadie quiso minar. Acá se fija el comportamiento nuevo: la ley **espera**.
"""

import hashlib
import json

import pytest

from common.storage import LawStatus, WindowResult
from nct.coordinator import NCTCoordinator


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_nct(bus, store, clock, *, min_workers=1, **kw):
    params = dict(n_zeros=2, window_seconds_promulgacion=60,
                  window_seconds_derogacion=90, cooldown_new=2,
                  cooldown_reproposed=4, clock=clock,
                  min_workers_for_window=min_workers)
    params.update(kw)
    nct = NCTCoordinator(bus, store, **params)
    nct.wire()
    return nct


def vivo(store, worker_id, **extra):
    """Un minero latiendo: es lo que el NCT lee para saber si hay red."""
    store.r.set(f"worker:status:{worker_id}",
                json.dumps({"worker_id": worker_id, **extra}), ex=15)


def con_equipo(store, team_id, coordinador, miembros=(), categories="",
               policy=None):
    store.r.sadd("teams", team_id)
    store.r.hset(f"team:{team_id}", mapping={
        "coordinator_worker_id": coordinador, "categories": categories})
    for m in miembros:
        store.r.sadd(f"team:members:{team_id}", m)
    if policy is not None:
        # `pool:policy:<coordinador>` es lo que escribe el API y relee el propio
        # coordinador del pool: lleva la agenda y además los vetos de acción.
        store.r.set(f"pool:policy:{coordinador}", json.dumps(policy))


def solve(base, n_zeros):
    prefix = "0" * n_zeros
    nonce = 0
    while not hashlib.md5(f"{base}{nonce}".encode()).hexdigest().startswith(prefix):
        nonce += 1
    return nonce


def promulgar(bus, challenges, law_id, category="general", author="A"):
    """Deja una ley promulgada, que es el precondición para poder derogarla."""
    propose(bus, law_id=law_id, author=author, category=category)
    ch = challenges[-1]
    bus.publish_nonce_response({
        "voting_window_id": ch["voting_window_id"],
        "nonce": solve(ch["partial_hash_base"], ch["n_zeros_required"]),
        "winning_node_or_pool": "alguien"})


def capture_challenges(bus):
    challenges = []
    bus.on_challenge(challenges.append)
    return challenges


def propose(bus, law_id="L1", author="A", category="general", **kw):
    bus.publish_proposal({"law_id": law_id, "author_pubkey": author,
                          "text_hash": f"h-{law_id}", "created_at": "t0",
                          "category": category, **kw})


class TestSinRedLaLeyEspera:
    def test_sin_mineros_no_se_abre_ventana_y_la_ley_queda_encolada(self, bus, store):
        challenges = capture_challenges(bus)
        make_nct(bus, store, Clock())

        propose(bus)

        assert challenges == []
        assert store.get_active_window() is None
        assert store.get_law("L1")["status"] == LawStatus.PENDING_QUEUE
        assert store.queued_law_ids() == ["L1"]

    def test_con_un_minero_vivo_se_abre_normal(self, bus, store):
        challenges = capture_challenges(bus)
        make_nct(bus, store, Clock())
        vivo(store, "m1")

        propose(bus)

        assert len(challenges) == 1
        assert store.get_law("L1")["status"] == LawStatus.IN_WINDOW

    def test_mineros_que_no_minan_el_area_no_son_red(self, bus, store):
        """Dos mineros prendidos y la ley igual espera: ninguno mina 'salud'."""
        challenges = capture_challenges(bus)
        make_nct(bus, store, Clock())
        vivo(store, "coord")
        vivo(store, "m1")
        con_equipo(store, "t1", "coord", ["m1"], categories="economia")

        propose(bus, category="salud")

        assert challenges == []
        assert store.get_law("L1")["status"] == LawStatus.PENDING_QUEUE

        # El mismo equipo sí atiende su propia área.
        propose(bus, law_id="L2", author="B", category="economia")
        assert len(challenges) == 1
        assert challenges[0]["category"] == "economia"


    def test_la_ventana_se_abre_sola_cuando_vuelve_la_red(self, bus, store):
        """Nadie tiene que reproponer: el tick del NCT reanuda la cola."""
        challenges = capture_challenges(bus)
        clock = Clock()
        nct = make_nct(bus, store, clock)

        propose(bus)
        assert challenges == []

        vivo(store, "m1")
        clock.t += 10  # vence el cache del quórum
        nct.tick()

        assert len(challenges) == 1
        assert store.get_law("L1")["status"] == LawStatus.IN_WINDOW

    def test_un_minimo_mas_alto_exige_mas_mineros(self, bus, store):
        challenges = capture_challenges(bus)
        clock = Clock()
        nct = make_nct(bus, store, clock, min_workers=3)
        vivo(store, "m1")
        vivo(store, "m2")

        propose(bus)
        assert challenges == []

        vivo(store, "m3")
        clock.t += 10
        nct.tick()
        assert len(challenges) == 1

    def test_con_el_gate_desactivado_se_abre_sin_mineros(self, bus, store):
        """El comportamiento previo sigue disponible por config (min_workers=0)."""
        challenges = capture_challenges(bus)
        make_nct(bus, store, Clock(), min_workers=0)

        propose(bus)

        assert len(challenges) == 1

class TestLaAbstencionSigueSiendoUnaOpcion:
    """Con `quorum_by_category=False` un área desierta vuelve a expirar.

    AGENT.md 3.10 define la abstención como mecanismo: si ningún equipo vota un
    área, su ley expira, y eso es el resultado político buscado. Medir el quórum
    por área lo convierte en una espera indefinida, que es otra cosa — por eso la
    perilla existe y por eso este test la fija.
    """

    def test_la_ley_de_un_area_sin_equipos_se_abre_igual(self, bus, store):
        challenges = capture_challenges(bus)
        make_nct(bus, store, Clock(), quorum_by_category=False)
        vivo(store, "coord")
        con_equipo(store, "t1", "coord", categories="economia")

        propose(bus, category="salud")

        assert len(challenges) == 1  # se abre; vencerá por abstención

    def test_pero_la_red_vacia_sigue_posponiendo(self, bus, store):
        challenges = capture_challenges(bus)
        make_nct(bus, store, Clock(), quorum_by_category=False)

        propose(bus, category="salud")

        assert challenges == []
        assert store.get_law("L1")["status"] == LawStatus.PENDING_QUEUE


class TestUnaLeyPostergadaNoBloqueaLaCola:
    """Postergar una ley no puede congelar el parlamento.

    Es el mismo principio que ya protege a la cuota de turnos: un mecanismo
    defensivo capaz de dejar al sistema sin abrir ventanas sería una denegación
    de servicio más barata que el ataque que intenta evitar.
    """

    def test_se_saltea_la_que_no_tiene_red_y_sigue_con_la_siguiente(
            self, bus, store):
        challenges = capture_challenges(bus)
        make_nct(bus, store, Clock())
        vivo(store, "coord")
        con_equipo(store, "t1", "coord", categories="economia")

        propose(bus, law_id="L1", author="A", category="salud")     # sin red
        propose(bus, law_id="L2", author="B", category="economia")  # con red

        assert [c["law_id"] for c in challenges] == ["L2"]
        # La salteada no se perdió: sigue esperando su turno en la cola.
        assert store.queued_law_ids() == ["L1"]
        assert store.get_law("L1")["status"] == LawStatus.PENDING_QUEUE

    def test_la_salteada_se_abre_cuando_aparece_su_equipo(self, bus, store):
        challenges = capture_challenges(bus)
        clock = Clock()
        nct = make_nct(bus, store, clock)
        vivo(store, "coord")
        con_equipo(store, "t1", "coord", categories="economia")
        propose(bus, law_id="L1", author="A", category="salud")

        vivo(store, "sanitario")  # standalone sin agenda: mina cualquier área
        clock.t += 10
        nct.tick()

        assert [c["law_id"] for c in challenges] == ["L1"]


class TestDerogarSeMideIgualQuePromulgar:
    """El quórum se evalúa sobre el ÁREA de la ley, también al derogar.

    Una derogación conserva la categoría de la ley original (AGENT.md 3.10), así
    que convoca a los mismos equipos. Pero votar el área no alcanza: un equipo
    puede vetar todas las derogaciones, y entonces para esa ventana no es red.
    """

    def test_la_derogacion_usa_el_area_de_la_ley_original(self, bus, store):
        challenges = capture_challenges(bus)
        clock = Clock()
        nct = make_nct(bus, store, clock)
        vivo(store, "libre")  # standalone sin agenda: promulga cualquier cosa
        promulgar(bus, challenges, "L1", category="salud")
        assert store.get_law("L1")["status"] == LawStatus.PROMULGATED

        # Ahora sólo queda un equipo que vota 'economia': nadie mina salud.
        store.r.delete("worker:status:libre")
        vivo(store, "coord")
        con_equipo(store, "t1", "coord", categories="economia")
        clock.t += 10

        propose(bus, law_id="L1", author="B", action="derogacion")

        assert len(challenges) == 1  # sólo la promulgación; la derogación espera
        # La ley encolada para derogar conserva `promulgated`: sigue vigente,
        # justamente porque su derogación todavía no se pudo someter a ventana.
        assert store.queued_law_ids() == ["L1"]
        assert store.get_law("L1")["action"] == "derogacion"
        assert store.get_availability_state()["category"] == "salud"

    def test_un_equipo_que_veta_derogaciones_no_es_red_para_derogar(self, bus, store):
        """Vota 'salud' y promulga, pero rechaza toda derogación: no cuenta."""
        challenges = capture_challenges(bus)
        clock = Clock()
        nct = make_nct(bus, store, clock)
        vivo(store, "coord")
        con_equipo(store, "t1", "coord", categories="salud", policy={
            "categories": ["salud"], "decision": "reject",
            "action": "derogacion"})

        promulgar(bus, challenges, "L1", category="salud")
        assert store.get_law("L1")["status"] == LawStatus.PROMULGATED

        clock.t += 10
        propose(bus, law_id="L1", author="B", action="derogacion")

        assert len(challenges) == 1  # la derogación no abrió ventana
        assert store.queued_law_ids() == ["L1"]
        assert "derogacion" in store.get_availability_state()["reason"]

    def test_y_se_abre_cuando_aparece_alguien_dispuesto_a_derogar(self, bus, store):
        challenges = capture_challenges(bus)
        clock = Clock()
        nct = make_nct(bus, store, clock)
        vivo(store, "coord")
        con_equipo(store, "t1", "coord", categories="salud", policy={
            "categories": ["salud"], "decision": "reject",
            "action": "derogacion"})
        promulgar(bus, challenges, "L1", category="salud")
        clock.t += 10
        propose(bus, law_id="L1", author="B", action="derogacion")
        assert len(challenges) == 1

        vivo(store, "opositor")  # standalone sin agenda: deroga lo que sea
        clock.t += 10
        nct.tick()

        assert len(challenges) == 2
        assert challenges[1]["action"] == "derogacion"
        assert challenges[1]["category"] == "salud"

    def test_un_standalone_que_rechaza_derogaciones_tampoco_cuenta(self, bus, store):
        challenges = capture_challenges(bus)
        clock = Clock()
        nct = make_nct(bus, store, clock)
        vivo(store, "libre")
        promulgar(bus, challenges, "L1", category="salud")

        store.r.delete("worker:status:libre")
        vivo(store, "pacifista", rejected_actions=["derogacion"])
        clock.t += 10
        propose(bus, law_id="L1", author="B", action="derogacion")

        assert len(challenges) == 1
        assert store.queued_law_ids() == ["L1"]


class TestVencerSinRedNoDescartaLaLey:
    def test_si_los_mineros_se_caen_durante_la_ventana_la_ley_vuelve_a_la_cola(
            self, bus, store):
        clock = Clock()
        nct = make_nct(bus, store, clock)
        vivo(store, "m1")
        propose(bus)
        assert store.get_law("L1")["status"] == LawStatus.IN_WINDOW

        store.r.delete("worker:status:m1")   # se apagó el único minero
        clock.t += 120                       # vence la ventana
        nct.tick()

        ley = store.get_law("L1")
        assert ley["status"] == LawStatus.PENDING_QUEUE
        assert store.queued_law_ids() == ["L1"]
        # Reproponerla no puede costar el cooldown de reproposición idéntica:
        # la ley nunca llegó a ser juzgada.
        assert store.is_text_hash_discarded("h-L1") is False

    def test_queda_registrado_por_que_venció(self, bus, store):
        clock = Clock()
        nct = make_nct(bus, store, clock)
        vivo(store, "m1")
        challenges = capture_challenges(bus)
        propose(bus)
        wid = challenges[0]["voting_window_id"]

        store.r.delete("worker:status:m1")
        clock.t += 120
        nct.tick()

        assert store.get_window(wid)["result"] == WindowResult.EXPIRED_NO_QUORUM

    def test_con_red_viva_el_vencimiento_descarta_como_siempre(self, bus, store):
        """Si había mineros y aun así venció, la regla 3.2/3.4 no cambia."""
        clock = Clock()
        nct = make_nct(bus, store, clock)
        vivo(store, "m1")
        propose(bus)

        clock.t += 120
        nct.tick()

        assert store.get_law("L1")["status"] == LawStatus.DISCARDED
        assert store.is_text_hash_discarded("h-L1") is True

    def test_la_ley_reencolada_no_se_reabre_mientras_no_haya_red(self, bus, store):
        """No hay bucle: sin quórum vuelve a la cola y ahí se queda."""
        clock = Clock()
        nct = make_nct(bus, store, clock)
        vivo(store, "m1")
        challenges = capture_challenges(bus)
        propose(bus)

        store.r.delete("worker:status:m1")
        clock.t += 120
        nct.tick()
        clock.t += 10
        nct.tick()

        assert len(challenges) == 1  # no se volvió a publicar el desafío
        assert store.queued_law_ids() == ["L1"]


class TestElSistemaDiceQueLePasa:
    def test_publica_la_indisponibilidad_para_que_el_api_la_lea(self, bus, store):
        make_nct(bus, store, Clock())

        propose(bus, category="salud")

        estado = store.get_availability_state()
        assert estado["available"] == "0"
        assert estado["category"] == "salud"
        assert estado["live_workers"] == "0"
        assert estado["queued_laws"] == "1"
        assert estado["since"]
        assert "no hay mineros vivos" in estado["reason"]

    def test_conserva_desde_cuando_esta_caido(self, bus, store):
        """`since` es el momento en que empezó, no el del último chequeo."""
        clock = Clock()
        nct = make_nct(bus, store, clock)
        propose(bus)
        primero = store.get_availability_state()["since"]

        clock.t += 60
        propose(bus, law_id="L2", author="B")  # cambia la cola, sigue sin red
        nct.tick()

        assert store.get_availability_state()["since"] == primero
        assert store.get_availability_state()["queued_laws"] == "2"

    def test_al_volver_la_red_se_marca_disponible(self, bus, store):
        clock = Clock()
        nct = make_nct(bus, store, clock)
        propose(bus)
        assert store.get_availability_state()["available"] == "0"

        vivo(store, "m1")
        clock.t += 10
        nct.tick()

        estado = store.get_availability_state()
        assert estado["available"] == "1"
        assert estado["since"] == ""

    def test_no_publica_nada_con_el_gate_desactivado(self, bus, store):
        make_nct(bus, store, Clock(), min_workers=0)
        propose(bus)
        assert store.get_availability_state() == {}


class TestFallasDeMedicionNoParalizanElGobierno:
    def test_si_no_se_puede_medir_se_abre_igual(self, bus, store, monkeypatch):
        """Un Redis que no responde no puede volverse una denegación de servicio.

        Es la misma decisión que toma la dificultad dinámica: ante una falla de
        observación se sigue con el gobierno andando, no se lo detiene.
        """
        challenges = capture_challenges(bus)
        make_nct(bus, store, Clock())

        def explota():
            raise RuntimeError("redis caído")

        monkeypatch.setattr(store, "live_workers", explota)

        propose(bus)

        assert len(challenges) == 1


@pytest.mark.parametrize("categoria", ["salud", "economia", "general"])
def test_el_quorum_se_evalua_contra_el_area_de_la_ley(bus, store, categoria):
    """Un equipo que vota sólo un área habilita esa y ninguna otra."""
    challenges = capture_challenges(bus)
    make_nct(bus, store, Clock())
    vivo(store, "coord")
    con_equipo(store, "t1", "coord", categories="salud")

    propose(bus, law_id=f"L-{categoria}", category=categoria)

    assert len(challenges) == (1 if categoria == "salud" else 0)
