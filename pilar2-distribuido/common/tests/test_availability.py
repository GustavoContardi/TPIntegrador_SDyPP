"""Quórum de mineros: qué población cuenta como red disponible para una ley."""

import pytest

from common.blockchain.availability import (
    Availability,
    assess,
    eligible_workers,
    policy_accepts,
)


def w(worker_id, **extra):
    return {"worker_id": worker_id, **extra}


def equipo(team_id, coordinador, miembros=(), categories=None, policy=None):
    return {"team_id": team_id, "coordinator_worker_id": coordinador,
            "members": list(miembros), "categories": categories,
            "policy": policy}


def ventana(category, action="promulgacion", law_id="L1"):
    return {"category": category, "action": action, "law_id": law_id}


def elegibles(workers, teams, category, action="promulgacion", law_id="L1"):
    return [e["worker_id"]
            for e in eligible_workers(workers, teams,
                                      ventana(category, action, law_id))]


class TestQuienCuentaComoRedDisponible:
    def test_un_standalone_sin_agenda_mina_cualquier_area(self):
        assert elegibles([w("m1")], [], "salud") == ["m1"]

    def test_un_standalone_con_agenda_solo_cuenta_para_su_area(self):
        minero = w("m1", categories=["economia"])
        assert elegibles([minero], [], "economia") == ["m1"]
        assert elegibles([minero], [], "salud") == []

    def test_un_equipo_sin_agenda_cuenta_para_todo(self):
        workers = [w("coord"), w("m1")]
        teams = [equipo("t1", "coord", ["m1"])]
        assert len(elegibles(workers, teams, "ambiente")) == 2

    def test_un_equipo_arrastra_a_sus_miembros_fuera_de_su_agenda(self):
        """La decisión de minar la toma el coordinador, no cada miembro.

        Es la propiedad que hace que contar mineros vivos a secas mienta: los
        dos están prendidos y ninguno va a poner un hash en esta ventana.
        """
        workers = [w("coord"), w("m1")]
        teams = [equipo("t1", "coord", ["m1"], categories=["economia"])]
        assert elegibles(workers, teams, "salud") == []
        assert len(elegibles(workers, teams, "economia")) == 2

    def test_la_agenda_del_equipo_pisa_la_del_minero(self):
        """Un miembro de pool no filtra por su cuenta: fragmenta lo que le dan."""
        workers = [w("m1", categories=["salud"])]
        teams = [equipo("t1", "coord-caido", ["m1"], categories=["economia"])]
        assert elegibles(workers, teams, "salud") == []
        assert len(elegibles(workers, teams, "economia")) == 1

    def test_un_miembro_muerto_no_cuenta(self):
        teams = [equipo("t1", "coord", ["m1", "m2"])]
        assert len(elegibles([w("coord")], teams, "general")) == 1

    def test_un_area_desconocida_se_lee_como_general(self):
        minero = w("m1", categories=["general"])
        assert len(elegibles([minero], [], "area-inventada")) == 1


class TestElVetoDeAccionTambienCuenta:
    """Votar el área no alcanza: el equipo puede rechazar la acción.

    Es el caso que hacía falta cerrar para que el quórum valiera igual al
    derogar que al promulgar. Un equipo de salud que rechaza toda derogación
    contaba como red disponible para derogar una ley de salud, y la ventana se
    abría para vencer — exactamente el modo de falla mudo que el quórum ataca.
    """

    def test_un_equipo_que_veta_derogaciones_no_cuenta_para_derogar(self):
        workers = [w("coord"), w("m1")]
        teams = [equipo("t1", "coord", ["m1"], policy={
            "categories": ["salud"], "decision": "reject",
            "action": "derogacion"})]
        assert len(elegibles(workers, teams, "salud", "promulgacion")) == 2
        assert elegibles(workers, teams, "salud", "derogacion") == []

    def test_un_standalone_que_rechaza_derogaciones_tampoco(self):
        minero = w("m1", rejected_actions=["derogacion"])
        assert elegibles([minero], [], "salud", "promulgacion") == ["m1"]
        assert elegibles([minero], [], "salud", "derogacion") == []

    def test_un_veto_a_una_ley_puntual_no_afecta_a_las_demas(self):
        workers = [w("coord")]
        teams = [equipo("t1", "coord", policy={
            "decision": "reject", "law_id": "L-odiada"})]
        assert elegibles(workers, teams, "salud", law_id="L-odiada") == []
        assert elegibles(workers, teams, "salud", law_id="L-otra") == ["coord"]

    def test_la_policy_del_coordinador_pisa_la_agenda_del_hash(self):
        """`pool:policy` es la fuente autoritativa; el hash es sólo el respaldo."""
        workers = [w("coord")]
        teams = [equipo("t1", "coord", categories=["economia"],
                        policy={"categories": ["salud"]})]
        assert elegibles(workers, teams, "salud") == ["coord"]
        assert elegibles(workers, teams, "economia") == []

    def test_sin_policy_se_usa_la_agenda_del_hash(self):
        """Un equipo recién fundado cuya política todavía no bajó a Redis."""
        workers = [w("coord")]
        teams = [equipo("t1", "coord", categories=["salud"], policy=None)]
        assert elegibles(workers, teams, "salud") == ["coord"]
        assert elegibles(workers, teams, "economia") == []


