"""Lógica pura de la deliberación (AGENT.md 3.12): a quién se convoca y qué se decide."""

import pytest

from common.blockchain.availability import assess, policy_convokes, prevetoed
from common.blockchain.deliberation import (
    KIND_STANDALONE,
    KIND_TEAM,
    OUTCOME_DISCARD,
    OUTCOME_OPEN,
    OUTCOME_REQUEUE,
    Convocado,
    all_answered,
    biggest,
    convocados,
    participates,
    resolve,
    window_seconds_for,
)

HPS = dict(hps_cpu=1000.0, hps_gpu=1e6)


def w(wid, mode="standalone", **extra):
    return {"worker_id": wid, "mode": mode, **extra}


def equipo(team_id, coord, miembros=(), categories=None, policy=None, owner="dueño"):
    return {"team_id": team_id, "name": team_id.upper(), "owner": owner,
            "coordinator_worker_id": coord, "members": list(miembros),
            "categories": categories or [], "policy": policy or {}}


class TestConvocados:
    def test_un_equipo_decide_como_uno_y_suma_a_sus_miembros_vivos(self):
        workers = [w("c", "pool-coordinator"), w("m1", "pool-worker"),
                   w("m2", "pool-worker")]
        lista = convocados(workers, [equipo("t", "c", ["m1", "m2", "muerto"])],
                           {"category": "salud"}, **HPS)
        assert [(c.voter_id, c.kind, c.hashrate) for c in lista] == [
            ("c", KIND_TEAM, 3000.0)]
        assert lista[0].name == "T" and lista[0].owner == "dueño"

    def test_la_agenda_decide_quien_es_convocado(self):
        workers = [w("c1", "pool-coordinator"), w("c2", "pool-coordinator")]
        teams = [equipo("eco", "c1", categories=["economia"]),
                 equipo("sal", "c2", categories=["salud"])]
        lista = convocados(workers, teams, {"category": "salud"}, **HPS)
        assert [c.voter_id for c in lista] == ["c2"]

    def test_standalone_con_agenda_propia(self):
        workers = [w("s1", categories=["economia"]), w("s2")]
        lista = convocados(workers, [], {"category": "salud"}, **HPS)
        assert [(c.voter_id, c.kind) for c in lista] == [("s2", KIND_STANDALONE)]

    def test_el_pool_anonimo_no_es_convocado(self):
        """'Si no responde, no vota': el pool de infraestructura no tiene quién responda."""
        workers = [w("auto-1", "pool-auto"), w("auto-2", "pool-auto"), w("s")]
        lista = convocados(workers, [], {"category": "general"}, **HPS)
        assert [c.voter_id for c in lista] == ["s"]

    def test_worker_sin_modo_se_lee_como_standalone(self):
        lista = convocados([{"worker_id": "viejo"}], [], {"category": "general"}, **HPS)
        assert [c.voter_id for c in lista] == ["viejo"]

    def test_quien_rechaza_toda_la_accion_no_es_convocado(self):
        workers = [w("c", "pool-coordinator"), w("s", rejected_actions=["derogacion"])]
        teams = [equipo("t", "c", policy={"decision": "reject", "action": "derogacion"})]
        lista = convocados(workers, teams,
                           {"category": "general", "action": "derogacion"}, **HPS)
        assert lista == []

    def test_el_veto_a_la_ley_puntual_convoca_igual_y_queda_como_respuesta(self):
        """Si el veto puntual excluyera, el grande vetaría para bajar la dificultad."""
        workers = [w("c", "pool-coordinator")]
        teams = [equipo("t", "c", policy={"decision": "reject", "law_id": "L1"})]
        lista = convocados(workers, teams, {"category": "general", "law_id": "L1"}, **HPS)
        assert [(c.voter_id, c.prevetoed) for c in lista] == [("c", True)]
        otra = convocados(workers, teams, {"category": "general", "law_id": "L2"}, **HPS)
        assert [(c.voter_id, c.prevetoed) for c in otra] == [("c", False)]

    def test_ordenados_de_mayor_a_menor(self):
        workers = [w("chico", hashrate_hps=10), w("grande", hashrate_hps=500),
                   w("medio", hashrate_hps=100)]
        lista = convocados(workers, [], {"category": "general"}, **HPS)
        assert [c.voter_id for c in lista] == ["grande", "medio", "chico"]
        assert biggest(lista) == 500

    def test_equipo_sin_miembros_vivos_no_se_convoca(self):
        lista = convocados([], [equipo("t", "c", ["m"])], {"category": "general"}, **HPS)
        assert lista == []

    def test_ida_y_vuelta_por_dict(self):
        c = Convocado("c", KIND_TEAM, "T", "o", 12.5, prevetoed=True, team_id="t")
        assert Convocado.from_dict(c.to_dict()) == c


