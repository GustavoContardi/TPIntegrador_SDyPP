"""Estado del sistema de cara al ciudadano: ¿se puede proponer ahora?

El NCT no abre la ventana de una ley si no hay mineros capaces de minar su área
(``common/blockchain/availability.py``): la ley queda encolada. Sin este
endpoint, desde afuera eso se ve exactamente igual que un sistema colgado — la
ley entra, no pasa nada, y nadie sabe si se perdió. Acá se dice qué está
pasando y en qué queda la ley.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from common.blockchain import (
    ACTION_DEROGACION,
    ACTION_PROMULGACION,
    DEFAULT_CATEGORY,
    category_label,
    normalize_category,
)
from voxchain_api.models import SystemAvailability
from voxchain_api.services.redis_reader import RedisReader

router = APIRouter(prefix="/api/system", tags=["system"])


def get_redis_reader():
    """Dependency injection for RedisReader."""
    return RedisReader()


def availability_for(redis: RedisReader, category: str, action: str = "",
                     law_id: str = "") -> SystemAvailability:
    """Disponibilidad del sistema para la ventana que abriría esta propuesta.

    Mide el quórum en el momento y le agrega el contexto que sólo tiene el NCT:
    desde cuándo está sin red y cuántas leyes quedaron esperando.

    ``action`` importa: un equipo puede votar el área y aun así rechazar todas
    las derogaciones, de modo que la misma categoría puede estar disponible para
    promulgar e indisponible para derogar.
    """
    area = normalize_category(category)
    quorum = redis.assess_availability(area, action, law_id)
    estado = redis.get_availability_state()
    try:
        encoladas = len(redis.store.queued_law_ids())
    except Exception:  # noqa: BLE001
        encoladas = 0
    return SystemAvailability(
        available=quorum.ok,
        category=area,
        live_workers=quorum.live,
        eligible_workers=quorum.eligible,
        required_workers=quorum.required,
        queued_laws=encoladas,
        # `since` es del NCT, que es el único que sabe cuándo dejó de abrir
        # ventanas. Sólo se propaga si coincide con lo que estamos midiendo: si
        # el NCT ve la red bien y nosotros no, el dato sería de otra situación.
        since=(estado.get("since") or None) if not quorum.ok else None,
        action=action,
        message=_message(quorum.ok, area, quorum.eligible, quorum.live,
                         quorum.required, encoladas, action),
    )


def _message(ok: bool, category: str, eligible: int, live: int, required: int,
             queued: int, action: str = "") -> str:
    """Explicación en castellano, pensada para mostrarse tal cual en la UI.

    Se arma en el backend y no en el frontend a propósito: la regla que decide
    si una ley sale o espera vive acá, y dos redacciones en paralelo terminan
    diciendo cosas distintas cuando una de las dos se olvida de actualizarse.
    """
    area = category_label(category)
    # "derogaciones de Salud" se lee mejor que "leyes de Salud (derogacion)", y
    # es el dato que le falta al ciudadano para entender por qué su derogación
    # espera mientras las leyes nuevas de la misma área salen.
    que = {ACTION_DEROGACION: f"derogaciones de {area}",
           ACTION_PROMULGACION: f"leyes nuevas de {area}"}.get(
               action, f"leyes de {area}")
    # Sin jerga a propósito: esto lo lee quien propone, que no sabe ni tiene
    # por qué saber qué es un quórum, una ventana o un minero elegible.
    if ok:
        mineros = "minero listo" if eligible == 1 else "mineros listos"
        return f"Hay {eligible} {mineros} para respaldar {que}."
    if live == 0:
        base = "En este momento no hay mineros encendidos en la red."
    else:
        encendidos = ("minero encendido" if live == 1
                      else "mineros encendidos")
        base = (f"Hay {live} {encendidos}, pero por ahora ninguno respalda "
                f"{que}.")
    cola = ""
    if queued:
        cola = (" Ya hay 1 ley esperando su turno." if queued == 1
                else f" Ya hay {queued} leyes esperando su turno.")
    return (f"{base} Tu ley no se pierde: queda esperando y su votación "
            f"arranca sola en cuanto haya mineros disponibles.{cola}")

@router.get("/availability", response_model=SystemAvailability)
async def get_availability(
    category: str = Query(DEFAULT_CATEGORY,
                          description="Área de gobierno de la ley a proponer"),
    action: str = Query("", description="promulgacion | derogacion"),
    law_id: str = Query("", description="Ley concreta, si se está por derogar"),
    redis: RedisReader = Depends(get_redis_reader),
):
    """¿Hay mineros dispuestos a minar la ventana que abriría esta propuesta?

    Sin ``action`` se responde sólo por el área, que es la pregunta del que está
    por escribir una ley. Con ``action=derogacion`` se aplica además el veto de
    acción: un equipo puede votar el área y rechazar toda derogación.
    """
    return availability_for(redis, category, action, law_id)
