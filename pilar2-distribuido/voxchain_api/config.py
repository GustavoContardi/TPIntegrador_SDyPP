"""Configuration for voxchain-api service."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _as_bool(raw: str | None, default: bool) -> bool:
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    REDIS_URL: str
    RABBITMQ_URL: str
    NCT_HEALTH_URL: str
    # Vacío: el frontend no se chequea y figura "unknown" (en local, por ejemplo,
    # la API no tiene cómo saber dónde está).
    FRONTEND_HEALTH_URL: str = ""
    PORT: int = 8000
    # Firma de propuestas (A-01). Si True, el API rechaza propuestas sin firma
    # válida antes de publicarlas. Debe ir alineado con el flag del NCT, y como
    # él arranca en True si la variable falta: el modo abierto se pide explícito.
    REQUIRE_SIGNATURES: bool = True
    # Quórum de mineros para abrir una ventana. Tiene que ser **el mismo valor
    # que el del NCT**, que es quien lo aplica: acá sólo se usa para anticiparle
    # al ciudadano que su ley va a quedar pospuesta. Con valores distintos el
    # API prometería una cosa y el NCT haría otra.
    MIN_WORKERS_FOR_WINDOW: int = 1
    # Ídem: tiene que coincidir con el del NCT.
    QUORUM_BY_CATEGORY: bool = True
    # Quién puede proponer (AGENT.md 3.2). Lo aplica también el NCT, que es la
    # verificación autoritativa; acá sirve para responder un 403 con el motivo
    # en vez de aceptar una propuesta que el NCT va a descartar en silencio.
    RESTRICT_PROPOSERS: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            REDIS_URL=os.getenv("REDIS_URL", "redis://redis:6379/0"),
            RABBITMQ_URL=os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/"),
            NCT_HEALTH_URL=os.getenv("NCT_HEALTH_URL", "http://coordinator:8080/health"),
            FRONTEND_HEALTH_URL=os.getenv("FRONTEND_HEALTH_URL", ""),
            PORT=int(os.getenv("PORT", "8000")),
            REQUIRE_SIGNATURES=_as_bool(os.getenv("REQUIRE_SIGNATURES"), True),
            MIN_WORKERS_FOR_WINDOW=int(os.getenv("MIN_WORKERS_FOR_WINDOW") or 1),
            QUORUM_BY_CATEGORY=_as_bool(os.getenv("QUORUM_BY_CATEGORY"), True),
            RESTRICT_PROPOSERS=_as_bool(os.getenv("RESTRICT_PROPOSERS"), True),
        )


config = Config.from_env()
