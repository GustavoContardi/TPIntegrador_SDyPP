"""Tests del puente al minero y de la lógica del worker."""

import hashlib
import os

from common.messaging import InMemoryBus
from common.metrics import REGISTRY, observe_challenge_latency
from worker_pkg.miner import parse_miner_output, run_miner
from worker_pkg.standalone_worker import StandaloneWorker


def _sample(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0

CPU_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                          "pilar1-minero", "cpu", "src", "brute_force.py")


def test_parse_salida_cpu():
    out = 'Nonce = 12345\nMD5("base12345") = 0000abcdef0000abcdef0000abcdef00'
    nonce, h = parse_miner_output(out)
    assert nonce == 12345
    assert h == "0000abcdef0000abcdef0000abcdef00"


def test_parse_salida_gpu():
    out = "Nonce = 777\nMD5 = 000ffacebabe0000facebabe00001234"
    nonce, h = parse_miner_output(out)
    assert nonce == 777
    assert h == "000ffacebabe0000facebabe00001234"


def test_parse_sin_solucion():
    assert parse_miner_output("No encontrado") == (None, None)
    assert parse_miner_output("No se encontró nonce en el rango especificado") == (None, None)


def test_run_miner_cpu_real_encuentra_nonce():
    """Integra de verdad con el minero CPU de Pilar 1 (sin GPU)."""
    base = "L1hW1promulgacion"
    nonce, h = run_miner(base, "00", 0, 1_000_000, prefer_gpu=False,
                         cpu_script=os.path.abspath(CPU_SCRIPT))
    assert nonce is not None
    assert hashlib.md5(f"{base}{nonce}".encode()).hexdigest().startswith("00")
    assert h.startswith("00")


def test_standalone_publica_nonce_cuando_encuentra():
    bus = InMemoryBus()
    publicados = []
    bus.on_nonce_response(publicados.append)

    def fake_mine(base, prefix, rmin, rmax):
        return 42, "0000deadbeef0000deadbeef00001234"

    sw = StandaloneWorker(bus, worker_id="w1", mine=fake_mine, clock=lambda: 0)
    sw._rejected_actions = set()
    sw.wire()
    bus.publish_challenge({
        "voting_window_id": "W1", "law_id": "L1", "action": "promulgacion",
        "partial_hash_base": "base", "n_zeros_required": 4,
    })
    assert len(publicados) == 1
    assert publicados[0]["nonce"] == 42
    assert publicados[0]["winning_node_or_pool"] == "w1"


def test_standalone_rechaza_por_accion():
    bus = InMemoryBus()
    publicados = []
    bus.on_nonce_response(publicados.append)

    sw = StandaloneWorker(bus, worker_id="w1", mine=lambda *a: (1, "h"), clock=lambda: 0)
    sw._rejected_actions = {"derogacion"}
    sw.wire()
    bus.publish_challenge({
        "voting_window_id": "W2", "law_id": "L2", "action": "derogacion",
        "partial_hash_base": "base", "n_zeros_required": 5,
    })
    assert publicados == []


def test_standalone_es_idempotente_por_ventana():
    bus = InMemoryBus()
    publicados = []
    bus.on_nonce_response(publicados.append)
    calls = []

    def fake_mine(base, prefix, rmin, rmax):
        calls.append((rmin, rmax))
        return 1, "h"

    sw = StandaloneWorker(bus, worker_id="w1", mine=fake_mine, clock=lambda: 0)
    sw._rejected_actions = set()
    sw.wire()
    bus.publish_challenge({
        "voting_window_id": "W1", "law_id": "L1", "action": "promulgacion",
        "partial_hash_base": "base", "n_zeros_required": 1,
    })
    bus.publish_challenge({
        "voting_window_id": "W1", "law_id": "L2", "action": "promulgacion",
        "partial_hash_base": "base", "n_zeros_required": 1,
    })
    assert len(publicados) == 1  # no re-publica para la misma ventana
    assert len(calls) == 1


def test_run_miner_registra_metricas_por_recurso():
    """Checklist §1: tasa de éxito, duración y hashrate por tipo de recurso."""
    cpu = {"resource": "cpu"}
    tasks_before = _sample("voxchain_worker_mining_tasks_total", cpu)
    success_before = _sample("voxchain_worker_mining_success_total", cpu)

    base = "L1hW1promulgacion"
    nonce, _ = run_miner(base, "00", 0, 1_000_000, prefer_gpu=False,
                         cpu_script=os.path.abspath(CPU_SCRIPT))
    assert nonce is not None

    assert _sample("voxchain_worker_mining_tasks_total", cpu) == tasks_before + 1
    assert _sample("voxchain_worker_mining_success_total", cpu) == success_before + 1
    assert _sample("voxchain_worker_hashrate_hps", cpu) > 0
    # La duración quedó registrada en el histograma con la longitud del prefijo.
    assert _sample("voxchain_worker_mining_duration_seconds_count",
                   {"resource": "cpu", "prefix_len": "2"}) >= 1


def test_observe_challenge_latency():
    count_before = _sample("voxchain_worker_challenge_latency_seconds_count")

    observe_challenge_latency({"published_at": 10.0}, now=10.5)
    assert _sample("voxchain_worker_challenge_latency_seconds_count") == count_before + 1

    # Sin published_at, con valor no numérico o con latencia negativa: no observa.
    observe_challenge_latency({}, now=10.5)
    observe_challenge_latency({"published_at": "no-numérico"}, now=10.5)
    observe_challenge_latency({"published_at": 99.0}, now=10.5)
    assert _sample("voxchain_worker_challenge_latency_seconds_count") == count_before + 1