class TestPoliticas:
    def test_prevetoed_solo_para_la_ley_del_veto(self):
        politica = {"decision": "reject", "law_id": "L1"}
        assert prevetoed(politica, "L1")
        assert not prevetoed(politica, "L2")
        assert not prevetoed({"decision": "reject", "action": "derogacion"}, "L1")
        assert not prevetoed({}, "L1")

    def test_policy_convokes_respeta_la_agenda(self):
        politica = {"categories": ["economia"], "decision": "reject", "law_id": "L1"}
        assert not policy_convokes(politica, {"category": "salud", "law_id": "L1"})
        assert policy_convokes(politica, {"category": "economia", "law_id": "L1"})


class TestQuorumDeConvocatoria:
    def test_un_veto_puntual_no_deja_a_la_ley_esperando(self):
        workers = [w("c", "pool-coordinator")]
        teams = [equipo("t", "c", policy={"decision": "reject", "law_id": "L1"})]
        desafio = {"category": "general", "law_id": "L1"}
        assert not assess(workers, teams, desafio, minimum=1).ok
        assert assess(workers, teams, desafio, minimum=1, convocation=True).ok

    def test_el_pool_anonimo_no_cuenta_como_red(self):
        workers = [w("auto", "pool-auto")]
        assert assess(workers, [], "general", minimum=1).ok
        assert not assess(workers, [], "general", minimum=1, convocation=True).ok


class TestResolver:
    IDS = ["a", "b", "c"]

    def test_algun_si_abre_solo_para_los_que_aceptaron(self):
        assert resolve(self.IDS, {"a": "reject", "b": "accept"}) == (OUTCOME_OPEN, ["b"])

    def test_todos_vetan_se_descarta(self):
        assert resolve(self.IDS, dict.fromkeys(self.IDS, "reject")) == (OUTCOME_DISCARD, [])

    def test_veto_y_silencio_se_descarta(self):
        """Quien no responde no vota: el único voto emitido fue en contra."""
        assert resolve(self.IDS, {"a": "reject"}) == (OUTCOME_DISCARD, [])

    def test_nadie_responde_vuelve_a_la_cola(self):
        assert resolve(self.IDS, {}) == (OUTCOME_REQUEUE, [])

    def test_respuestas_de_no_convocados_no_cuentan(self):
        assert resolve(self.IDS, {"intruso": "accept"}) == (OUTCOME_REQUEUE, [])

    def test_all_answered(self):
        assert all_answered(self.IDS, dict.fromkeys(self.IDS, "accept"))
        assert not all_answered(self.IDS, {"a": "accept"})
        # Sin convocados no se cierra antes: si no, sería un bucle en el mismo tick.
        assert not all_answered([], {})


class TestPlazo:
    def test_factor_por_el_tiempo_esperado_del_mas_grande(self):
        # 16^3 = 4096 intentos a 1024 H/s = 4 s esperados; ×2 = 8 → piso 5.
        assert window_seconds_for(3, 1024, factor=2, minimum=5, maximum=100) == 8

    def test_acotado_por_piso_y_techo(self):
        assert window_seconds_for(1, 1e6, factor=2, minimum=20, maximum=100) == 20
        assert window_seconds_for(9, 1.0, factor=2, minimum=20, maximum=100) == 100

    @pytest.mark.parametrize("hashrate,factor", [(0, 2), (1000, 0)])
    def test_sin_computo_o_sin_factor_es_el_techo(self, hashrate, factor):
        assert window_seconds_for(4, hashrate, factor=factor, minimum=20,
                                  maximum=120) == 120


class TestParticipa:
    def test_sin_lista_mina_cualquiera(self):
        assert participates({"voting_window_id": "W1"}, "x")

    def test_con_lista_solo_los_que_aceptaron(self):
        ch = {"participants": ["a"]}
        assert participates(ch, "a")
        assert not participates(ch, "b")
        assert not participates({"participants": []}, "a")


class TestRespuestaPorDefecto:
    IDS = ["a", "b"]

    def test_vale_para_quien_no_respondio(self):
        assert resolve(self.IDS, {}, {"a": "accept"}) == (OUTCOME_OPEN, ["a"])
        assert resolve(self.IDS, {}, {"b": "reject"}) == (OUTCOME_DISCARD, [])

    def test_la_respuesta_de_la_pausa_la_pisa(self):
        assert resolve(self.IDS, {"a": "reject"}, {"a": "accept"}) == (OUTCOME_DISCARD, [])

    def test_vacia_o_desconocida_no_cuenta(self):
        assert resolve(self.IDS, {}, {"a": "", "b": "quizas"}) == (OUTCOME_REQUEUE, [])

    def test_no_cierra_la_pausa_antes(self):
        """Es 'si no respondo': cerrar antes le quitaría la pausa para cambiarla."""
        assert not all_answered(self.IDS, {})

    def test_se_lee_del_equipo_y_del_standalone(self):
        workers = [w("c", "pool-coordinator"), w("s", default_decision="reject"),
                   w("x", default_decision="cualquiera")]
        teams = [{**equipo("t", "c"), "default_decision": "accept"}]
        lista = {c.voter_id: c.default_decision
                 for c in convocados(workers, teams, {"category": "general"}, **HPS)}
        assert lista == {"c": "accept", "s": "reject", "x": ""}
