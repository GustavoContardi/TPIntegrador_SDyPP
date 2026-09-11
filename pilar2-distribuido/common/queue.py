"""Lógica de cola de ventanas compartida entre NCT y API Gateway.

- ``select_next_law``: round-robin entre autores distintos (AGENT.md 3.3) más
  una **cuota de turnos** que acota cuánto puede monopolizar una identidad.
"""

from __future__ import annotations

from collections import Counter
from typing import Optional

# Cuántas ventanas pasadas mira la cuota, y qué fracción de ésas puede haberse
# llevado una misma identidad antes de que se le ceda el turno a otra.
#
# La cuota sólo se aplica con la muestra completa: con menos historia que
# `TURN_QUOTA_WINDOWS`, el primer autor estaría siempre al 100% y se bloquearía
# a sí mismo en un sistema recién arrancado.
TURN_QUOTA_WINDOWS = 10
TURN_QUOTA_MAX_SHARE = 0.5


def turn_holder(law: dict) -> Optional[str]:
    """Identidad a la que se le cobra **este** turno.

    Normalmente es el autor de la ley, pero una derogación reutiliza el registro
    de la ley promulgada (para que herede su categoría, AGENT.md 3.10) y por eso
    `author_pubkey` sigue siendo el del autor original. Cobrarle a él el turno
    sería doblemente injusto: el que deroga no gasta cuota y el autor la gasta
    por una ventana que no pidió. `requested_by` guarda quién pidió esta ventana.
    """
    return law.get("requested_by") or law.get("author_pubkey")


def authors_over_quota(recent_authors: list[str], *,
                       max_share: float = TURN_QUOTA_MAX_SHARE,
                       sample: int = TURN_QUOTA_WINDOWS) -> set[str]:
    """Identidades que ya excedieron su cuota en las últimas ventanas.

    Se compara con ``>`` y no con ``>=``: dos autores alternando quedan cada uno
    exactamente en la mitad, y ese es el reparto sano, no una infracción.
    """
    if len(recent_authors) < sample or sample <= 0:
        return set()
    ventana = recent_authors[:sample]
    total = len(ventana)
    return {autor for autor, veces in Counter(ventana).items()
            if autor and veces / total > max_share}


def select_next_law(pending_laws: list[dict], last_author: Optional[str],
                    recent_authors: Optional[list[str]] = None,
                    *, max_share: float = TURN_QUOTA_MAX_SHARE,
                    sample: int = TURN_QUOTA_WINDOWS) -> Optional[dict]:
    """Elige la próxima ley para abrir ventana.

    ``pending_laws`` viene ordenada de más antigua a más nueva. Se prefiere la
    más antigua que cumpla las dos reglas:

    1. **Round-robin** (AGENT.md 3.3): su autor no es el último que tuvo ventana,
       para que nadie encadene turnos consecutivos.
    2. **Cuota de turnos**: su autor no se llevó más de ``max_share`` de las
       últimas ``sample`` ventanas.

    Si ninguna ley cumple las dos, se relaja la cuota antes que el round-robin, y
    en última instancia se devuelve la más antigua. **La cola nunca se bloquea**:
    una cuota que pudiera dejar al sistema sin abrir ventanas sería un modo de
    denegación de servicio más barato que el que intenta evitar.

    ``recent_authors`` son los autores de las ventanas pasadas, de la más
    reciente a la más vieja. Sin ese dato la cuota no se aplica y el
    comportamiento es el round-robin de siempre.

    **Lo que esta cuota NO hace.** No cierra Sybil: un atacante que firma cada
    ley con una identidad nueva nunca acumula cuota, porque la cuota es por
    identidad y generar identidades es gratis (AGENT.md 9, limitación aceptada y
    documentada). Lo que sí acota es el monopolio de *una* identidad, que es el
    caso barato y el único que se puede frenar sin verificación de identidad
    real —explícitamente fuera de alcance por AGENT.md 10—.
    """
    if not pending_laws:
        return None

    excedidos = authors_over_quota(recent_authors or [],
                                   max_share=max_share, sample=sample)

    for law in pending_laws:
        autor = turn_holder(law)
        if autor != last_author and autor not in excedidos:
            return law

    # Nadie cumple las dos. Se cede primero la cuota: el round-robin es la regla
    # del enunciado y la cuota es la protección agregada, así que ante el
    # conflicto manda la primera.
    if last_author is not None:
        for law in pending_laws:
            if turn_holder(law) != last_author:
                return law

    return pending_laws[0]
