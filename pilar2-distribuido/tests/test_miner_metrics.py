"""Métricas de los mineros del k3s exportadas por el API desde `worker:status:*`.

Prometheus no alcanza el `/metrics` de los mineros (otro clúster): lo que ve de
ellos es lo que el API arma con el latido que dejan en Redis.
"""

from __future__ import annotations

import json

import fakeredis
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

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
