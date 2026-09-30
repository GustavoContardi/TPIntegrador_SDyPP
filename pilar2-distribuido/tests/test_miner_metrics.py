"""Métricas de los mineros del k3s exportadas por el API desde `worker:status:*`.

Prometheus no alcanza el `/metrics` de los mineros (otro clúster): lo que ve de
ellos es lo que el API arma con el latido que dejan en Redis.
"""

from __future__ import annotations

import json

import fakeredis
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from common.metrics import mining_stats_snapshot
from common.storage import VoxChainStore
from voxchain_api.services.miner_metrics import LiveMinersCollector


def _scrape(store) -> dict:
    registry = CollectorRegistry()
    registry.register(LiveMinersCollector(store))
    muestras = {}
    for familia in text_string_to_metric_families(generate_latest(registry).decode()):
        for s in familia.samples:
            muestras[(s.name, s.labels.get("worker_id"))] = (s.value, s.labels)
    return muestras


def _muestras(store) -> list:
    """Todas las muestras del scrape, para las series con más labels que worker_id."""
    registry = CollectorRegistry()
    registry.register(LiveMinersCollector(store))
    return [s for familia in text_string_to_metric_families(generate_latest(registry).decode())
            for s in familia.samples]


def _valor(muestras, nombre, **labels):
    encontradas = [s.value for s in muestras
                   if s.name == nombre and all(s.labels.get(k) == v for k, v in labels.items())]
    assert len(encontradas) == 1, (nombre, labels, encontradas)
    return encontradas[0]


def _histo(pares, suma):
    return {"buckets": [[le, v] for le, v in pares], "sum": suma}


def _latido(r, worker_id, **estado):
    r.set(f"worker:status:{worker_id}", json.dumps({"worker_id": worker_id, **estado}), ex=15)


def test_un_gauge_por_minero_vivo():
    r = fakeredis.FakeRedis(decode_responses=True)
    _latido(r, "w-cpu", mode="standalone", has_gpu=False, running=True,
            hashrate_hps=950_000.0, capacity=1)
    _latido(r, "w-gpu", mode="pool-coordinator", has_gpu=True, running=True,
            hashrate_hps=2.4e8, capacity=4)

    m = _scrape(VoxChainStore(r))

    assert m[("voxchain_miner_up", "w-cpu")][0] == 1
    assert m[("voxchain_miner_up", "w-cpu")][1] == {
        "worker_id": "w-cpu", "mode": "standalone", "resource": "cpu"}
    assert m[("voxchain_miner_hashrate_hps", "w-gpu")][0] == 2.4e8
    assert m[("voxchain_miner_has_gpu", "w-gpu")][0] == 1
    assert m[("voxchain_miner_has_gpu", "w-cpu")][0] == 0
    assert m[("voxchain_miner_capacity", "w-gpu")][0] == 4
    assert m[("voxchain_miner_status_read_ok", None)][0] == 1


def test_minero_sin_latido_no_aparece():
    r = fakeredis.FakeRedis(decode_responses=True)
    _latido(r, "w-vivo", mode="standalone", running=True)
    r.set("worker:status:w-muerto", json.dumps({"mode": "standalone"}))
    r.delete("worker:status:w-muerto")  # venció el TTL

    m = _scrape(VoxChainStore(r))

    assert ("voxchain_miner_up", "w-vivo") in m
    assert ("voxchain_miner_up", "w-muerto") not in m


def test_campos_faltantes_o_invalidos_no_rompen_el_scrape():
    r = fakeredis.FakeRedis(decode_responses=True)
    _latido(r, "w-viejo", hashrate_hps="no-numerico")  # sin mode ni capacity

    m = _scrape(VoxChainStore(r))

    assert m[("voxchain_miner_up", "w-viejo")][1]["mode"] == "desconocido"
    assert m[("voxchain_miner_hashrate_hps", "w-viejo")][0] == 0
    assert m[("voxchain_miner_capacity", "w-viejo")][0] == 1


def test_redis_caido_no_tumba_metrics():
    class _StoreCaido:
        def live_workers(self):
            raise ConnectionError("redis no responde")

    m = _scrape(_StoreCaido())

    assert m[("voxchain_miner_status_read_ok", None)][0] == 0
    assert not any(nombre == "voxchain_miner_up" for nombre, _ in m)


def test_registrar_no_lee_redis():
    """Sin describe(), register() llamaría a collect() y escanearía Redis al arrancar."""
    llamadas = []

    class _Store:
        def live_workers(self):
            llamadas.append(1)
            return []

    CollectorRegistry().register(LiveMinersCollector(_Store()))

    assert llamadas == []


# ---- mining_stats: counters e histogramas que el minero manda en el latido ----

