"""Propuestas firmadas para los escenarios de carga (A-01, AGENT.md 3.1).

Con ``REQUIRE_SIGNATURES=true`` el API y el NCT rechazan toda propuesta sin
firma, así que los escenarios firman igual que el frontend: un par ECDSA P-256
propio, el mensaje canónico ``author_pubkey|action|text_hash|law_id|created_at|category``
y la firma cruda ``r||s`` en base64.

El mensaje canónico se importa de ``common.identity`` (Pilar 2) en vez de
replicarlo acá: si alguna vez cambia —ya pasó al sumar la categoría— una copia
desactualizada haría que toda la carga se rechace con 401 y la medición saldría
en cero sin que nada lo explique.

Requiere ``cryptography`` (``pip install cryptography``); el resto de los
escenarios sigue siendo biblioteca estándar.
"""

from __future__ import annotations

import hashlib
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import cryptography  # noqa: F401
except ImportError:
    raise SystemExit(
        "Los escenarios firman sus propuestas (REQUIRE_SIGNATURES=true) y para "
        "eso necesitan la librería cryptography:  pip install cryptography")

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "pilar2-distribuido"))

from common.identity import (  # noqa: E402
    generate_private_key,
    proposal_message,
    public_key_b64,
    sign,
)


def propuesta_firmada(text: str, action: str = "promulgacion",
                      category: str = "general",
                      law_id: str | None = None) -> dict:
    """Payload de ``POST /api/laws`` firmado por una identidad recién creada.

    Una identidad por propuesta, como hacían los ``pk-test-<uuid>`` de antes: el
    cooldown es por autor (3.4), y reusar una sola haría que el API rechace todo
    menos la primera. Generar un par P-256 cuesta fracciones de milisegundo, así
    que no distorsiona la medición.

    Hay que llamarla al momento de enviar, no antes: ``created_at`` entra en la
    firma y el NCT descarta lo que tenga más de ``PROPOSAL_MAX_AGE_SECONDS``.
    """
    key = generate_private_key()
    pubkey = public_key_b64(key)
    law_id = law_id or f"ley-{uuid.uuid4().hex[:8]}"
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    created_at = datetime.now(timezone.utc).isoformat()
    return {
        "law_id": law_id,
        "author_pubkey": pubkey,
        "text": text,
        "action": action,
        "category": category,
        "text_hash": text_hash,
        "created_at": created_at,
        "signature": sign(key, proposal_message(pubkey, action, text_hash,
                                                law_id, created_at, category)),
    }
