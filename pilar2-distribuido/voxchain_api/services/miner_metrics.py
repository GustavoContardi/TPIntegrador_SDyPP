"""Métricas de los mineros del k3s, leídas de Redis y expuestas en el `/metrics` del API.

Los mineros corren en el clúster k3s y Prometheus en GKE: un ServiceMonitor sólo
descubre pods del clúster donde corre Prometheus, así que su `/metrics` propio
nunca se scrapea. Lo que sí llega a GKE es el latido que cada minero deja en
`worker:status:<id>` (TTL 15 s), el mismo que usa el NCT para el quórum y la
dificultad dinámica. Este collector lo convierte en gauges al momento del scrape.

Consecuencias:

- Sólo aparecen los mineros **vivos**: uno que deja de latir desaparece a los
  15 s, igual que para el NCT.
- Son valores del último latido, no los contadores ni los histogramas del
  minero (tareas, nonces, latencia del desafío): esos no están en Redis.
- El API corre con 2 réplicas y las dos exportan lo mismo, con distinto `pod`.
  Las consultas tienen que agregar con `max by (worker_id)`, no con `sum`.

Los nombres llevan el prefijo `voxchain_miner_` y no `voxchain_worker_`: esos son
los del propio minero, y si alguno llegara a correr en GKE las dos fuentes
chocarían.
"""

from __future__ import annotations

import logging

from prometheus_client.core import GaugeMetricFamily

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
        scrape_ok.add_metric([], 1)
        yield from (up, hashrate, running, capacity, has_gpu, scrape_ok)


def _numero(valor, default: float = 0.0) -> float:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return default
