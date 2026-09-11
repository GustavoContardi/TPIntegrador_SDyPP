"""Cuota de turnos por identidad (`common/queue.py`).

Lo que se prueba es la regla de gobierno, no el formato: que una identidad no
monopolice las ventanas, que la cola nunca se bloquee, y —explícitamente— que
contra Sybil la cuota degrada, porque esa limitación está aceptada en AGENT.md 9
y conviene que un test la fije en vez de que se descubra como sorpresa.
"""

from __future__ import annotations

from common.queue import authors_over_quota, select_next_law


def _ley(law_id: str, autor: str) -> dict:
    return {"law_id": law_id, "author_pubkey": autor}


class TestCuota:
    def test_una_identidad_que_se_llevo_casi_todo_cede_el_turno(self):
        recientes = ["atacante"] * 9 + ["honesto"]
        cola = [_ley("l1", "atacante"), _ley("l2", "honesto")]
        elegida = select_next_law(cola, last_author=None, recent_authors=recientes)
        assert elegida["law_id"] == "l2"

    def test_dos_autores_alternando_no_infringen_la_cuota(self):
        # 50% cada uno es el reparto sano: la cuota compara con `>`, no con `>=`.
        recientes = ["a", "b"] * 5
        assert authors_over_quota(recientes) == set()

    def test_sin_muestra_completa_no_se_aplica(self):
        """Recién arrancado, el primer autor estaría al 100% y se autobloquearía."""
        assert authors_over_quota(["a", "a", "a"]) == set()

    def test_sin_historial_es_el_round_robin_de_siempre(self):
        cola = [_ley("l1", "a"), _ley("l2", "b")]
        assert select_next_law(cola, last_author="a")["law_id"] == "l2"
        assert select_next_law(cola, last_author=None)["law_id"] == "l1"


class TestNoSeBloquea:
    def test_si_todos_exceden_igual_abre_ventana(self):
        """Una cuota capaz de dejar al sistema sin ventanas sería un DoS peor."""
        recientes = ["a"] * 10
        cola = [_ley("l1", "a")]
        assert select_next_law(cola, last_author=None,
                               recent_authors=recientes)["law_id"] == "l1"

    def test_el_round_robin_gana_sobre_la_cuota(self):
        # Único candidato distinto del último autor, pero excedido: igual va.
        recientes = ["b"] * 10
        cola = [_ley("l1", "a"), _ley("l2", "b")]
        elegida = select_next_law(cola, last_author="a", recent_authors=recientes)
        assert elegida["law_id"] == "l2"

    def test_cola_vacia(self):
        assert select_next_law([], last_author=None, recent_authors=["a"] * 10) is None


class TestLimiteConocido:
    def test_sybil_evade_la_cuota(self):
        """AGENT.md 9: identidades gratis ⇒ la cuota por identidad no las frena.

        No es un bug a arreglar acá: cerrarlo exige verificación de identidad
        real, que AGENT.md 10 pone fuera de alcance. El test existe para que la
        limitación quede fijada y no se confunda la cuota con una defensa Sybil.
        """
        recientes = [f"sybil-{i}" for i in range(10)]      # una identidad por ventana
        assert authors_over_quota(recientes) == set()

        cola = [_ley("l1", "sybil-99"), _ley("l2", "honesto")]
        elegida = select_next_law(cola, last_author=None, recent_authors=recientes)
        assert elegida["law_id"] == "l1"                    # el Sybil sigue pasando

    def test_pero_el_monopolio_de_una_sola_identidad_si_se_frena(self):
        """El contraste: el mismo ataque sin rotar identidad sí queda acotado."""
        recientes = ["codicioso"] * 10
        cola = [_ley("l1", "codicioso"), _ley("l2", "honesto")]
        elegida = select_next_law(cola, last_author=None, recent_authors=recientes)
        assert elegida["law_id"] == "l2"


class TestDeQuienEsElTurno:
    """Una derogación reutiliza el registro de la ley promulgada.

    El bug que esto fija apareció corriendo el demo: el historial de turnos
    anotaba al autor original de la ley en vez de a quien pidió derogarla, así
    que el que deroga no gastaba cuota y el autor la gastaba por una ventana que
    no había pedido. Afectaba también al round-robin, que es anterior a la cuota.
    """

    def test_una_promulgacion_la_pide_su_autor(self):
        from common.queue import turn_holder
        assert turn_holder({"author_pubkey": "A"}) == "A"

    def test_una_derogacion_la_pide_quien_deroga(self):
        from common.queue import turn_holder
        ley = {"author_pubkey": "autor-original", "requested_by": "el-que-deroga"}
        assert turn_holder(ley) == "el-que-deroga"

    def test_la_cuota_se_le_cobra_al_que_deroga(self):
        recientes = ["spammer"] * 10
        cola = [
            # Ley ajena que "spammer" quiere derogar: sin `requested_by` el
            # round-robin la dejaría pasar porque el autor figura como otro.
            {"law_id": "LD", "author_pubkey": "victima", "requested_by": "spammer"},
            {"law_id": "LH", "author_pubkey": "honesto"},
        ]
        elegida = select_next_law(cola, last_author=None, recent_authors=recientes)
        assert elegida["law_id"] == "LH"

    def test_el_round_robin_tambien_lo_respeta(self):
        cola = [{"law_id": "LD", "author_pubkey": "victima", "requested_by": "A"},
                {"law_id": "LH", "author_pubkey": "B"}]
        assert select_next_law(cola, last_author="A")["law_id"] == "LH"
