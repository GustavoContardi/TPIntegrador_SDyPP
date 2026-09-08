"""Cliente de estado de VoxChain sobre Redis (AGENT.md 7).

Esquema de claves (namespaced):

- ``law:<id>``            hash con la ley (7.1), incluida su ``category``
- ``window:<id>``         hash con la ventana de votación (7.2)
- ``block:<hash>``        hash con el bloque (7.3)
- ``cooldown:<pubkey>``   hash con el cooldown del autor (7.4)
- ``chain``               lista ordenada de ``block_hash`` (la cadena)
- ``active_window``       clave única con el ``voting_window_id`` vigente
- ``window_sealed:<id>``  guard atómico de cierre (primer nonce válido gana, BUG 2)
- ``window_counter``      contador monótono de ventanas abiertas (base del cooldown)
- ``law_queue``           lista de ``law_id`` en estado ``pending_queue``
- ``discarded_text_hashes`` set de ``text_hash`` descartados (detección de reproposición)
- ``node:owner:<node_pubkey>`` ciudadano dueño de un nodo minero/pool (3.1)

Se asume un cliente Redis con ``decode_responses=True`` (valores como ``str``).
Las claves privadas de los individuos **nunca** se persisten (AGENT.md 10):
sólo circula ``author_pubkey``.
"""

from __future__ import annotations

import json
from typing import Optional

from common.blockchain.block import Block, GENESIS_PREVIOUS_HASH
from common.blockchain.categories import DEFAULT_CATEGORY, normalize_category


class LawStatus:
    PENDING_QUEUE = "pending_queue"
    IN_WINDOW = "in_window"
    PROMULGATED = "promulgated"
    DISCARDED = "discarded"
    REPEALED = "repealed"


class WindowResult:
    SUCCESS = "success"
    EXPIRED_PENDING = "expired_pending"


class CooldownReason:
    PROPOSED_NEW = "proposed_new"
    REPROPOSED_IDENTICAL = "reproposed_identical"


def connect_redis(url: str, **kwargs):
    """Crea un cliente redis-py a partir de una URL (``redis://host:port/db``)."""
    import redis  # import diferido: el paquete common no debe exigir redis en tests puros

    return redis.Redis.from_url(url, decode_responses=True, **kwargs)


def _clean(mapping: dict) -> dict:
    """Redis no admite valores None en un hash; se omiten esos campos."""
    return {k: v for k, v in mapping.items() if v is not None}


