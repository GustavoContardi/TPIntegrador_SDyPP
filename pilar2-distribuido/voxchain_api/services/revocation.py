"""Rechazo de firmas de identidades revocadas (AGENT.md 3.1, "Revocación").

Una firma válida de una clave revocada sigue siendo matemáticamente válida: lo
que cambia es que el sistema ya no la acepta. Por eso el chequeo no vive en
``common.identity.verify`` (que es pura y no ve Redis) sino en cada lugar que
acepta la firma de un ciudadano, antes de verificarla.
"""

from __future__ import annotations

from fastapi import HTTPException

from common.storage import VoxChainStore

REVOKED_DETAIL = ("Esta identidad fue revocada y ya no puede firmar nada. "
                  "Creá una identidad nueva.")


def reject_if_revoked(redis_client, pubkey: str) -> None:
    """401 si ``pubkey`` está revocada. ``redis_client`` es el cliente crudo."""
    if pubkey and VoxChainStore(redis_client).is_revoked(pubkey):
        raise HTTPException(status_code=401, detail=REVOKED_DETAIL)
