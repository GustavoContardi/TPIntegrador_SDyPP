"""Elección Bully-by-effort para Pool Coordinator vía Redis.

Protocolo:
1. Todos los candidatos sin líder calculan el mismo seed:
       seed = f"{last_block_hash}:{epoch}"
   donde epoch = floor(time() / ELECTION_EPOCH_SECONDS).
2. Cada candidato resuelve el mini-PoW localmente (sin coordinación).
3. El primero en resolver intenta SET NX ``pool:election:{pool_id}:{epoch}``.
   Solo uno puede ganar (Redis garantiza la atomicidad).
4. El ganador adquiere el lease ``pool:leader:{pool_id}`` con SET (sin NX, ya
   arbitrado por el PoW + el claim atómico).

Si la época cambia durante el PoW (cómputo muy lento), el candidato
descarta el nonce y la siguiente llamada desde tick() usará la época
actual.

**Las dos claves van namespaceadas por ``pool_id``, y eso es esencial.** La
elección arbitra *quién coordina un pool dado* entre candidatos intercambiables
de ese mismo pool; no arbitra entre pools distintos, que son organizaciones
independientes y compiten por el nonce de la ventana, no por un lease. Con una
clave global —como estaba— el coordinador del primer equipo se quedaba con el
lease y ningún otro equipo podía tomar el suyo: los demás se quedaban sin emitir
keepalive y sin figurar en las métricas, pisándose la elección entre sí.
"""

from __future__ import annotations

import logging
import time

from common.blockchain.challenge import solve_mini_challenge

log = logging.getLogger("voxchain.pool.election")

ELECTION_EPOCH_SECONDS = 30


def lease_key_for(pool_id: str) -> str:
    """Lease de coordinador **de este pool**. Ver el docstring del módulo."""
    return f"pool:leader:{pool_id}"


def election_key_for(pool_id: str, epoch: int) -> str:
    """Claim atómico de la elección de este pool en esta época."""
    return f"pool:election:{pool_id}:{epoch}"


# -- rango del lease -------------------------------------------------------
#
# Dos coordinadores de modos distintos pueden terminar sirviendo al mismo pool:
# basta con que el `POOL_ID` de un pod en `pool-auto` coincida con el
# `worker_id` del coordinador de un equipo, y ambos derivan el mismo
# `pool:leader:<id>`. Compartir ese lease es deliberado —es lo que evita que
# coordinen en paralelo sin enterarse—, pero los dos coordinadores **no son
# intercambiables**: a uno lo designó una persona desde la pantalla de Equipos
# y el otro salió de una elección entre nodos anónimos. Sin rango, quién se
# queda con el pool lo decidía el orden de arranque, y un nodo cualquiera podía
# desalojar al coordinador que el dueño del equipo había elegido a dedo.
#
# El rango hace explícita esa asimetría: el designado desplaza al electo, nunca
# al revés. No es un namespace aparte a propósito —separarlos devolvería el
# problema de los dos coordinadores simultáneos que este lease vino a resolver.
LEASE_RANK_DESIGNATED = "designated"
LEASE_RANK_ELECTED = "elected"

_RANK_ORDER = {LEASE_RANK_ELECTED: 0, LEASE_RANK_DESIGNATED: 1}
_LEASE_SEP = "|"


def encode_lease(holder: str, rank: str) -> str:
    """Valor a guardar en el lease: ``<rango>|<dueño>``."""
    return f"{rank}{_LEASE_SEP}{holder}"


def decode_lease(raw) -> tuple[str, str]:
    """``(rango, dueño)`` de un valor de lease; acepta ``bytes`` y ``None``.

    Un valor sin rango viene de una versión anterior a este campo (o de un
    coordinador todavía sin actualizar, durante un rollout). Se lee como
    ``designated`` a propósito: ante un dueño que no sabemos clasificar, la
    opción segura es no desplazarlo.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if raw is None:
        return LEASE_RANK_DESIGNATED, ""
    rank, sep, holder = raw.partition(_LEASE_SEP)
    if not sep or rank not in _RANK_ORDER:
        return LEASE_RANK_DESIGNATED, raw
    return rank, holder


def lease_holder(raw) -> str:
    """Sólo el dueño; azúcar para los sitios que no miran el rango."""
    return decode_lease(raw)[1]


def outranks(rank: str, other: str) -> bool:
    """¿``rank`` puede desplazar a ``other``? Sólo si es **estrictamente** mayor.

    El empate no desplaza, y es la parte que importa: dos nodos del mismo rango
    sirviendo al mismo pool se robarían el lease en cada tick, en bucle, en vez
    de que uno se retire.
    """
    return _RANK_ORDER.get(rank, 1) > _RANK_ORDER.get(other, 1)


def run_pool_election(
    redis,
    pool_id: str,
    *,
    n_zeros: int = 2,
    lease_key: str | None = None,
    lease_ttl: int = 10,
    lease_rank: str = LEASE_RANK_DESIGNATED,
    epoch_duration: int = ELECTION_EPOCH_SECONDS,
    clock=time.time,
) -> bool:
    """Participa en la elección de Pool Coordinator de ``pool_id``.

    Devuelve True si este candidato ganó y adquirió el liderazgo del pool.

    ``lease_key`` se puede pasar explícita para casos raros, pero el default
    —derivado de ``pool_id``— es el correcto: el claim de la elección y el lease
    tienen que estar en el **mismo** namespace, o dos pools se pisarían el claim
    aunque cada uno guardara su lease por separado.
    """
    lease_key = lease_key or lease_key_for(pool_id)
    epoch = int(clock() / epoch_duration)
    election_key = election_key_for(pool_id, epoch)

    if redis.get(election_key) is not None:
        log.debug("pool %s: elección %d ya resuelta, retirándose", pool_id, epoch)
        return False

    last_hash = (redis.lindex("chain", -1) or "genesis")
    seed = f"{last_hash}:{epoch}"

    log.info("pool %s: resolviendo mini-PoW (seed=…%s, %d ceros, epoch=%d)",
             pool_id, seed[-8:], n_zeros, epoch)

    nonce = solve_mini_challenge(seed, n_zeros)
    if nonce is None:
        log.warning("pool %s: no se pudo resolver el mini-PoW", pool_id)
        return False

    # Si la época cambió durante el cómputo, el claim sería para una época vieja.
    if int(clock() / epoch_duration) != epoch:
        log.info("pool %s: época cambió durante el PoW, abortando", pool_id)
        return False

    # Claim atómico: solo el primero en llegar gana.
    won = bool(redis.set(election_key, pool_id, nx=True, ex=lease_ttl * 3))
    if not won:
        log.info("pool %s: perdió el claim atómico (otro candidato más rápido)", pool_id)
        return False

    redis.set(lease_key, encode_lease(pool_id, lease_rank), ex=lease_ttl)
    log.info("pool %s: ¡GANÓ la elección! (nonce=%d, epoch=%d)", pool_id, nonce, epoch)
    return True