class VoxChainStore:
    """Fachada de persistencia. Recibe un cliente Redis (real o fakeredis)."""

    def __init__(self, client):
        self.r = client

    # ---- conexión / salud -------------------------------------------------
    def ping(self) -> bool:
        try:
            return bool(self.r.ping())
        except Exception:
            return False

    # ---- leyes (7.1) ------------------------------------------------------
    def save_law(self, *, law_id: str, author_pubkey: str, text_hash: str,
                 created_at: str, status: str = LawStatus.PENDING_QUEUE,
                 action: str = "promulgacion",
                 category: str = DEFAULT_CATEGORY,
                 text_ref: Optional[str] = None,
                 text_compressed: Optional[str] = None,
                 text_original_len: int = 0) -> None:
        # `action` es la acción que la próxima ventana de esta ley ejecutará.
        # Una derogación reutiliza la ley promulgada existente cambiando su action.
        # `category` es el área de gobierno (AGENT.md 3.10) y NO cambia nunca:
        # se fija al proponer y la derogación hereda la de la ley original, para
        # que derogar convoque a los mismos equipos que promulgaron.
        self.r.hset(f"law:{law_id}", mapping=_clean({
            "law_id": law_id,
            "author_pubkey": author_pubkey,
            "text_hash": text_hash,
            "text_ref": text_ref,
            "text_compressed": text_compressed,
            "text_original_len": str(text_original_len) if text_original_len else None,
            "status": status,
            "action": action,
            "category": normalize_category(category),
            "created_at": created_at,
        }))

    def get_law(self, law_id: str) -> Optional[dict]:
        data = self.r.hgetall(f"law:{law_id}")
        if not data:
            return None
        # Las leyes guardadas antes de que existieran las categorías no tienen el
        # campo; se leen como `general` en vez de romper a quien las consuma.
        data["category"] = normalize_category(data.get("category"))
        return data

    def set_law_status(self, law_id: str, status: str) -> None:
        self.r.hset(f"law:{law_id}", "status", status)

    def set_law_action(self, law_id: str, action: str) -> None:
        self.r.hset(f"law:{law_id}", "action", action)

    # ---- cola de leyes (round-robin lo decide el NCT) ---------------------
    def enqueue_law(self, law_id: str) -> None:
        self.r.rpush("law_queue", law_id)

    def remove_from_queue(self, law_id: str) -> None:
        self.r.lrem("law_queue", 0, law_id)

    def queued_law_ids(self) -> list[str]:
        return self.r.lrange("law_queue", 0, -1)

    def queued_laws(self) -> list[dict]:
        return [law for lid in self.queued_law_ids() if (law := self.get_law(lid))]

    def all_laws(self) -> list[dict]:
        laws = []
        cursor = 0
        while True:
            cursor, keys = self.r.scan(cursor, match="law:*")
            for key in keys:
                law_id = key.split(":", 1)[1]
                law = self.get_law(law_id)
                if law:
                    laws.append(law)
            if cursor == 0:
                break
        return laws

    # ---- detección de reproposición (3.5) ---------------------------------
    def mark_text_hash_discarded(self, text_hash: str) -> None:
        self.r.sadd("discarded_text_hashes", text_hash)

    def is_text_hash_discarded(self, text_hash: str) -> bool:
        return bool(self.r.sismember("discarded_text_hashes", text_hash))

    # ---- contador de ventanas (base del cooldown) -------------------------
    def current_window_number(self) -> int:
        val = self.r.get("window_counter")
        return int(val) if val is not None else 0

    def next_window_number(self) -> int:
        """Incrementa y devuelve el número de la ventana que se está abriendo."""
        return int(self.r.incr("window_counter"))

    # ---- ventanas (7.2) ---------------------------------------------------
    def save_window(self, *, voting_window_id: str, law_id: str, action: str,
                    n_zeros_required: int, opened_at: str, deadline: str,
                    partial_hash_base: str,
                    category: str = DEFAULT_CATEGORY,
                    result: Optional[str] = None,
                    winning_nonce: Optional[int] = None,
                    winning_node_or_pool: Optional[str] = None) -> None:
        # La categoría se copia de la ley a la ventana: es el dato con el que la
        # UI explica por qué un equipo aportó (o no) cómputo a esta ventana, y
        # no queremos que eso dependa de que la ley todavía exista con esa etiqueta.
        self.r.hset(f"window:{voting_window_id}", mapping=_clean({
            "voting_window_id": voting_window_id,
            "law_id": law_id,
            "action": action,
            "n_zeros_required": n_zeros_required,
            "opened_at": opened_at,
            "deadline": deadline,
            "partial_hash_base": partial_hash_base,
            "category": normalize_category(category),
            "result": result,
            "winning_nonce": winning_nonce,
            "winning_node_or_pool": winning_node_or_pool,
        }))

    def get_window(self, voting_window_id: str) -> Optional[dict]:
        data = self.r.hgetall(f"window:{voting_window_id}")
        if not data:
            return None
        data["category"] = normalize_category(data.get("category"))
        return data

    def set_window_result(self, voting_window_id: str, *, result: str,
                          winning_nonce: Optional[int] = None,
                          winning_node_or_pool: Optional[str] = None) -> None:
        self.r.hset(f"window:{voting_window_id}", mapping=_clean({
            "result": result,
            "winning_nonce": winning_nonce,
            "winning_node_or_pool": winning_node_or_pool,
        }))

    # ---- cierre atómico de ventana (BUG 2 / AGENT.md 5 / P2) --------------
    def try_seal_window(self, voting_window_id: str, winning_node_or_pool: str,
                        *, ttl: int = 3600) -> bool:
        """Reclama el cierre de una ventana de forma atómica (SETNX).

        El **primer** nonce válido recibido para ``voting_window_id`` gana el
        cierre; las soluciones tardías ven la clave ``window_sealed:<id>`` ya
        puesta y obtienen ``False`` (se descartan, AGENT.md 5). El guard vive en
        Redis para ser autoritativo ante un failover (un NCT distinto retomando),
        no sólo en el estado en memoria del proceso. La clave expira tras ``ttl``
        para no acumular entradas indefinidamente.
        """
        acquired = self.r.set(f"window_sealed:{voting_window_id}",
                              winning_node_or_pool, nx=True, ex=ttl)
        return bool(acquired)

    def get_window_sealer(self, voting_window_id: str) -> Optional[str]:
        return self.r.get(f"window_sealed:{voting_window_id}")

    # ---- ventana activa (estado único) ------------------------------------
    def set_active_window(self, voting_window_id: str) -> None:
        self.r.set("active_window", voting_window_id)

    def get_active_window(self) -> Optional[str]:
        return self.r.get("active_window")

    def clear_active_window(self) -> None:
        self.r.delete("active_window")

    # ---- último autor (round-robin, AGENT.md 3.3) -------------------------
    def set_last_author(self, author_pubkey: str) -> None:
        self.r.set("nct:last_author", author_pubkey)

    def get_last_author(self) -> Optional[str]:
        return self.r.get("nct:last_author")

    # ---- cooldowns (7.4) --------------------------------------------------
    def set_cooldown(self, author_pubkey: str, cooldown_until_window: int,
                     reason: str) -> None:
        self.r.hset(f"cooldown:{author_pubkey}", mapping={
            "author_pubkey": author_pubkey,
            "cooldown_until_window": cooldown_until_window,
            "cooldown_reason": reason,
        })

    def get_cooldown(self, author_pubkey: str) -> Optional[dict]:
        data = self.r.hgetall(f"cooldown:{author_pubkey}")
        return data or None

    def is_in_cooldown(self, author_pubkey: str) -> bool:
        """True si el autor todavía no alcanzó su ``cooldown_until_window``."""
        cd = self.get_cooldown(author_pubkey)
        if not cd:
            return False
        return self.current_window_number() < int(cd["cooldown_until_window"])

    # ---- bloques y cadena (7.3) -------------------------------------------

    # Script Lua que hace HSET + RPUSH de forma atómica solo si el tip actual
    # de la cadena coincide con previous_hash (CAS sobre el tip). Esto evita
    # que dos NCT en split-brain encadenen bloques del mismo previous_hash
    # produciendo un fork (A-04).
    #
    # KEYS[1] = "chain"
    # KEYS[2] = "block:<hash>"
    # ARGV[1] = previous_hash esperado
    # ARGV[2] = nuevo block_hash
    # ARGV[3..] = pares campo-valor para HSET
    #
    # Retorna 1 si el append se realizó, 0 si el tip no coincidió.
    _APPEND_BLOCK_LUA = """
local tip = redis.call('LINDEX', KEYS[1], -1)
local expected = ARGV[1]
if tip ~= false and tip ~= expected then
    return 0
end
local fields = {}
for i = 3, #ARGV do
    fields[#fields + 1] = ARGV[i]
end
redis.call('HSET', KEYS[2], unpack(fields))
redis.call('RPUSH', KEYS[1], ARGV[2])
return 1
"""

    def append_block(self, block: Block) -> bool:
        """Agrega un bloque a la cadena con CAS atómico sobre el tip.

        Devuelve True si el bloque se agregó, False si el tip de la cadena
        ya no era block.previous_hash (otro NCT se adelantó durante split-brain).
        """
        block_data = {k: ("" if v is None else str(v))
                      for k, v in block.to_dict().items()}
        flat_fields = [item for pair in block_data.items() for item in pair]
        result = self.r.eval(
            self._APPEND_BLOCK_LUA, 2,
            "chain",
            f"block:{block.block_hash}",
            block.previous_hash,
            block.block_hash,
            *flat_fields,
        )
        return bool(result)

    def get_block(self, block_hash: str) -> Optional[Block]:
        data = self.r.hgetall(f"block:{block_hash}")
        return Block.from_dict(data) if data else None

    def chain_hashes(self) -> list[str]:
        return self.r.lrange("chain", 0, -1)

    def get_chain(self) -> list[Block]:
        return [b for h in self.chain_hashes() if (b := self.get_block(h))]

    def last_block_hash(self) -> str:
        hashes = self.chain_hashes()
        return hashes[-1] if hashes else GENESIS_PREVIOUS_HASH

    def chain_length(self) -> int:
        return self.r.llen("chain")

    # ---- liderazgo del NCT (failover por lease, AGENT.md 4.1) -------------
    #
    # Dos modos de adquisición del lease:
    #
    # 1. try_acquire_leadership (NX): para el arranque inicial. Solo adquiere si
    #    la clave no existe, evitando que dos nodos que arrancan a la vez compitan.
    #
    # 2. elect_acquire_leadership (SET sin NX): para el standby que detectó la
    #    caída. El líder anterior está muerto pero su clave puede seguir viva
    #    dentro del TTL, así que hay que poder sobreescribirla — de ahí el SET
    #    sin NX, acotado por el dead_threshold (ver el docstring del método).
    #    La elección del NCT NO usa PoW ni cola de mensajes (AGENT.md 4.1): entre
    #    réplicas homogéneas el esfuerzo no discrimina, así que arbitra Redis.
    #
    # TTL coherente con el timeout de heartbeat: el leader renueva cada
    # heartbeat_interval (≈3 s); el TTL debe ser mayor que el intervalo pero
    # aproximado al timeout de detección (≈12 s) para que el lease expire si el
    # líder deja de renovar, sin interferir con la adquisición via PoW.
    # Valor por defecto: 20 s (≈ 1.6× el timeout de 12 s, >> el intervalo de 3 s).

    def try_acquire_leadership(self, candidate_id: str, ttl: int = 20) -> bool:
        """Intenta adquirir el liderazgo del NCT vía SETNX (arranque inicial).

        Devuelve True si este candidato ganó la adquisición. El lock expira
        después de ``ttl`` segundos; el líder debe renovarlo con heartbeats.
        """
        acquired = self.r.set("nct:leader", candidate_id, nx=True, ex=ttl)
        return bool(acquired)

    def elect_acquire_leadership(self, candidate_id: str, ttl: int = 20,
                                 dead_threshold: int = 6) -> bool:
        """Adquiere el lease de líder del NCT tras detectar la caída del anterior.

        Aplica tres reglas en orden:
        1. Clave inexistente (lease expiró naturalmente) → adquirir.
        2. Clave == nosotros → renovar TTL (restart tras crash).
        3. Clave == otro candidato:
           - TTL ≤ dead_threshold → el holder está muerto (dejó de renovar) → adquirir.
           - TTL > dead_threshold → otro candidato ganó la elección concurrente → fallar.

        El ``dead_threshold`` debe ser > (LEADER_LEASE_TTL - HEARTBEAT_TIMEOUT) para
        cubrir el TTL restante del líder caído cuando la elección dispara, y <<
        LEADER_LEASE_TTL para no confundirlo con un ganador concurrente recién
        adquirido. Valor seguro: 2 × HEARTBEAT_INTERVAL ≈ 6 s.

        Nota: la implementación es GET + SET, no atómica. En condiciones normales
        sólo un standby llega acá —los demás siguen viendo heartbeats o encuentran
        el lease ya tomado— y el margen entre ambas operaciones es de microsegundos.
        Si dos entraran a la vez, el perdedor lo detecta en su siguiente
        ``renew_leadership`` y ejecuta ``step_down`` (AGENT.md 11.4).
        """
        current = self.r.get("nct:leader")
        if current is None:
            self.r.set("nct:leader", candidate_id, ex=ttl)
            return True
        if current == candidate_id:
            self.r.expire("nct:leader", ttl)
            return True
        remaining = self.r.ttl("nct:leader")
        if remaining >= 0 and remaining <= dead_threshold:
            self.r.set("nct:leader", candidate_id, ex=ttl)
            return True
        return False

    def renew_leadership(self, candidate_id: str, ttl: int = 20) -> bool:
        """Renueva el liderazgo: sólo el líder actual puede extender su TTL."""
        # Usamos una transacción Lua para verificar que seguimos siendo el líder.
        lua = """
        local current = redis.call("GET", "nct:leader")
        if current == ARGV[1] then
            redis.call("EXPIRE", "nct:leader", ARGV[2])
            return 1
        end
        return 0
        """
        ok = self.r.eval(lua, 0, candidate_id, ttl)
        return bool(ok)

    def get_leader(self) -> str | None:
        return self.r.get("nct:leader")

    def clear_leadership(self) -> None:
        self.r.delete("nct:leader")

    # --- Identidad de nodo ↔ ciudadano dueño (AGENT.md 3.1) -----------------
    #
    # Un minero firma sus nonces con una clave **propia**, generada dentro de su
    # propio proceso, no con la del ciudadano que lo registró: así el alta de un
    # minero nunca necesita que una clave privada de individuo viaje a ningún
    # lado. El precio es que ``winning_node_or_pool`` deja de ser la pubkey del
    # ciudadano, y las reglas que hablan del ciudadano (3.4: el autor no gana su
    # propia ventana) necesitan resolver nodo → dueño. Ese es este índice.

    def bind_node_identity(self, node_pubkey: str, owner_pubkey: str) -> None:
        """Vincula la pubkey de un nodo con el ciudadano que lo registró."""
        if node_pubkey and owner_pubkey:
            self.r.set(f"node:owner:{node_pubkey}", owner_pubkey)

    def unbind_node_identity(self, node_pubkey: str) -> None:
        self.r.delete(f"node:owner:{node_pubkey}")

    def owner_of_node(self, node_pubkey: str) -> Optional[str]:
        """Ciudadano dueño de un nodo, o ``None`` si la pubkey no está vinculada.

        ``None`` no es un error: un pool o un minero que no completó el enrolamiento
        firma con una identidad anónima y sigue siendo un participante válido —
        simplemente no se le puede imputar la regla 3.4 a ningún autor.
        """
        if not node_pubkey:
            return None
        return self.r.get(f"node:owner:{node_pubkey}")