STATS_GPU = {
    "tasks": {"gpu": 7.0, "cpu": 1.0},
    "success": {"gpu": 3.0},
    "duration": [
        {"labels": {"resource": "gpu", "prefix_len": "7"},
         **_histo([("1", 1.0), ("10", 5.0), ("+Inf", 6.0)], 31.5)},
        # El intento que cayó a CPU: el recurso es el del intento, no el del minero.
        {"labels": {"resource": "cpu", "prefix_len": "7"},
         **_histo([("1", 0.0), ("10", 0.0), ("+Inf", 1.0)], 280.0)},
    ],
    "challenge_latency": [
        {"labels": {}, **_histo([("0.01", 2.0), ("0.1", 6.0), ("+Inf", 7.0)], 0.4)},
    ],
}


def test_counters_e_histogramas_desde_el_latido():
    r = fakeredis.FakeRedis(decode_responses=True)
    _latido(r, "w-gpu", mode="standalone", has_gpu=True, running=True,
            mining_stats=STATS_GPU)

    m = _muestras(VoxChainStore(r))
    base = {"worker_id": "w-gpu", "mode": "standalone"}

    assert _valor(m, "voxchain_miner_mining_tasks_total", **base, resource="gpu") == 7
    assert _valor(m, "voxchain_miner_mining_tasks_total", **base, resource="cpu") == 1
    assert _valor(m, "voxchain_miner_mining_success_total", **base, resource="gpu") == 3

    gpu7 = {**base, "resource": "gpu", "prefix_len": "7"}
    assert _valor(m, "voxchain_miner_mining_duration_seconds_bucket", **gpu7, le="10.0") == 5
    assert _valor(m, "voxchain_miner_mining_duration_seconds_count", **gpu7) == 6
    assert _valor(m, "voxchain_miner_mining_duration_seconds_sum", **gpu7) == 31.5
    assert _valor(m, "voxchain_miner_mining_duration_seconds_count",
                  **base, resource="cpu", prefix_len="7") == 1

    # La latencia es del minero: lleva su recurso, igual que los gauges.
    assert _valor(m, "voxchain_miner_challenge_latency_seconds_count",
                  **base, resource="gpu") == 7
    assert _valor(m, "voxchain_miner_challenge_latency_seconds_bucket",
                  **base, resource="gpu", le="0.1") == 6


def test_latido_sin_stats_solo_trae_gauges():
    """Un minero anterior a `mining_stats` sigue apareciendo, sin counters."""
    r = fakeredis.FakeRedis(decode_responses=True)
    _latido(r, "w-viejo", mode="standalone", running=True)

    nombres = {s.name for s in _muestras(VoxChainStore(r))}

    assert "voxchain_miner_up" in nombres
    assert not any(n.startswith(("voxchain_miner_mining_", "voxchain_miner_challenge_"))
                   for n in nombres)


def test_stats_mal_formadas_se_saltean_sin_romper_el_scrape():
    r = fakeredis.FakeRedis(decode_responses=True)
    _latido(r, "w-roto", mode="standalone", running=True, mining_stats={
        "tasks": {"cpu": "muchos", "gpu": 2},
        "success": ["no", "es", "un", "dict"],
        "duration": [
            {"labels": {"resource": "cpu", "prefix_len": "5"},
             **_histo([("1", 1.0), ("10", 2.0)], 3.0)},                  # sin +Inf
            {"labels": {"resource": "cpu", "prefix_len": "6"},
             **_histo([("1", 5.0), ("10", 2.0), ("+Inf", 5.0)], 3.0)},   # no acumulativo
            {"labels": {"resource": "cpu", "prefix_len": "4"}, "buckets": "x"},
            "ni-siquiera-un-dict",
        ],
        "challenge_latency": {"no": "es una lista"},
    })
    _latido(r, "w-sano", mode="standalone", running=True, mining_stats=STATS_GPU)

    m = _muestras(VoxChainStore(r))

    roto = [s for s in m if s.labels.get("worker_id") == "w-roto"]
    assert {s.name for s in roto if "mining_" in s.name} == {"voxchain_miner_mining_tasks_total"}
    assert _valor(m, "voxchain_miner_mining_tasks_total", worker_id="w-roto", resource="gpu") == 2
    assert _valor(m, "voxchain_miner_mining_tasks_total", worker_id="w-sano", resource="gpu") == 7
    assert _valor(m, "voxchain_miner_status_read_ok") == 1


def test_ida_y_vuelta_con_el_snapshot_real_del_minero():
    """Lo que arma `common.metrics` en el minero es lo que el API sabe leer."""
    snap = json.loads(json.dumps(mining_stats_snapshot()))
    r = fakeredis.FakeRedis(decode_responses=True)
    _latido(r, "w-real", mode="standalone", running=True, mining_stats=snap)

    m = _muestras(VoxChainStore(r))

    for recurso, valor in snap["tasks"].items():
        assert _valor(m, "voxchain_miner_mining_tasks_total",
                      worker_id="w-real", resource=recurso) == valor
    (latencia,) = snap["challenge_latency"]
    assert _valor(m, "voxchain_miner_challenge_latency_seconds_count",
                  worker_id="w-real") == latencia["buckets"][-1][1]
