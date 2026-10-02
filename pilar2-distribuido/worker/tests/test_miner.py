"""Tests del puente al minero y de la lógica del worker."""

import hashlib
import importlib.util
import json
import os
import pathlib

import pytest

from common.messaging import InMemoryBus
from common.metrics import REGISTRY, mining_stats_snapshot, observe_challenge_latency
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

    def fake_mine(base, prefix, rmin, rmax, **kw):
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

    sw = StandaloneWorker(bus, worker_id="w1", mine=lambda *a, **kw: (1, "h"), clock=lambda: 0)
    sw._rejected_actions = {"derogacion"}
    sw.wire()
    bus.publish_challenge({
        "voting_window_id": "W2", "law_id": "L2", "action": "derogacion",
        "partial_hash_base": "base", "n_zeros_required": 5,
    })
    assert publicados == []


def test_standalone_con_deliberacion_mina_solo_si_su_dueno_acepto():
    """Si no responde no vota: fuera de la lista de participantes no toca un hash."""
    bus = InMemoryBus()
    publicados = []
    bus.on_nonce_response(publicados.append)

    sw = StandaloneWorker(bus, worker_id="w1", mine=lambda *a, **kw: (1, "h"), clock=lambda: 0)
    sw._rejected_actions = set()
    sw.wire()
    base = {"action": "promulgacion", "partial_hash_base": "base",
            "n_zeros_required": 1, "law_id": "L1"}
    bus.publish_challenge({**base, "voting_window_id": "W1", "participants": ["w2"]})
    assert publicados == []

    bus.publish_challenge({**base, "voting_window_id": "W2",
                           "participants": ["w1", "w2"]})
    assert [p["voting_window_id"] for p in publicados] == ["W2"]


def test_standalone_es_idempotente_por_ventana():
    bus = InMemoryBus()
    publicados = []
    bus.on_nonce_response(publicados.append)
    calls = []

    def fake_mine(base, prefix, rmin, rmax, **kw):
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


def test_gpu_rota_silenciosa_cae_a_cpu(tmp_path):
    """Bug real del despliegue: en un nodo sin GPU el binario CUDA sale 0 sin
    nonce (fallo silencioso), y el fallback por excepción nunca se disparaba —
    el worker minaba al vacío. El self-test debe detectarlo y usar CPU."""
    import worker_pkg.miner as miner_mod
    fake_gpu = tmp_path / "gpu_bin"
    fake_gpu.write_text("#!/bin/sh\necho 'No se encontró nonce en el rango especificado'\nexit 0\n")
    fake_gpu.chmod(0o755)

    miner_mod._gpu_selftest_ok = None  # resetear el cache del self-test
    try:
        base = "L1hW1promulgacion"
        nonce, h = run_miner(base, "00", 0, 1_000_000,
                             gpu_bin=str(fake_gpu),
                             cpu_script=os.path.abspath(CPU_SCRIPT),
                             prefer_gpu=True)
        assert nonce is not None, "debió caer a CPU y encontrar el nonce"
        assert hashlib.md5(f"{base}{nonce}".encode()).hexdigest().startswith("00")
        assert miner_mod._gpu_selftest_ok is False
    finally:
        miner_mod._gpu_selftest_ok = None


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


def test_snapshot_de_mineria_refleja_el_registro():
    """Lo que viaja en el latido es lo mismo que tiene el /metrics del minero."""
    nonce, _ = run_miner("L1hW1snapshot", "00", 0, 1_000_000, prefer_gpu=False,
                         cpu_script=os.path.abspath(CPU_SCRIPT))
    assert nonce is not None
    observe_challenge_latency({"published_at": 10.0}, now=10.2)

    # Tiene que sobrevivir a json.dumps: es lo que se escribe en Redis.
    snap = json.loads(json.dumps(mining_stats_snapshot()))

    cpu = {"resource": "cpu"}
    assert snap["tasks"]["cpu"] == _sample("voxchain_worker_mining_tasks_total", cpu)
    assert snap["success"]["cpu"] == _sample("voxchain_worker_mining_success_total", cpu)

    duracion = {"resource": "cpu", "prefix_len": "2"}
    serie = next(s for s in snap["duration"] if s["labels"] == duracion)
    assert serie["buckets"][-1] == [
        "+Inf", _sample("voxchain_worker_mining_duration_seconds_count", duracion)]
    assert serie["sum"] == pytest.approx(
        _sample("voxchain_worker_mining_duration_seconds_sum", duracion))
    assert [le for le, _ in serie["buckets"]][:2] == ["0.1", "0.5"]

    (latencia,) = snap["challenge_latency"]
    assert latencia["labels"] == {}
    assert latencia["buckets"][-1] == [
        "+Inf", _sample("voxchain_worker_challenge_latency_seconds_count")]


def test_el_latido_lleva_el_snapshot():
    # `main` a secas resuelve al del nct-coordinator (ver test_switch_mode_remoto).
    ruta = pathlib.Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("worker_main_latido", ruta)
    worker_main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker_main)

    estado = worker_main.WorkerManager("w-latido", has_gpu=False).get_status()

    assert estado["mining_stats"] == mining_stats_snapshot()


