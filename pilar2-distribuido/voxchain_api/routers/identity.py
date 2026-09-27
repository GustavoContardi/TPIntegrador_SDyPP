"""Revocación de identidades (AGENT.md 3.1, "Revocación").

Una identidad es una clave: si la clave se filtra, sin esto no hay forma de
dejar de aceptarla. Revocar es presentar un *certificado de revocación* —la
firma de ``revoke|<pubkey>|voxchain-revocation-v1`` hecha con esa misma
clave— y es permanente.

Que la firme la propia clave es lo que impide que cualquiera revoque a
cualquiera. La contracara es que quien robó la clave también puede revocarla;
pero eso sólo destruye una identidad que ya estaba comprometida.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from common.identity import revocation_message, verify
from voxchain_api.services.redis_reader import RedisReader

router = APIRouter(prefix="/api/identity", tags=["identity"])


def get_redis_reader():
    return RedisReader()


class RevocationRequest(BaseModel):
    """El certificado de revocación, tal como lo descarga el frontend."""

    pubkey: str
    signature: str


class RevocationStatus(BaseModel):
    pubkey: str
    revoked: bool
    revoked_at: Optional[str] = None


@router.post("/revoke", response_model=RevocationStatus)
async def revoke_identity(req: RevocationRequest,
                          redis: RedisReader = Depends(get_redis_reader)):
    """Revoca una identidad. Idempotente: presentarla de nuevo no cambia nada.

    No se exige frescura (no hay timestamp en el mensaje): el certificado se
    firma por adelantado justamente para poder usarlo cuando ya no se tenga la
    clave.
    """
    if not verify(req.pubkey, revocation_message(req.pubkey), req.signature):
        raise HTTPException(status_code=401,
                            detail="El certificado no corresponde a esa identidad.")
    when = redis.store.revoke_identity(req.pubkey, datetime.now(timezone.utc).isoformat())
    return RevocationStatus(pubkey=req.pubkey, revoked=True, revoked_at=when)


@router.get("/revocation/{pubkey:path}", response_model=RevocationStatus)
async def revocation_status(pubkey: str, redis: RedisReader = Depends(get_redis_reader)):
    """Si ``pubkey`` está revocada. Es pública: cualquiera puede consultarla."""
    when = redis.store.revocation_of(pubkey)
    return RevocationStatus(pubkey=pubkey, revoked=when is not None, revoked_at=when)
