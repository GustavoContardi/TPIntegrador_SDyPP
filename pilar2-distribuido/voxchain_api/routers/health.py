"""Router for health endpoints."""

from __future__ import annotations

import asyncio
import threading
import time

import httpx

from fastapi import APIRouter, Depends

from common.messaging import build_rabbitmq
from voxchain_api.config import config
from voxchain_api.models import HealthResponse
from voxchain_api.services.redis_reader import RedisReader

router = APIRouter(prefix="/api/health", tags=["health"])

# Desfase de reloj a partir del cual se reporta "skew". Los nodos sincronizan por
# NTP y dos máquinas sanas difieren en milisegundos; medio segundo ya es una
# falla de sincronización, no ruido de la medición.
CLOCK_SKEW_THRESHOLD_MS = 500

# El chequeo de RabbitMQ abre una conexión AMQP (con TLS en el despliegue).
# /api/health es público: sin caché, cada request abriría una conexión al broker.
RABBITMQ_CHECK_TTL_S = 10.0
_rabbitmq_cache: dict = {"at": 0.0, "status": "unknown"}
_rabbitmq_lock = threading.Lock()


def get_redis_reader():
    """Dependency injection for RedisReader."""
    return RedisReader()


def _check_rabbitmq() -> str:
    """Estado del broker, cacheado ``RABBITMQ_CHECK_TTL_S`` segundos."""
    with _rabbitmq_lock:
        if time.monotonic() - _rabbitmq_cache["at"] < RABBITMQ_CHECK_TTL_S:
            return _rabbitmq_cache["status"]
        try:
            status = "ok" if build_rabbitmq(config.RABBITMQ_URL).ping() else "error"
        except Exception:
            status = "error"
        _rabbitmq_cache.update(at=time.monotonic(), status=status)
        return status


def _clock_skew_ms(redis) -> float:
    """Diferencia entre el reloj de este pod y el del servidor de Redis.

    Cada nodo sincroniza con NTP por su cuenta (COS/timesyncd en GKE); esto no
    lo reemplaza, lo hace verificable desde afuera. Se compensa la ida y vuelta
    tomando el punto medio entre antes y después de ``TIME``.
    """
    antes = time.time()
    secs, usecs = redis.store.r.time()
    despues = time.time()
    return (secs + usecs / 1e6 - (antes + despues) / 2) * 1000


@router.get("/live")
async def liveness():
    """Liveness/readiness de la API sola, sin tocar dependencias.

    Las probes van acá y no a ``/api/health``: aquel consulta NCT, Redis y
    RabbitMQ con timeouts de segundos, más que el de 1 s de la probe, y una
    dependencia lenta hacía que Kubernetes reiniciara una API sana.
    """
    return {"api": "ok"}


@router.get("", response_model=HealthResponse)
async def get_health(redis: RedisReader = Depends(get_redis_reader)):
    """Get unified health status of all services."""
    # Check Redis
    redis_status = "ok" if redis.ping() else "error"

    # Check NCT
    nct_status = "ok"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(config.NCT_HEALTH_URL, timeout=2.0)
            nct_status = "ok" if response.status_code == 200 else "error"
    except Exception:
        nct_status = "error"

    # Estado de los workers, vía Redis.
    #
    # Antes se intentaba alcanzar por HTTP al pool coordinator
    # (`http://worker-pool-coordinator:9090/status`), pero ese nombre sólo
    # resuelve dentro del clúster k3s y esta API corre en GKE: el chequeo no
    # podía funcionar en el despliegue federado y siempre caía en "unknown",
    # incluso con los mineros trabajando.
    #
    # Redis es lo único que comparten los dos clústers, y cada worker ya escribe
    # ahí `worker:status:<id>` con TTL de 15 s. Contar esas claves dice cuántos
    # workers están vivos ahora mismo, sin depender de la topología de red.
    workers_status = "unknown"
    try:
        vivos = sum(1 for _ in redis.store.r.scan_iter(match="worker:status:*", count=100))
        # "none" y no "error": que no haya mineros conectados es un estado
        # legítimo del sistema, no una falla de este servicio.
        workers_status = "ok" if vivos else "none"
    except Exception:
        workers_status = "unknown"

    rabbitmq_status = await asyncio.to_thread(_check_rabbitmq)

    frontend_status = "unknown"
    if config.FRONTEND_HEALTH_URL:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(config.FRONTEND_HEALTH_URL, timeout=2.0)
                frontend_status = "ok" if response.status_code == 200 else "error"
        except Exception:
            frontend_status = "error"

    clock_status, skew_ms = "unknown", None
    try:
        skew_ms = round(_clock_skew_ms(redis), 1)
        clock_status = "ok" if abs(skew_ms) < CLOCK_SKEW_THRESHOLD_MS else "skew"
    except Exception:
        pass

    return HealthResponse(
        api="ok",
        nct=nct_status,
        redis=redis_status,
        rabbitmq=rabbitmq_status,
        frontend=frontend_status,
        workers=workers_status,
        clock=clock_status,
        clock_skew_ms=skew_ms,
    )