# -- corte del minado: deadline y aviso de cierre ----------------------------
#
# Antes nadie avisaba que una ventana había cerrado y el minero barría su rango
# entero: con 50M de nonces, minutos de cómputo sobre una ventana muerta.

import threading
import time
from datetime import datetime, timezone

# Un prefijo imposible sobre un rango enorme: el minero CPU no termina solo.
_IMPOSIBLE = ("L1hW1promulgacion", "0" * 20, 0, 10**12)


def test_run_miner_corta_al_vencer_el_timeout():
    cpu = {"resource": "cpu"}
    tareas_antes = _sample("voxchain_worker_mining_tasks_total", cpu)
    empezo = time.monotonic()
    nonce, h = run_miner(*_IMPOSIBLE, prefer_gpu=False,
                         cpu_script=os.path.abspath(CPU_SCRIPT), timeout=0.5)
    assert (nonce, h) == (None, None)
    assert time.monotonic() - empezo < 5
    # Un barrido a medias no se registra: falsearía el hashrate.
    assert _sample("voxchain_worker_mining_tasks_total", cpu) == tareas_antes


def test_run_miner_corta_al_prenderse_cancel():
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    empezo = time.monotonic()
    nonce, _ = run_miner(*_IMPOSIBLE, prefer_gpu=False,
                         cpu_script=os.path.abspath(CPU_SCRIPT), cancel=cancel)
    assert nonce is None
    assert time.monotonic() - empezo < 5


def _desafio(wid, **extra):
    return {"voting_window_id": wid, "law_id": "L1", "action": "promulgacion",
            "partial_hash_base": f"base-{wid}", "n_zeros_required": 1, **extra}


class _MineroLento:
    """Mina hasta que lo cortan; anota qué ventanas cortaron."""

    def __init__(self):
        self.empezo = {}
        self.cortadas = []
        self.termino = threading.Event()

    def __call__(self, base, prefix, rmin, rmax, *, timeout=None, cancel=None):
        wid = base.removeprefix("base-")
        self.empezo.setdefault(wid, threading.Event()).set()
        if cancel.wait(5):
            self.cortadas.append(wid)
        self.termino.set()
        return 7, "h"

    def esperar_inicio(self, wid):
        return self.empezo.setdefault(wid, threading.Event()).wait(5)


def _standalone(bus, mine, **kw):
    sw = StandaloneWorker(bus, worker_id="w1", mine=mine, clock=lambda: 0, **kw)
    sw._rejected_actions = set()
    sw._categories = []
    sw.wire()
    return sw


def test_standalone_corta_la_ventana_al_recibir_el_cierre():
    bus = InMemoryBus()
    publicados = []
    bus.on_nonce_response(publicados.append)
    minero = _MineroLento()
    _standalone(bus, minero, background=True)

    # En segundo plano el handler vuelve enseguida: el hilo de RabbitMQ queda
    # libre para recibir el aviso mientras se mina.
    bus.publish_challenge(_desafio("W1"))
    assert minero.esperar_inicio("W1")
    bus.publish_window_closed({"voting_window_id": "W1", "result": "success"})

    assert minero.termino.wait(5)
    time.sleep(0.2)
    assert minero.cortadas == ["W1"]
    assert publicados == []  # el nonce de una ventana cerrada no se publica


def test_standalone_un_desafio_nuevo_corta_el_anterior():
    bus = InMemoryBus()
    minero = _MineroLento()
    sw = _standalone(bus, minero, background=True)

    bus.publish_challenge(_desafio("W1"))
    assert minero.esperar_inicio("W1")
    bus.publish_challenge(_desafio("W2"))
    assert minero.esperar_inicio("W2")

    deadline = time.monotonic() + 5
    while "W1" not in minero.cortadas and time.monotonic() < deadline:
        time.sleep(0.05)
    assert minero.cortadas == ["W1"]
    sw.stop()  # corta W2 también


def test_standalone_no_mina_una_ventana_que_ya_cerro():
    bus = InMemoryBus()
    llamadas = []
    _standalone(bus, lambda *a, **kw: llamadas.append(a) or (1, "h"))
    bus.publish_window_closed({"voting_window_id": "W1", "result": "expired_pending"})
    bus.publish_challenge(_desafio("W1"))
    assert llamadas == []


def test_standalone_le_pasa_el_deadline_al_minero():
    bus = InMemoryBus()
    recibido = {}

    def mine(base, prefix, rmin, rmax, *, timeout=None, cancel=None):
        recibido["timeout"] = timeout
        return None, None

    sw = StandaloneWorker(bus, worker_id="w1", mine=mine, clock=lambda: 1000.0)
    sw._rejected_actions = set()
    sw._categories = []
    sw.wire()
    deadline = datetime.fromtimestamp(1030.0, tz=timezone.utc).isoformat()
    bus.publish_challenge(_desafio("W1", deadline=deadline))
    assert recibido["timeout"] == pytest.approx(30.0)
