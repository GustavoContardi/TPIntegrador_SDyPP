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

# Modo que el minero **debe** tener, decidido por su dueño. A diferencia del
# estado (que caduca a los 15 s y describe el presente), esto es una intención y
# no caduca nunca.
#
# Existe porque el comando por RabbitMQ es efímero: el consumidor de
# ``worker.command`` es una cola exclusiva por worker, así que una orden
# publicada mientras el minero está apagado no la recibe nadie y se pierde en
# silencio. Sin este registro, asignar a un equipo un minero que todavía no
# arrancó era una operación que la UI daba por buena y el minero jamás ejecutaba.
#
# Con él, el comando pasa a ser una optimización de latencia: el minero aplica
# su modo al arrancar y lo reconcilia mientras corre, así que la orden llega
# igual aunque el mensaje se haya perdido.
DESIRED_KEY = "worker:desired_mode:{worker_id}"

# Política de voto del pool: la lee el PoolCoordinator en su tick (cada 3 s) y
# también viaja por HTTP como atajo para el Compose local. Es donde vive la
# agenda temática del equipo del lado del worker.
POLICY_KEY = "pool:policy:{worker_id}"


def read_desired_mode(redis_client, worker_id: str) -> Optional[dict]:
    """Modo que el dueño le fijó al minero, o ``None`` si nunca se le fijó uno."""
    try:
        raw = redis_client.get(DESIRED_KEY.format(worker_id=worker_id))
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def write_desired_mode(redis_client, worker_id: str, mode: str,
                       pool_url: str = "") -> None:
    """Deja registrado el modo que este minero debe adoptar."""
    try:
        redis_client.set(DESIRED_KEY.format(worker_id=worker_id),
                         json.dumps({"mode": mode, "pool_url": pool_url or ""}))
    except Exception:  # noqa: BLE001
        log.warning("no se pudo fijar el modo deseado de %s", worker_id,
                    exc_info=True)


def clear_desired_mode(redis_client, worker_id: str) -> None:
    """Olvida la intención (al dar de baja el minero)."""
    try:
        redis_client.delete(DESIRED_KEY.format(worker_id=worker_id))
    except Exception:  # noqa: BLE001
        log.debug("no se pudo borrar el modo deseado de %s", worker_id,
                  exc_info=True)


def refresh_desired_pool_url(redis_client, worker_id: str, pool_url: str) -> bool:
    """Actualiza la URL del coordinador en la intención de un miembro.

    La dirección del coordinador puede cambiar sin que cambie el equipo (en
    Kubernetes alcanza con que su pod se reinicie). Como el minero reconcilia su
    modo contra este registro, corregirlo acá basta para que se reconecte solo:
    no hace falta re-despachar nada por RabbitMQ.

    Devuelve True si hubo un cambio real.
    """
    desired = read_desired_mode(redis_client, worker_id)
    if not desired or desired.get("mode") != "pool-worker":
        return False
    if (desired.get("pool_url") or "") == (pool_url or ""):
        return False
    write_desired_mode(redis_client, worker_id, "pool-worker", pool_url)
    log.info("minero %s: URL del coordinador actualizada a %s", worker_id, pool_url)
    return True


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

    1. **Redis (``worker:desired_mode:*``)**: la intención, que sobrevive a que
       el minero esté apagado. Es el canal autoritativo.
    2. **RabbitMQ**: el aviso inmediato, para que un minero encendido no espere
       al siguiente ciclo de reconciliación.
    3. **Redis (``worker:status:*``)**: escritura optimista del modo esperado,
       para que la UI no muestre el modo viejo mientras tanto.
    4. **HTTP**: atajo para el Compose local; si falla se ignora.
    """
    # La intención va primero y es lo único que no se puede perder: si el minero
    # está apagado, el mensaje de abajo no lo recibe nadie, pero al arrancar va a
    # leer esto y adoptar el modo.
    write_desired_mode(redis_client, worker_id, target, pool_url)

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


async def push_voting_policy(coordinator_worker_id: str, categories,
                             redis_client) -> dict:
    """Baja la agenda del equipo a su coordinador (AGENT.md 3.10).

    La agenda se guarda en el equipo, pero quien decide si se mina una ventana
    es el ``PoolCoordinator``, que corre en otra máquina. El puente es
    ``pool:policy:<coordinador>`` en Redis, que el coordinador relee en cada
    tick; el POST HTTP es sólo el atajo del Compose local, y su fallo se ignora
    porque Redis ya cubrió el caso.

    Se preserva ``decision`` si ya había una política escrita: la agenda por
    categorías y el rechazo puntual por ``action``/``law_id`` son dos filtros
    distintos y ninguno debería pisar al otro.
    """
    key = POLICY_KEY.format(worker_id=coordinator_worker_id)
    policy = {"decision": "accept"}
    try:
        raw = redis_client.get(key)
        if raw:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            previous = json.loads(raw)
            if isinstance(previous, dict):
                policy = previous
    except Exception:  # noqa: BLE001
        log.debug("política previa de %s ilegible; se reescribe entera",
                  coordinator_worker_id, exc_info=True)
    policy["categories"] = list(categories or [])
    policy.setdefault("decision", "accept")

    redis_client.set(key, json.dumps(policy))

    base_url = _pool_http_url(coordinator_worker_id, redis_client)
    if base_url:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(f"{base_url}/pool/policy", json=policy,
                                  timeout=2.0)
        except Exception:  # noqa: BLE001
            log.debug("atajo HTTP de política a %s no disponible",
                      coordinator_worker_id, exc_info=True)
    return policy


def clear_voting_policy(coordinator_worker_id: str, redis_client) -> None:
    """Borra la política de un coordinador que dejó de serlo.

    Sin esto, disolver un equipo y volver a fundar otro con el mismo minero lo
    haría arrancar con la agenda del equipo anterior, que nadie eligió.
    """
    try:
        redis_client.delete(POLICY_KEY.format(worker_id=coordinator_worker_id))
    except Exception:  # noqa: BLE001
        log.debug("no se pudo borrar la política de %s", coordinator_worker_id,
                  exc_info=True)


def _pool_http_url(worker_id: str, redis_client) -> Optional[str]:
    """URL HTTP del coordinador, si se la puede resolver desde su estado."""
    status = read_worker_status(redis_client, worker_id)
    if not status:
        return None
    return coordinator_address(status, worker_id)
