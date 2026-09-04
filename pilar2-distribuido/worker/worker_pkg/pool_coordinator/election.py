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


def run_pool_election(
    redis,
    pool_id: str,
    *,
    n_zeros: int = 2,
    lease_key: str | None = None,
    lease_ttl: int = 10,
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

    redis.set(lease_key, pool_id, ex=lease_ttl)
    log.info("pool %s: ¡GANÓ la elección! (nonce=%d, epoch=%d)", pool_id, nonce, epoch)
    return True
