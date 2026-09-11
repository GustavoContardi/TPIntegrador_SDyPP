"""Dificultad dinámica (`common/blockchain/difficulty.py`).

El objetivo del módulo es que **promulgar cueste siempre lo mismo en tiempo**
sin importar cuántos mineros haya. Lo que se prueba acá es exactamente eso, más
las dos defensas que impiden que el mecanismo se use como palanca: el trinquete
y los topes.
"""

from __future__ import annotations

from common.blockchain.difficulty import (
    DifficultyRatchet,
    difficulty_for,
    effective_hashrate,
    expected_attempts,
    hashrate_of,
    max_n_for_space,
    nonce_space_for,
    searchers,
)

CPU = 1_000_000.0
GPU = CPU * 100


def _w(wid, gpu=False, cap=1, hps=None):
    w = {"worker_id": wid, "has_gpu": gpu, "capacity": cap}
    if hps is not None:
        w["hashrate_hps"] = hps
    return w


def _equipo(tid, coord, miembros):
    return {"team_id": tid, "coordinator_worker_id": coord, "members": miembros}


class TestElObjetivo:
    """Lo que el usuario pidió: el tiempo se mantiene, `n` se mueve."""

    def test_mas_computo_sube_n(self):
        chico = difficulty_for(CPU, target_seconds=30)
        grande = difficulty_for(CPU * 1000, target_seconds=30)
        assert grande > chico

    def test_el_tiempo_esperado_queda_cerca_del_objetivo(self):
        for hps in (CPU, CPU * 50, CPU * 5000, GPU):
            n = difficulty_for(hps, target_seconds=30)
            t = expected_attempts(n) / hps
            # `n` es discreto y salta de a factores de 16: el tiempo real cae
            # entre el objetivo/16 y el objetivo. Lo que importa es que no se
            # dispare en ninguna de las dos direcciones.
            assert 30 / 16 <= t <= 30

    def test_redondea_hacia_abajo(self):
        """Pasarse encarece por 16: mejor sellar antes que vencer sin sellar."""
        assert difficulty_for(CPU, target_seconds=17) == 6
        assert difficulty_for(CPU, target_seconds=16) == 5


class TestComoSeMideLaRed:
    def test_los_standalone_no_se_suman_entre_si(self):
        """Todos barren desde 0 el mismo rango: calculan el mismo nonce."""
        uno = effective_hashrate([_w("w0")], [], CPU, GPU)
        veinte = effective_hashrate([_w(f"w{i}") for i in range(20)], [], CPU, GPU)
        assert uno == veinte == CPU

    def test_veinte_standalone_no_mueven_la_dificultad(self):
        """El caso que motivó todo: registrar mineros no puede abaratar leyes."""
        uno = [_w("w0")]
        millon = [_w(f"w{i}") for i in range(1000)]
        assert (difficulty_for(effective_hashrate(uno, [], CPU, GPU))
                == difficulty_for(effective_hashrate(millon, [], CPU, GPU)))

    def test_un_equipo_si_agrega(self):
        ws = [_w(f"w{i}") for i in range(20)]
        equipo = [_equipo("t", "w0", [f"w{i}" for i in range(1, 20)])]
        assert effective_hashrate(ws, equipo, CPU, GPU) == 20 * CPU

    def test_dos_equipos_no_se_suman_entre_si(self):
        ws = [_w(f"w{i}") for i in range(4)]
        equipos = [_equipo("a", "w0", ["w1"]), _equipo("b", "w2", ["w3"])]
        assert effective_hashrate(ws, equipos, CPU, GPU) == 2 * CPU

    def test_un_miembro_apagado_no_cuenta(self):
        """Se mide lo vivo: inflar el padrón no mueve la dificultad."""
        pob = [_w("c")]
        equipo = [_equipo("t", "c", ["fantasma"])]
        assert effective_hashrate(pob, equipo, CPU, GPU) == CPU

    def test_prefiere_el_hashrate_medido_al_estimado(self):
        assert hashrate_of(_w("w", hps=12345.0), CPU, GPU) == 12345.0
        assert hashrate_of(_w("w"), CPU, GPU) == CPU

    def test_un_minero_recien_conectado_se_estima_por_recurso(self):
        # hashrate_hps=0 ⇒ todavía no minó; si no se le estimara algo, no
        # contaría para la dificultad y bastaría reconectar para abaratarla.
        assert hashrate_of(_w("g", gpu=True, hps=0), CPU, GPU) == GPU

    def test_red_vacia_da_el_piso(self):
        assert effective_hashrate([], [], CPU, GPU) == 0.0
        assert difficulty_for(0.0) == 3

    def test_los_buscadores_salen_ordenados(self):
        ws = [_w("chico"), _w("g", gpu=True)]
        assert searchers(ws, [], CPU, GPU)[0][0] == "standalone:g"