class TestElPredicadoCompartido:
    """`policy_accepts` es la única definición de la regla en todo el sistema.

    La usan el coordinador del pool (para fragmentar) y el NCT (para abrir). Con
    dos copias, la predicción y la conducta se irían separando en silencio.
    """

    def test_sin_politica_mina_todo(self):
        assert policy_accepts({}, ventana("salud", "derogacion")) is True

    def test_accept_ignora_action_y_law_id(self):
        policy = {"decision": "accept", "action": "derogacion"}
        assert policy_accepts(policy, ventana("salud", "derogacion")) is True

    def test_reject_sin_criterio_rechaza_todo(self):
        assert policy_accepts({"decision": "reject"}, ventana("salud")) is False

    def test_la_agenda_manda_sobre_el_decision(self):
        """La agenda es la decisión política; `decision` es el veto puntual."""
        policy = {"categories": ["economia"], "decision": "accept"}
        assert policy_accepts(policy, ventana("salud")) is False


class TestVeredicto:
    def test_sin_mineros_no_hay_quorum(self):
        r = assess([], [], "salud", minimum=1)
        assert r.ok is False
        assert r.live == 0 and r.eligible == 0
        assert "no hay mineros vivos" in r.reason()

    def test_con_mineros_que_no_minan_el_area_tampoco(self):
        workers = [w("coord"), w("m1")]
        teams = [equipo("t1", "coord", ["m1"], categories=["economia"])]
        r = assess(workers, teams, "salud", minimum=1)
        assert r.ok is False
        # El mensaje distingue los dos casos porque piden acciones distintas:
        # levantar mineros vs. sumar un equipo que vote esa área.
        assert r.live == 2 and r.eligible == 0
        assert "salud" in r.reason() and "2 minero" in r.reason()

    def test_la_misma_area_puede_estar_disponible_para_una_accion_y_no_para_otra(self):
        workers = [w("coord")]
        teams = [equipo("t1", "coord", policy={
            "categories": ["salud"], "decision": "reject",
            "action": "derogacion"})]
        assert assess(workers, teams, ventana("salud", "promulgacion"),
                      minimum=1).ok is True
        r = assess(workers, teams, ventana("salud", "derogacion"), minimum=1)
        assert r.ok is False
        # El mensaje nombra la acción: sin eso diría "hay equipos de salud" y la
        # derogación sin abrirse, que es desconcertante.
        assert "derogacion" in r.reason() and "salud" in r.reason()

    def test_alcanza_con_el_minimo_exacto(self):
        assert assess([w("m1"), w("m2")], [], "salud", minimum=2).ok is True
        assert assess([w("m1")], [], "salud", minimum=2).ok is False

    def test_sin_mirar_agendas_el_area_desierta_tiene_quorum(self):
        """`by_category=False` recupera el veto por abstención de AGENT.md 3.10.

        El equipo no mina 'salud' y la ventana va a vencer; lo que cambia es que
        vencer vuelve a ser una decisión política de las facciones y no una
        indisponibilidad del sistema. El gate sigue cubriendo la red vacía, que
        es el caso que no discute nadie.
        """
        workers = [w("coord"), w("m1")]
        teams = [equipo("t1", "coord", ["m1"], categories=["economia"])]
        assert assess(workers, teams, "salud", minimum=1,
                      by_category=False).ok is True
        assert assess([], teams, "salud", minimum=1,
                      by_category=False).ok is False

    def test_minimo_cero_desactiva_el_chequeo(self):
        r = assess([], [], "salud", minimum=0)
        assert r.ok is True
        assert r.reason() == ""

    @pytest.mark.parametrize("ok", [True, False])
    def test_disponible_no_da_explicacion(self, ok):
        r = Availability(ok=ok, category="salud", live=1, eligible=1, required=1)
        assert bool(r.reason()) is not ok
