"""Utilidad para crear una conexión Redis."""

from __future__ import annotations

import logging

from .redaction import redact_url

log = logging.getLogger("voxchain.common.redis")


def create_redis(url: str):
    from .storage.redis_store import connect_redis
    r = connect_redis(url)
    # La URL trae la contraseña embebida (redis://:contraseña@host): sin
    # redactarla, la credencial de Redis termina en texto plano en los logs.
    log.info("conectado a Redis (%s)", redact_url(url))
    return r