class TestTrinquete:
    """Sube en el acto, baja con histéresis: bajar es lo único que sirve atacar."""

    def test_el_primer_valor_se_adopta_tal_cual(self):
        assert DifficultyRatchet(decay_windows=3).update(6) == 6

    def test_sube_de_inmediato(self):
        r = DifficultyRatchet(decay_windows=3)
        r.update(5)
        assert r.update(8) == 8

    def test_no_baja_a_la_primera_medicion_chica(self):
        r = DifficultyRatchet(decay_windows=3)
        r.update(7)
        assert r.update(4) == 7
        assert r.update(4) == 7

    def test_baja_recien_tras_sostener_la_medicion(self):
        r = DifficultyRatchet(decay_windows=3)
        r.update(7)
        for _ in range(3):
            n = r.update(4)
        assert n == 6          # baja de a un cero, no de golpe a 4

    def test_una_medicion_alta_reinicia_la_racha(self):
        """Apagar mineros de a ratos no alcanza: hay que sostenerlo."""
        r = DifficultyRatchet(decay_windows=3)
        r.update(7)
        r.update(4)
        r.update(4)
        r.update(7)            # vuelve a aparecer cómputo
        assert r.update(4) == 7
        assert r.update(4) == 7


class TestTopes:
    def test_el_espacio_de_nonces_acota_n(self):
        """Techo duro: con `n` por encima, las derogaciones vencen sin solución."""
        assert max_n_for_space(1_250_000_000) == 6
        assert difficulty_for(GPU * 1000, nonce_space=1_250_000_000) <= 6

    def test_el_espacio_se_dimensiona_sobre_la_derogacion(self):
        assert nonce_space_for(6) > expected_attempts(7)

    def test_espacio_y_techo_son_inversos(self):
        for n in range(3, 8):
            assert max_n_for_space(nonce_space_for(n)) >= n

    def test_respeta_piso_y_techo(self):
        assert difficulty_for(1.0, n_min=4) == 4
        assert difficulty_for(GPU * 10**9, n_max=7) == 7


class TestElTrinqueteSobreviveAlFailover:
    """El estado se persiste, o reiniciar el NCT saltea la histéresis.

    Sin esto había un atajo para el atacante que la histéresis debía cerrar:
    apagar el cómputo y provocar una caída del NCT. El sucesor arrancaba sin
    memoria y adoptaba la medición baja de una sola vez.
    """

    def test_ida_y_vuelta_del_estado(self):
        r = DifficultyRatchet(decay_windows=3)
        r.update(7)
        r.update(4)                       # racha en 1

        sucesor = DifficultyRatchet(decay_windows=3)
        sucesor.restore(r.state())
        assert sucesor.current == 7
        # Continúa la racha donde quedó, no la reinicia.
        assert sucesor.update(4) == 7
        assert sucesor.update(4) == 6     # tercera consecutiva ⇒ baja un cero

    def test_un_nct_nuevo_no_adopta_la_medicion_baja(self):
        """El ataque: apagar el cómputo y forzar un failover."""
        original = DifficultyRatchet(decay_windows=3)
        original.update(8)

        sucesor = DifficultyRatchet(decay_windows=3)
        sucesor.restore(original.state())
        assert sucesor.update(3) == 8     # sin persistencia habría devuelto 3

    def test_el_estado_sobrevive_a_redis(self):
        """Redis devuelve strings: el estado tiene que aguantar el viaje."""
        r = DifficultyRatchet(decay_windows=3)
        r.update(6)
        r.update(4)
        crudo = {k: str(v) for k, v in r.state().items()}   # como lo guarda un hash

        sucesor = DifficultyRatchet(decay_windows=3)
        sucesor.restore(crudo)
        assert sucesor.current == 6
        assert sucesor.update(4) == 6

    def test_un_estado_ausente_o_roto_no_rompe_nada(self):
        """Degradar a la versión en memoria es mejor que no abrir ventanas."""
        for basura in (None, {}, {"current": "ni idea"}, {"low_streak": None}):
            r = DifficultyRatchet(decay_windows=3)
            r.update(5)
            r.restore(basura)
            assert r.update(5) == 5
