"""Órdenes a los workers y lectura de su estado reportado.

Extraído del router de workers porque el flujo de equipos manda exactamente las
mismas órdenes (`switch_mode`) que el cambio manual de modo, y tener dos copias
del mismo procedimiento —con el fallback HTTP y la escritura optimista en Redis
incluidos— era la forma segura de que se desincronizaran.

El canal autoritativo es RabbitMQ (exchange ``worker.command``, routing key =
``worker_id``): llega a los workers estén donde estén, incluido el clúster k3s
externo. El HTTP directo es sólo un atajo para el Compose local.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

log = logging.getLogger("voxchain.api.worker_control")

# Atajo HTTP para el stack de docker-compose, donde cada worker es un servicio
# con nombre fijo. En Kubernetes no aplica (los pods no tienen DNS estable) y el
# comando viaja sólo por RabbitMQ.
LOCAL_ADMIN_URLS = {
    "worker-1": "http://worker-1:9090",
    "worker-2": "http://worker-2:9090",
    "pool-coordinator-1": "http://worker-pool-coordinator:9090",
}

# Los workers escriben su estado con TTL 15 s; leerlo es la única forma que
# tiene el backend de saber la dirección real de un coordinador.
STATUS_KEY = "worker:status:{worker_id}"


def read_worker_status(redis_client, worker_id: str) -> Optional[dict]:
    """Último estado reportado por el worker, o ``None`` si no reporta."""
    try:
        raw = redis_client.get(STATUS_KEY.format(worker_id=worker_id))
    except Exception:  # noqa: BLE001
        log.warning("no se pudo leer el estado de %s en Redis", worker_id,
                    exc_info=True)
        return None
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("estado de %s ilegible en Redis", worker_id)
        return None


def coordinator_address(status: dict, worker_id: str,
                        fallback_port: int = 9001) -> str:
    """Dirección HTTP con la que otros workers alcanzan a este coordinador.

    Preferimos el campo ``address`` que el worker publica de sí mismo, porque es
    el único que conoce su IP de pod o el hostname con el que lo resuelven sus
    pares. ``pool_url`` sirve de respaldo para workers de una versión anterior
    que todavía no publican ``address``; el último recurso es armarla con el id,
    que sólo funciona cuando el id coincide con un nombre resoluble.
    """
    for key in ("address", "pool_url"):
        value = (status or {}).get(key) or ""
        if value.strip():
            return value.strip().rstrip("/")
    return f"http://{worker_id}:{fallback_port}"


async def dispatch_switch_mode(worker_id: str, target: str, pool_url: str,
                               redis_client, publisher) -> None:
    """Ordena a un worker cambiar de modo y refleja el cambio en el estado.

    Tres pasos, y los tres importan:

    1. **RabbitMQ**: la orden real. Si falla, se propaga — no tiene sentido
       decirle a la UI que el modo cambió cuando la orden nunca salió.
    2. **Redis**: escritura optimista del modo esperado, para que la UI no
       muestre el modo viejo durante los segundos que tarda el worker en
       aplicar el cambio y volver a reportar.
    3. **HTTP**: atajo para el Compose local; si falla se ignora, porque el
       camino de RabbitMQ ya cubrió el caso.
    """
    cmd = {"type": "switch_mode", "mode": target, "pool_url": pool_url or ""}
    publisher.messaging.publish_worker_command(worker_id, cmd)

    # El paso optimista **refina** lo que el worker reportó; no lo inventa. Si
    # no hay estado previo es porque el worker nunca reportó —recién desplegado,
    # o caído— y escribir uno con running=True lo haría pasar por vivo: un
    # equipo mostraría a su coordinador "en línea" con una dirección adivinada, y
    # alguien podría unirse a un pool que no existe. Sin estado previo, la
    # verdad es "no sé", y eso es lo que hay que mostrar.
    try:
        raw = redis_client.get(STATUS_KEY.format(worker_id=worker_id))
        if raw:
            status = json.loads(raw)
            status["mode"] = target
            status["worker_id"] = worker_id
            status["running"] = True
            status["pool_url"] = pool_url or ""
            redis_client.set(STATUS_KEY.format(worker_id=worker_id),
                             json.dumps(status), ex=15)
    except Exception:  # noqa: BLE001
        log.debug("no se pudo pre-actualizar el estado de %s", worker_id,
                  exc_info=True)

    base_url = LOCAL_ADMIN_URLS.get(worker_id)
    if base_url:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(f"{base_url}/switch-mode",
                                  json={"target": target, "pool_url": pool_url or ""},
                                  timeout=2.0)
        except Exception:  # noqa: BLE001
            log.debug("atajo HTTP a %s no disponible", worker_id, exc_info=True)
