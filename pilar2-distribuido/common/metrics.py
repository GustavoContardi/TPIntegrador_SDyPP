"""Métricas Prometheus compartidas entre todos los servicios de VoxChain.

Cada servicio registra sus métricas específicas sobre el registro global.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, registry

REGISTRY = registry.REGISTRY

# ----- NCT -----
nct_proposals_total = Counter(
    "voxchain_nct_proposals_total", "Total de propuestas recibidas",
)
nct_blocks_sealed_total = Counter(
    "voxchain_nct_blocks_sealed_total", "Bloques sellados",
)
nct_windows_opened_total = Counter(
    "voxchain_nct_windows_opened_total", "Ventanas de votación abiertas",
)
nct_windows_closed_total = Counter(
    "voxchain_nct_windows_closed_total", "Ventanas de votación cerradas",
)
nct_is_leader = Gauge(
    "voxchain_nct_is_leader", "1 si este NCT es el líder actual",
)

# ----- TrP -----
trp_tasks_published_total = Counter(
    "voxchain_trp_tasks_published_total", "Tareas de minería publicadas",
)
trp_active_workers = Gauge(
    "voxchain_trp_active_workers",
    "Workers reportando keep-alive actualmente",
)

# ----- Minería por tipo de recurso (checklist §1) -----
# La tasa de éxito CPU vs GPU se deriva como success_total / tasks_total
# filtrando por el label ``resource``.
worker_mining_tasks_total = Counter(
    "voxchain_worker_mining_tasks_total",
    "Intentos de minería ejecutados, por tipo de recurso",
    labelnames=["resource"],
)
worker_mining_success_total = Counter(
    "voxchain_worker_mining_success_total",
    "Intentos de minería que encontraron nonce, por tipo de recurso",
    labelnames=["resource"],
)
worker_mining_duration_seconds = Histogram(
    "voxchain_worker_mining_duration_seconds",
    "Duración de cada intento de minería, por recurso y longitud del prefijo",
    labelnames=["resource", "prefix_len"],
    buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600),
)
worker_hashrate_hps = Gauge(
    "voxchain_worker_hashrate_hps",
    "Hashes por segundo estimados en el último intento de minería",
    labelnames=["resource"],
)
worker_challenge_latency_seconds = Histogram(
    "voxchain_worker_challenge_latency_seconds",
    "Latencia entre la publicación del desafío en RabbitMQ y su recepción",
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 2.5, 5, 10),
)


def observe_challenge_latency(challenge: dict, now: float) -> None:
    """Mide la latencia RabbitMQ → worker si el desafío trae ``published_at``."""
    published_at = challenge.get("published_at")
    if published_at is None:
        return
    try:
        latency = now - float(published_at)
    except (TypeError, ValueError):
        return
    if latency >= 0:
        worker_challenge_latency_seconds.observe(latency)


def mining_stats_snapshot() -> dict:
    """Contadores e histogramas de minería de este proceso, para el latido.

    Los mineros corren en el k3s y Prometheus en GKE, así que el ``/metrics``
    del minero nunca se scrapea. Este resumen viaja en ``worker:status:<id>``
    y el API lo vuelve a exponer (``voxchain_api/services/miner_metrics.py``).

    Son los acumulados desde que arrancó el proceso, no deltas: así el API los
    publica como counters e histogramas de verdad y ``rate()`` funciona. Un
    reinicio del pod se ve como el reset de un counter, que Prometheus ya
    contempla.

    Formato::

        {"tasks":   {"cpu": 12.0, "gpu": 3.0},
         "success": {"cpu": 2.0},
         "duration": [{"labels": {"resource": "cpu", "prefix_len": "6"},
                       "buckets": [["0.1", 0.0], ..., ["+Inf", 5.0]],
                       "sum": 41.2}],
         "challenge_latency": [{"labels": {}, "buckets": [...], "sum": 0.3}]}

    Los buckets son acumulativos y llevan su ``le``, así el API no depende de
    que minero y API tengan la misma versión de este módulo.
    """
    return {
        "tasks": _counter_por_recurso(worker_mining_tasks_total),
        "success": _counter_por_recurso(worker_mining_success_total),
        "duration": _histograma(worker_mining_duration_seconds),
        "challenge_latency": _histograma(worker_challenge_latency_seconds),
    }


def _counter_por_recurso(counter: Counter) -> dict:
    return {s.labels["resource"]: s.value
            for familia in counter.collect() for s in familia.samples
            if s.name.endswith("_total")}


def _histograma(histogram: Histogram) -> list[dict]:
    series: dict[tuple, dict] = {}
    for familia in histogram.collect():
        for s in familia.samples:
            labels = {k: v for k, v in s.labels.items() if k != "le"}
            serie = series.setdefault(tuple(sorted(labels.items())),
                                      {"labels": labels, "buckets": [], "sum": 0.0})
            if s.name.endswith("_bucket"):
                serie["buckets"].append([s.labels["le"], s.value])
            elif s.name.endswith("_sum"):
                serie["sum"] = s.value
    return list(series.values())


nct_nonce_validation_seconds = Histogram(
    "voxchain_nct_nonce_validation_seconds",
    "Tiempo de verificación y sellado de un nonce válido (verify + seal)",
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 2.5),
)

# ----- Worker -----
worker_tasks_received_total = Counter(
    "voxchain_worker_tasks_received_total", "Tareas de minería recibidas",
)
worker_nonces_found_total = Counter(
    "voxchain_worker_nonces_found_total", "Nonces válidos encontrados",
)
worker_busy = Gauge(
    "voxchain_worker_busy", "1 si el worker está minando actualmente",
)
worker_has_gpu = Gauge(
    "voxchain_worker_has_gpu", "1 si el worker tiene GPU disponible",
)

# ----- API -----
api_http_requests_total = Counter(
    "voxchain_api_http_requests_total",
    "Total de requests HTTP por método y ruta",
    labelnames=["method", "path"],
)
# ----- Pool -----
pool_miners_registered = Gauge(
    "voxchain_pool_miners_registered",
    "Miners registrados actualmente en el pool coordinator",
)
pool_work_distributed_total = Counter(
    "voxchain_pool_work_distributed_total",
    "Sub-tareas de minería distribuidas a miners",
)
pool_nonces_found_total = Counter(
    "voxchain_pool_nonces_found_total",
    "Nonces válidos recibidos de miners del pool",
)
pool_is_leader = Gauge(
    "voxchain_pool_is_leader",
    "1 si este pool coordinator es el líder actual",
)

api_http_request_duration_seconds = Histogram(
    "voxchain_api_http_request_duration_seconds",
    "Duración de requests HTTP en segundos",
    labelnames=["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
