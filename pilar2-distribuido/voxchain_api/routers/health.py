"""Router for health endpoints."""

from __future__ import annotations

import httpx

from fastapi import APIRouter, Depends

from voxchain_api.config import config
from voxchain_api.models import HealthResponse
from voxchain_api.services.redis_reader import RedisReader

router = APIRouter(prefix="/api/health", tags=["health"])


def get_redis_reader():
    """Dependency injection for RedisReader."""
    return RedisReader()


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

    return HealthResponse(
        api="ok",
        nct=nct_status,
        redis=redis_status,
        workers=workers_status,
    )
