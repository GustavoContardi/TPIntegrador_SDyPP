"""Ocultar credenciales antes de que lleguen a un log.

Las URLs de conexión de este proyecto —AMQP y Redis— llevan la contraseña
embebida (``esquema://usuario:contraseña@host``). Loguearlas tal cual manda esas
credenciales en texto plano a Cloud Logging, donde las ve cualquiera con permiso
de lectura de logs. Vive en su propio módulo, y no junto a un cliente concreto,
porque el problema es de todos los que loguean una URL.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

def redact_url(url: str) -> str:
    """Reemplaza la contraseña de una URL por ``***``, conservando el resto.

    Una URL sin contraseña se devuelve intacta. Si no se puede parsear, se
    devuelve ``"***"``: ante la duda, no se filtra nada.
    """
    try:
        partes = urlsplit(url)
    except ValueError:  # pragma: no cover - URL no parseable
        return "***"
    if not partes.password:
        return url
    credencial = f"{partes.username or ''}:***@"
    puerto = f":{partes.port}" if partes.port else ""
    netloc = f"{credencial}{partes.hostname or ''}{puerto}"
    return urlunsplit((partes.scheme, netloc, partes.path, partes.query,
                       partes.fragment))
