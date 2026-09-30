"""Métricas de los mineros del k3s, leídas de Redis y expuestas en el `/metrics` del API.

Los mineros corren en el clúster k3s y Prometheus en GKE: un ServiceMonitor sólo
descubre pods del clúster donde corre Prometheus, así que su `/metrics` propio
nunca se scrapea. Lo que sí llega a GKE es el latido que cada minero deja en
`worker:status:<id>` (TTL 15 s), el mismo que usa el NCT para el quórum y la
dificultad dinámica. Este collector lo convierte en gauges al momento del scrape.

Consecuencias:

- Sólo aparecen los mineros **vivos**: uno que deja de latir desaparece a los
  15 s, igual que para el NCT.
- Los gauges son el valor del último latido. Los contadores y los
  histogramas de minería (intentos y éxitos por recurso, duración por prefijo,
  latencia RabbitMQ → minero) viajan en el campo `mining_stats` del latido
  (`common.metrics.mining_stats_snapshot`) y se exponen como counters e
  histogramas de verdad: son los acumulados del proceso, así que `rate()` y
  `histogram_quantile()` funcionan y un reinicio del minero es un reset de
  counter. El `resource` de esas series es el del **intento**, no el del
  minero: un minero con GPU que cae a CPU suma en `cpu`.
- Un minero anterior a `mining_stats` sólo aporta los gauges.
- El API corre con 2 réplicas y las dos exportan lo mismo, con distinto `pod`.
  Las consultas tienen que agregar con `max by (worker_id)`, no con `sum`.

Los nombres llevan el prefijo `voxchain_miner_` y no `voxchain_worker_`: esos son
los del propio minero, y si alguno llegara a correr en GKE las dos fuentes
chocarían.
"""

from __future__ import annotations

import logging

from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
)
from prometheus_client.utils import floatToGoString

logger = logging.getLogger("voxchain.api.miner_metrics")

_LABELS = ["worker_id", "mode", "resource"]


class LiveMinersCollector:
    """Collector de prometheus_client sobre `store.live_workers()`."""

    def __init__(self, store):
        self.store = store

    def describe(self):
        # Sin describe(), register() llama a collect() para conocer los nombres
        # y eso haría un scan de Redis al arrancar el API.
        return []

    def collect(self):
        up = GaugeMetricFamily("voxchain_miner_up",
                               "1 por cada minero con latido vigente en Redis", labels=_LABELS)
        hashrate = GaugeMetricFamily("voxchain_miner_hashrate_hps",
                                     "Hashes por segundo del último intento del minero",
                                     labels=_LABELS)
        running = GaugeMetricFamily("voxchain_miner_running",
                                    "1 si el minero tiene su hilo de minería vivo",
                                    labels=_LABELS)
        capacity = GaugeMetricFamily("voxchain_miner_capacity",
                                     "Fragmentos que el minero atiende a la vez", labels=_LABELS)
        has_gpu = GaugeMetricFamily("voxchain_miner_has_gpu",
                                    "1 si el minero mina con GPU", labels=_LABELS)
        tasks = CounterMetricFamily("voxchain_miner_mining_tasks",
                                    "Intentos de minería del minero, por recurso del intento",
                                    labels=_LABELS)
        success = CounterMetricFamily("voxchain_miner_mining_success",
                                      "Intentos que encontraron nonce, por recurso del intento",
                                      labels=_LABELS)
        duration = HistogramMetricFamily("voxchain_miner_mining_duration_seconds",
                                         "Duración de cada intento de minería, por recurso "
                                         "y longitud del prefijo",
                                         labels=_LABELS + ["prefix_len"])
        latency = HistogramMetricFamily("voxchain_miner_challenge_latency_seconds",
                                        "Latencia entre la publicación del desafío en "
                                        "RabbitMQ y su recepción en el minero",
                                        labels=_LABELS)
        scrape_ok = GaugeMetricFamily("voxchain_miner_status_read_ok",
                                      "1 si el API pudo leer worker:status de Redis")
        try:
            vivos = self.store.live_workers()
        except Exception:  # noqa: BLE001 — un Redis caído no debe tumbar /metrics
            logger.warning("no se pudo leer worker:status para las métricas", exc_info=True)
            scrape_ok.add_metric([], 0)
            yield scrape_ok
            return

        for estado in vivos:
            labels = [str(estado.get("worker_id", "")),
                      str(estado.get("mode") or "desconocido"),
                      "gpu" if estado.get("has_gpu") else "cpu"]
            up.add_metric(labels, 1)
            hashrate.add_metric(labels, _numero(estado.get("hashrate_hps")))
            running.add_metric(labels, 1 if estado.get("running") else 0)
            capacity.add_metric(labels, _numero(estado.get("capacity"), 1))
            has_gpu.add_metric(labels, 1 if estado.get("has_gpu") else 0)
            _agregar_stats(estado, labels, tasks, success, duration, latency)
        scrape_ok.add_metric([], 1)
        yield from (up, hashrate, running, capacity, has_gpu,
                    tasks, success, duration, latency, scrape_ok)


def _agregar_stats(estado: dict, labels: list, tasks, success, duration, latency) -> None:
    """Vuelca `mining_stats` del latido en las familias. Lo mal formado se saltea."""
    stats = estado.get("mining_stats")
    if not isinstance(stats, dict):
        return
    worker_id, mode, _ = labels
    for familia, clave in ((tasks, "tasks"), (success, "success")):
        por_recurso = stats.get(clave)
        if not isinstance(por_recurso, dict):
            continue
        for recurso, valor in por_recurso.items():
            numero = _numero(valor, None)
            if numero is not None:
                familia.add_metric([worker_id, mode, str(recurso)], numero)
    for serie in _series(stats.get("duration")):
        recurso = str(serie["labels"].get("resource", ""))
        prefix_len = str(serie["labels"].get("prefix_len", ""))
        duration.add_metric([worker_id, mode, recurso, prefix_len],
                            serie["buckets"], sum_value=serie["sum"])
    # El histograma de latencia no tiene labels propios: es del minero, así que
    # lleva el recurso del minero, igual que los gauges.
    for serie in _series(stats.get("challenge_latency")):
        latency.add_metric(labels, serie["buckets"], sum_value=serie["sum"])


def _series(crudas) -> list[dict]:
    """Series de histograma válidas: buckets acumulativos que terminan en +Inf.

    El `le` se normaliza como lo escribe prometheus_client ("10.0", "+Inf"): un
    "10" y un "10.0" serían series distintas para Prometheus.
    """
    if not isinstance(crudas, list):
        return []
    validas = []
    for serie in crudas:
        try:
            bordes = [float(le) for le, _ in serie["buckets"]]
            buckets = [(floatToGoString(le), float(v))
                       for le, (_, v) in zip(bordes, serie["buckets"])]
            labels = serie.get("labels") or {}
            suma = float(serie.get("sum", 0.0))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if (not buckets or buckets[-1][0] != "+Inf" or not isinstance(labels, dict)
                or bordes != sorted(bordes)
                or any(a > b for (_, a), (_, b) in zip(buckets, buckets[1:]))):
            continue
        validas.append({"labels": labels, "buckets": buckets, "sum": suma})
    return validas


def _numero(valor, default: float | None = 0.0) -> float | None:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return default
