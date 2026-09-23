"""Deliberación: la ley anunciada y las respuestas de los convocados (AGENT.md 3.12).

Cuando a una ley le toca su turno, el NCT no abre la ventana en el acto: la
anuncia —sólo la ley y su área— y congela la dificultad sobre el convocado más
grande. Durante la pausa, el fundador de cada equipo convocado y el dueño de
cada standalone convocado responden si aportan cómputo. Quien no responde no
mina.

El NCT escribe el anuncio (``nct:deliberation``) y este router escribe las
respuestas (``deliberation:decisions:<law_id>``): dos claves con un escritor
cada una. Cada respuesta va **firmada** por quien responde, igual que el resto
de las acciones de dueño: ``<voter_id>|deliberate:<law_id>:<decisión>|<ts>``.
La ley y la decisión van dentro del mensaje firmado para que una firma de "sí"
no se pueda reusar como "no", ni la de una ley para otra.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from common.blockchain.deliberation import DECISIONS, KIND_TEAM
from common.storage.redis_store import VoxChainStore
from voxchain_api.models import (
    Deliberation,
    DeliberationDecisionRequest,
    DeliberationResult,
    DeliberationVoter,
)
from voxchain_api.routers.workers import (
    OWNER_WORKERS_MAPPING,
    authorize_owner_action,
    authorize_worker_action,
    get_owner_id,
    get_redis_reader,
    get_signature,
    get_signature_timestamp,
)
from voxchain_api.services.redis_reader import RedisReader
from voxchain_api.services.teams_store import TeamsStore

router = APIRouter(prefix="/api/deliberation", tags=["deliberation"])

log = logging.getLogger("voxchain.api.deliberation")


def deliberate_action(law_id: str, decision: str) -> str:
    """La acción que firma quien responde. El frontend arma la misma."""
    return f"deliberate:{law_id}:{decision}"


def _decode(value) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else (value or "")


def _owner_of(redis_client, voter: dict) -> str:
    """Quién responde por este convocado, leído de la fuente autoritativa.

    Para un equipo, su fundador (``team:<id>``); para un standalone, quien lo
    registró (``worker:owner:<id>``). No se toma el que viene en el anuncio: es
    un dato para mostrar, no para autorizar.
    """
    if voter.get("kind") == KIND_TEAM:
        team = TeamsStore(redis_client).get_team(voter.get("team_id", "")) or {}
        return team.get("owner", "")
    voter_id = voter.get("voter_id", "")
    registrado = _decode(redis_client.get(f"worker:owner:{voter_id}"))
    if registrado:
        return registrado
    # Mineros precargados del desarrollo local: su dueño es ``default``, no un
    # registro en Redis (el camino por cabecera de `authorize_worker_action`).
    return next((dueno for dueno, ids in OWNER_WORKERS_MAPPING.items()
                 if voter_id in ids), "")


@router.get("", response_model=Optional[Deliberation])
async def get_deliberation(redis: RedisReader = Depends(get_redis_reader)):
    """La ley en deliberación ahora, o ``null`` si no hay ninguna."""
    redis_client = redis.store.r
    store = VoxChainStore(redis_client)
    estado = store.get_deliberation()
    if not estado:
        return None
    decisiones = store.deliberation_decisions(estado["law_id"])
    convocados = estado.get("convocados", [])
    mayor = max((float(c.get("hashrate") or 0) for c in convocados), default=0.0)
    voters = [
        DeliberationVoter(
            voter_id=c.get("voter_id", ""), kind=c.get("kind", ""),
            name=c.get("name", ""), owner=_owner_of(redis_client, c),
            team_id=c.get("team_id", ""),
            hashrate=float(c.get("hashrate") or 0),
            decision=decisiones.get(c.get("voter_id", "")),
            default_decision=c.get("default_decision") or "",
            biggest=mayor > 0 and float(c.get("hashrate") or 0) == mayor)
        for c in convocados
    ]
    return Deliberation(
        law_id=estado["law_id"], action=estado.get("action", ""),
        category=estado.get("category", ""),
        started_at=estado.get("started_at", ""),
        decide_until=estado.get("decide_until", ""),
        seconds_left=max(0.0, float(estado.get("decide_until_epoch", 0)) - time.time()),
        n_zeros_required=int(estado.get("n_zeros_required", 0)),
        window_seconds=float(estado.get("window_seconds", 0)),
        voters=voters)


@router.get("/result/{law_id}", response_model=DeliberationResult)
async def get_deliberation_result(law_id: str,
                                  redis: RedisReader = Depends(get_redis_reader)):
    """Cómo terminó la última deliberación de ``law_id``."""
    resultado = VoxChainStore(redis.store.r).get_deliberation_result(law_id)
    if not resultado:
        raise HTTPException(status_code=404,
                            detail="Esa ley no pasó por ninguna deliberación")
    return DeliberationResult(law_id=law_id, **resultado)


@router.post("/{law_id}/decision", response_model=DeliberationVoter)
async def decide(
    law_id: str,
    request: DeliberationDecisionRequest,
    owner_id: str = Depends(get_owner_id),
    signature: Optional[str] = Depends(get_signature),
    timestamp: Optional[str] = Depends(get_signature_timestamp),
    redis: RedisReader = Depends(get_redis_reader),
):
    """Un convocado responde si aporta cómputo a la ventana de ``law_id``.

    Puede cambiar de opinión mientras dure la pausa: vale la última respuesta.
    Responde el fundador por su equipo y el dueño por su standalone.
    """
    if request.decision not in DECISIONS:
        raise HTTPException(status_code=400,
                            detail=f"Decisión inválida; usá una de {list(DECISIONS)}")
    redis_client = redis.store.r
    store = VoxChainStore(redis_client)
    estado = store.get_deliberation()
    if not estado or estado.get("law_id") != law_id:
        raise HTTPException(status_code=409,
                            detail="Esa ley no está en deliberación")
    if time.time() >= float(estado.get("decide_until_epoch", 0)):
        raise HTTPException(status_code=409,
                            detail="La deliberación ya terminó")
    voter = next((c for c in estado.get("convocados", [])
                  if c.get("voter_id") == request.voter_id), None)
    if voter is None:
        raise HTTPException(status_code=403,
                            detail="Ese minero no está convocado para esta ley")

    accion = deliberate_action(law_id, request.decision)
    if voter.get("kind") == KIND_TEAM:
        authorize_owner_action(redis_client, request.voter_id, accion,
                               _owner_of(redis_client, voter), owner_id,
                               signature, timestamp)
    else:
        authorize_worker_action(redis_client, request.voter_id, accion,
                                owner_id, signature, timestamp)

    store.set_deliberation_decision(law_id, request.voter_id, request.decision)
    log.info("%s respondió '%s' en la deliberación de %s",
             request.voter_id, request.decision, law_id)
    return DeliberationVoter(
        voter_id=request.voter_id, kind=voter.get("kind", ""),
        name=voter.get("name", ""), owner=_owner_of(redis_client, voter),
        team_id=voter.get("team_id", ""),
        hashrate=float(voter.get("hashrate") or 0), decision=request.decision)
