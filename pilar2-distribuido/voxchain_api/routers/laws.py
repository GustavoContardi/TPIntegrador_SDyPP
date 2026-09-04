"""Router for laws endpoints."""

from __future__ import annotations

import hashlib
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

from common.blockchain import (
    ACTION_DEROGACION,
    CATEGORY_LABELS,
    decompress_text,
    normalize_category,
    validate_category,
)
from common.identity import proposal_message, verify
from voxchain_api.config import config
from voxchain_api.models import Law, LawProposalRequest
from voxchain_api.services.rabbitmq_publisher import RabbitMQPublisher
from voxchain_api.services.redis_reader import RedisReader

router = APIRouter(prefix="/api/laws", tags=["laws"])


def get_redis_reader():
    """Dependency injection for RedisReader."""
    return RedisReader()


def get_rabbitmq_publisher():
    """Dependency injection for RabbitMQPublisher."""
    return RabbitMQPublisher()


@router.get("/categories", response_model=list[dict])
async def get_categories():
    """Áreas de gobierno disponibles, con su etiqueta legible.

    La lista la sirve el backend en vez de duplicarla en el frontend: los slugs
    tienen que ser exactamente los mismos que valida el NCT y que los equipos
    guardan en su agenda, y dos listas separadas terminan divergiendo.
    """
    return [{"value": value, "label": label}
            for value, label in CATEGORY_LABELS.items()]


@router.get("", response_model=list[Law])
async def get_laws(
    status: Optional[str] = Query(None, description="Filter by status"),
    category: Optional[str] = Query(None, description="Filter by category"),
    redis: RedisReader = Depends(get_redis_reader),
):
    """Get all laws, optionally filtered by status and/or category."""
    laws = redis.get_laws(status=status)
    if category:
        wanted = normalize_category(category)
        laws = [law for law in laws
                if normalize_category(law.get("category")) == wanted]
    return laws


@router.get("/next", response_model=Optional[Law])
async def get_next_law(redis: RedisReader = Depends(get_redis_reader)):
    """Get the next law that will enter a voting window (round-robin order)."""
    return redis.get_next_law()


@router.get("/queue", response_model=list[Law])
async def get_law_queue(redis: RedisReader = Depends(get_redis_reader)):
    """Get the full ordered queue of pending laws."""
    return redis.get_queued_laws()


@router.get("/{law_id}/text", response_class=PlainTextResponse)
async def get_law_text(law_id: str, redis: RedisReader = Depends(get_redis_reader)):
    """Get the decompressed text of a law."""
    law = redis.get_law(law_id)
    if not law:
        raise HTTPException(status_code=404, detail="Law not found")
    compressed = law.get("text_compressed")
    if not compressed:
        raise HTTPException(status_code=404, detail="Law text not available")
    return decompress_text(compressed)


@router.get("/{law_id}", response_model=Law)
async def get_law(law_id: str, redis: RedisReader = Depends(get_redis_reader)):
    """Get a specific law by ID."""
    law = redis.get_law(law_id)
    if not law:
        raise HTTPException(status_code=404, detail="Law not found")
    return law


@router.post("", response_model=Law)
async def propose_law(
    proposal: LawProposalRequest,
    publisher: RabbitMQPublisher = Depends(get_rabbitmq_publisher),
    redis: RedisReader = Depends(get_redis_reader),
):
    """Propose a new law.

    This endpoint replicates the logic from scripts/propose_law.py:
    - Calculates SHA-256 of the text
    - Compresses the text
    - Generates law_id if not provided
    - Publishes to the RabbitMQ 'propuestas' queue
    """
    category = _resolve_category(proposal, redis)
    _verify_proposal_signature(proposal, category)

    if redis.store.is_in_cooldown(proposal.author_pubkey):
        cd = redis.store.get_cooldown(proposal.author_pubkey)
        current = redis.store.current_window_number()
        until = cd["cooldown_until_window"]
        raise HTTPException(
            status_code=429,
            detail=(
                f"El autor está en cooldown hasta la ventana {until} "
                f"(ventana actual: {current}). "
                f"Debes esperar {int(until) - current} ventana(s) más."
            ),
        )

    law = publisher.publish_law_proposal(
        author_pubkey=proposal.author_pubkey,
        text=proposal.text,
        action=proposal.action,
        category=category,
        law_id=proposal.law_id,
        text_hash=proposal.text_hash,
        created_at=proposal.created_at,
        signature=proposal.signature,
    )
    publisher.close()
    return law


def _resolve_category(proposal: LawProposalRequest, redis: RedisReader) -> str:
    """Área de gobierno efectiva de la propuesta (AGENT.md 3.10).

    En una **promulgación** manda lo que declaró el autor, validado contra la
    lista cerrada de categorías: una categoría inventada es un 400, no un
    silencioso "general", porque el autor firmó una cosa y encolar otra dejaría
    su ley esperando a equipos que nunca la van a minar.

    En una **derogación** manda la categoría de la ley original: derogar convoca
    a los mismos equipos que promulgaron. Lo que el proponente haya declarado se
    ignora — si pudiera reetiquetar, elegiría el área donde su facción mina y la
    ajena no.
    """
    if proposal.action == ACTION_DEROGACION and proposal.law_id:
        target = redis.get_law(proposal.law_id)
        if target:
            return normalize_category(target.get("category"))
    try:
        return validate_category(proposal.category)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _verify_proposal_signature(proposal: LawProposalRequest, category: str) -> None:
    """Verifica la firma del cliente (A-01) antes de publicar la propuesta.

    Si no hay firma: rechaza con 401 sólo si REQUIRE_SIGNATURES está activo;
    en migración acepta y deja que el NCT haga la verificación autoritativa.
    Si hay firma: exige law_id/created_at (forman el mensaje firmado),
    que text_hash == sha256(text) y que la firma valide contra author_pubkey.

    ``category`` es la ya resuelta por ``_resolve_category``: en una derogación
    eso significa que el cliente tiene que firmar la categoría de la ley que
    quiere derogar, no una cualquiera.
    """
    if not proposal.signature:
        if config.REQUIRE_SIGNATURES:
            raise HTTPException(status_code=401, detail="Propuesta sin firma")
        return
    if not proposal.law_id or not proposal.created_at or not proposal.text_hash:
        raise HTTPException(
            status_code=400,
            detail="Propuesta firmada requiere law_id, created_at y text_hash",
        )
    expected = hashlib.sha256(proposal.text.encode()).hexdigest()
    if proposal.text_hash != expected:
        raise HTTPException(status_code=400, detail="text_hash no corresponde al texto")
    msg = proposal_message(proposal.author_pubkey, proposal.action,
                           proposal.text_hash, proposal.law_id,
                           proposal.created_at, category)
    if not verify(proposal.author_pubkey, msg, proposal.signature):
        raise HTTPException(status_code=401, detail="Firma inválida")
