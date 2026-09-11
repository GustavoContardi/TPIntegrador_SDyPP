"""Pool Coordinator embebido: consume desafíos del NCT y los fragmenta.

Un pool es una organización que agrega mineros voluntarios. Desde la perspectiva
del NCT es indistinguible de un worker standalone. Internamente subdivide
el espacio de nonces entre sus miners registrados (HTTP) y su propio auto-miner.

Además es donde se hace efectiva la **agenda temática del equipo** (AGENT.md
3.10): si la ventana en curso es de un área que el equipo no vota, el
coordinador no fragmenta nada, y entonces ni él ni ninguno de sus mineros suma
un solo hash a esa ley. Ése es todo el mecanismo — no hace falta avisarle a cada
minero, porque los mineros sólo pueden trabajar en los fragmentos que el
coordinador reparte.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from threading import Lock

from common.blockchain.categories import (
    covers_category,
    normalize_category,
    parse_categories,
)
from common.blockchain.challenge import prefix_for_zeros
from worker_pkg.pool_coordinator.election import (
    LEASE_RANK_DESIGNATED,
    decode_lease,
    encode_lease,
    lease_holder,
    lease_key_for,
    outranks,
)
from common.metrics import (
    observe_challenge_latency,
    pool_is_leader,
    pool_miners_registered,
    pool_nonces_found_total,
    pool_work_distributed_total,
    worker_busy,
    worker_nonces_found_total,
    worker_tasks_received_total,
)

log = logging.getLogger("voxchain.pool")

KEEPALIVE_TTL = 15.0

# Sin agenda declarada el pool vota todas las ventanas: es el pool clásico, y
# tiene que seguir siendo el comportamiento por defecto (AGENT.md 3.10).
_DEFAULT_POLICY = {"decision": "accept", "categories": []}


def fragment_range(start: int, end: int, fragment_size: int) -> list[tuple[int, int]]:
    if fragment_size <= 0:
        raise ValueError("fragment_size debe ser positivo")
    if end <= start:
        return []
    chunks = []
    cur = start
    while cur < end:
        chunks.append((cur, min(cur + fragment_size, end)))
        cur += fragment_size
    return chunks


class PoolCoordinator:
    def __init__(self, messaging, *, pool_id: str, redis=None, mine,
                 capacity: int = 1, clock=time.time,
                 keepalive_interval: float = 5.0,
                 lease_ttl: int = 10, lease_key: str | None = None,
                 lease_rank: str = LEASE_RANK_DESIGNATED,
                 election_n_zeros: int | None = None,
                 elect_leader: bool = True, on_lost_leadership=None,
                 signer=None):
        self.m = messaging
        self.pool_id = pool_id
        self.signer = signer
        self.redis = redis
        self.capacity = capacity
        self.mine = mine
        self.now = clock
        self.keepalive_interval = keepalive_interval
        self.lease_ttl = lease_ttl
        # Un lease **por pool**: el lease dice "yo soy el coordinador vivo de
        # ESTE pool", no "yo soy el único pool de la red". Con la clave global
        # que había antes, el primer equipo que arrancaba se quedaba con ella y
        # los coordinadores de los demás equipos nunca lograban tomar la suya —
        # se quedaban sin emitir keepalive y sin aparecer en las métricas.
        self.lease_key = lease_key or lease_key_for(pool_id)
        # Quién puede desplazar a quién cuando dos coordinadores de modos
        # distintos sirven al mismo pool (ver `election.py`). El default es
        # `designated` porque el constructor sin este argumento es el del modo
        # `pool-coordinator`: ahí al coordinador lo eligió una persona.
        self.lease_rank = lease_rank
        self._lease_value = encode_lease(pool_id, lease_rank)
        self._miners: dict[str, dict] = {}
        self._pending_fragments: deque[dict] = deque()
        self._lock = Lock()
        self._solved: set[str] = set()
        # La agenda real la baja el backend a `pool:policy:<pool_id>` cuando el
        # dueño del equipo la elige, y se relee en cada tick.
        self._voting_policy = dict(_DEFAULT_POLICY)
        self._last_keepalive = 0.0
        self._last_lease_renew = 0.0
        # ¿Quién decide si este coordinador manda?
        #
        # - `elect_leader=True` (modo `pool-coordinator`): lo decide la elección
        #   por Redis de `election.py`. Arrancamos como follower y competimos.
        # - `elect_leader=False` (modo `pool-auto`): **ya lo decidió el bully por
        #   RabbitMQ**. Arrancamos mandando y no corremos ninguna elección propia,
        #   pero igual sostenemos el lease en Redis: es lo que hace que los dos
        #   mecanismos se vean entre sí en vez de coordinar el mismo pool a la vez.
        # - Sin Redis alcanzable, mandamos y no hay nada que sostener.
        self.elect_leader = elect_leader
        self._on_lost_leadership = on_lost_leadership
        self.is_leader = (redis is None) or not elect_leader
        self._miner_counter = 0
        self._running = False
        self._auto_miner_thread: threading.Thread | None = None
        self.nonce_space = int(os.getenv("NONCE_SPACE", "50000000"))
        self.fragment_size = int(os.getenv("FRAGMENT_SIZE", "1000000"))
        self._election_n_zeros = (
            election_n_zeros
            if election_n_zeros is not None
            else int(os.getenv("POOL_ELECTION_N_ZEROS", "2"))
        )
        self._election_thread: threading.Thread | None = None
        self._election_result = False
        self._election_in_progress = False
        pool_is_leader.set(0)

    def wire(self) -> None:
        self.m.on_challenge(self.handle_challenge)

    def try_acquire_leadership(self) -> bool:
        if self.redis is None:
            self.is_leader = True
            pool_is_leader.set(1)
            return True
        acquired = self.redis.set(self.lease_key, self._lease_value,
                                  nx=True, ex=self.lease_ttl)
        if not acquired:
            acquired = self._try_outrank_holder()
        if acquired:
            self.is_leader = True
            pool_is_leader.set(1)
            log.info("pool coordinator %s adquirió liderazgo", self.pool_id)
        return bool(acquired)

    def _try_outrank_holder(self) -> bool:
        """Toma un lease ocupado si su dueño es de rango menor que el nuestro.

        Sólo se llega acá con el SET NX ya fallado. El empate no desplaza (ver
        `election.outranks`): dos coordinadores del mismo rango en un pool son
        el caso de HA que el lease tiene que arbitrar, y ahí el segundo espera.
        """
        rank, holder = decode_lease(self.redis.get(self.lease_key))
        if holder == self.pool_id or not outranks(self.lease_rank, rank):
            return False
        log.warning("pool coordinator %s (%s) desplaza a %s (%s) del lease %s",
                    self.pool_id, self.lease_rank, holder, rank, self.lease_key)
        self.redis.set(self.lease_key, self._lease_value, ex=self.lease_ttl)
        return True

    def renew_leadership(self) -> bool:
        if self.redis is None:
            return True
        pipe = self.redis.pipeline()
        pipe.get(self.lease_key)
        pipe.pttl(self.lease_key)
        current, _ttl = pipe.execute()
        if current is None:
            self.redis.setex(self.lease_key, self.lease_ttl, self._lease_value)
            return True
        rank, holder = decode_lease(current)
        if holder == self.pool_id:
            self.redis.setex(self.lease_key, self.lease_ttl, self._lease_value)
            return True
        if outranks(self.lease_rank, rank):
            # No es nuestro, pero lo tiene alguien de rango menor: un coordinador
            # designado retomando su pool de manos de uno electo. Renovar acá (en
            # vez de ceder) es lo que impide que un nodo anónimo se quede con el
            # pool de un equipo sólo por haber arrancado primero.
            log.warning("pool coordinator %s (%s) desplaza a %s (%s) del lease %s",
                        self.pool_id, self.lease_rank, holder, rank, self.lease_key)
            self.redis.setex(self.lease_key, self.lease_ttl, self._lease_value)
            return True
        self.is_leader = False
        pool_is_leader.set(0)
        pool_miners_registered.set(0)
        log.warning("pool coordinator %s perdió el lease %s: lo tiene %s",
                    self.pool_id, self.lease_key, current)
        # Avisar a quien nos arrancó. Con la elección por Redis alcanza con dejar
        # de emitir keepalive (el tick se encarga), pero con arbitraje externo
        # (bully) el dueño tiene que enterarse: si no, sigue creyéndose
        # coordinador, sirviendo HTTP y fragmentando en paralelo al que sí tiene
        # el lease. Ése era justamente el caso de "dos coordinadores del mismo
        # pool sin que ninguno lo detecte".
        if self._on_lost_leadership is not None:
            try:
                self._on_lost_leadership()
            except Exception:  # noqa: BLE001
                log.exception("pool %s: error avisando la pérdida de liderazgo",
                              self.pool_id)
        return False

    def register_miner(self, capacity: int = 1, has_gpu: bool = False) -> str:
        with self._lock:
            self._miner_counter += 1
            mid = f"{self.pool_id}-miner-{self._miner_counter}"
            self._miners[mid] = {
                "capacity": capacity,
                "has_gpu": has_gpu,
                "last_seen": self.now(),
                "busy": False,
            }
            pool_miners_registered.set(len(self._miners))
            log.info("miner %s registrado (capacity=%d, gpu=%s)", mid, capacity, has_gpu)
            return mid

    def handle_heartbeat(self, miner_id: str) -> bool:
        with self._lock:
            if miner_id in self._miners:
                self._miners[miner_id]["last_seen"] = self.now()
                return True
            return False

    def _fresh_miners(self) -> list[tuple[str, dict]]:
        cutoff = self.now() - KEEPALIVE_TTL
        return [(mid, info) for mid, info in self._miners.items()
                if info["last_seen"] >= cutoff]

    def _purge_stale_miners(self) -> None:
        cutoff = self.now() - KEEPALIVE_TTL
        stale = [mid for mid, info in self._miners.items()
                 if info["last_seen"] < cutoff]
        for mid in stale:
            del self._miners[mid]
        pool_miners_registered.set(len(self._miners))
        if stale:
            log.debug("miners stale eliminados: %s", stale)

    def get_next_task(self, miner_id: str) -> dict | None:
        with self._lock:
            if miner_id not in self._miners:
                return None
            if not self._pending_fragments:
                return None
            fragment = self._pending_fragments.popleft()
            pool_work_distributed_total.inc()
            return {
                "voting_window_id": fragment["voting_window_id"],
                "law_id": fragment.get("law_id"),
                "action": fragment.get("action"),
                "category": fragment.get("category"),
                "partial_hash_base": fragment["partial_hash_base"],
                "n_zeros_required": fragment.get("n_zeros_required"),
                "range_min": fragment["range_min"],
                "range_max": fragment["range_max"],
            }

    def _get_auto_miner_fragment(self) -> dict | None:
        with self._lock:
            if not self._pending_fragments:
                return None
            return self._pending_fragments.popleft()

    def _discard_fragments(self, wid: str) -> int:
        """Saca de la cola los fragmentos pendientes de la ventana ``wid``."""
        with self._lock:
            quedan = deque(f for f in self._pending_fragments
                           if f.get("voting_window_id") != wid)
            descartados = len(self._pending_fragments) - len(quedan)
            self._pending_fragments = quedan
        if descartados:
            log.info("pool %s descartó %d fragmentos de la ventana %s",
                     self.pool_id, descartados, wid)
        return descartados

    def submit_result(self, miner_id: str, result: dict) -> bool:
        wid = result.get("voting_window_id")
        nonce = result.get("nonce")
        hash_hex = result.get("block_hash_candidato")
        if not wid or nonce is None:
            return False
        if wid in self._solved:
            return False
        self._solved.add(wid)
        # La ventana ya está ganada: los fragmentos que quedaron sin repartir
        # sólo generan trabajo inútil. Si no se descartan, los mineros siguen
        # barriendo el espacio de nonces de una ventana cerrada y recién
        # atienden la siguiente cuando terminan, lo que retrasa el sellado de
        # cada ley por el resto de la cola (con NONCE_SPACE grande son cientos
        # de fragmentos).
        self._discard_fragments(wid)
        pool_nonces_found_total.inc()
        winner = self.signer.identity(self.pool_id) if self.signer else self.pool_id
        payload = {
            "voting_window_id": wid,
            "nonce": nonce,
            "winning_node_or_pool": winner,
            "block_hash_candidato": hash_hex,
        }
        if self.signer and self.signer.enabled:
            payload["signature"] = self.signer.sign_nonce(wid, nonce, winner)
        self.m.publish_nonce_response(payload)
        log.info("pool %s publicó nonce %d para ventana %s", self.pool_id, nonce, wid)
        return True

    def set_voting_policy(self, policy: dict) -> None:
        decision = policy.get("decision", "accept")
        if decision not in ("accept", "reject"):
            raise ValueError(f"decision inválida: {decision}")
        policy = dict(policy)
        # Las categorías desconocidas se descartan en vez de tirar: la política
        # puede venir de un backend más nuevo que este worker, y quedarse sin
        # minar por un slug que no reconocemos sería peor que ignorarlo.
        policy["categories"] = parse_categories(policy.get("categories"))
        self._voting_policy = policy
        log.info("pool %s política de voto: %s", self.pool_id, policy)

    def voting_categories(self) -> list[str]:
        """Agenda vigente del pool; vacía significa "vota todas"."""
        return parse_categories(self._voting_policy.get("categories"))

    def _check_voting_policy(self, challenge: dict) -> bool:
        policy = self._voting_policy
        # Primero la agenda temática: es la decisión política del equipo y no
        # depende de `decision`, que sigue siendo el veto puntual de siempre
        # (rechazar todas las derogaciones, o una ley concreta).
        if not covers_category(policy.get("categories"),
                               challenge.get("category")):
            return False
        if policy.get("decision", "accept") == "accept":
            return True
        if policy.get("action") and challenge.get("action") == policy["action"]:
            return False
        if policy.get("law_id") and challenge.get("law_id") == policy["law_id"]:
            return False
        if not policy.get("action") and not policy.get("law_id"):
            return False
        return True

    def handle_challenge(self, challenge: dict) -> None:
        if not self._running:
            return
        wid = challenge.get("voting_window_id")
        if not wid or wid in self._solved:
            return
        deadline_str = challenge.get("deadline", "")
        if deadline_str:
            try:
                deadline_ts = datetime.fromisoformat(deadline_str).timestamp()
                if self.now() > deadline_ts:
                    log.info("pool %s ventana %s ya venció, saltando", self.pool_id, wid)
                    return
            except (ValueError, TypeError):
                pass
        if not self._check_voting_policy(challenge):
            log.info("pool %s no aporta cómputo a la ventana %s (categoría %s, "
                     "agenda %s)", self.pool_id, wid,
                     normalize_category(challenge.get("category")),
                     self.voting_categories() or "todas")
            return
        observe_challenge_latency(challenge, self.now())
        worker_tasks_received_total.inc()
        worker_busy.set(1)
        # Ídem standalone: el espacio de esta ventana lo fija el NCT junto con
        # la dificultad. Fragmentar sobre el valor de entorno dejaría al pool
        # barriendo un rango donde la solución no está.
        espacio = int(challenge.get("nonce_space") or self.nonce_space)
        chunks = fragment_range(0, espacio, self.fragment_size)
        log.info("pool %s desafío %s fragmentado en %d tareas (espacio %d)",
                 self.pool_id, wid, len(chunks), espacio)
        # El NCT tiene una sola ventana activa por vez, así que lo que haya
        # quedado de otra ventana ya no sirve. Cubre el caso de una ventana que
        # venció sin ganador (ahí no pasa por submit_result).
        with self._lock:
            otras_ventanas = {f.get("voting_window_id")
                              for f in self._pending_fragments
                              if f.get("voting_window_id") != wid}
        for vieja in otras_ventanas:
            self._discard_fragments(vieja)
        with self._lock:
            for rmin, rmax in chunks:
                self._pending_fragments.append({
                    "voting_window_id": wid,
                    "law_id": challenge.get("law_id"),
                    "action": challenge.get("action"),
                    "category": normalize_category(challenge.get("category")),
                    "partial_hash_base": challenge["partial_hash_base"],
                    "n_zeros_required": challenge.get("n_zeros_required"),
                    "range_min": rmin,
                    "range_max": rmax,
                })

    def _auto_mine_loop(self) -> None:
        while self._running:
            fragment = self._get_auto_miner_fragment()
            if fragment:
                wid = fragment["voting_window_id"]
                base = fragment["partial_hash_base"]
                prefix = prefix_for_zeros(int(fragment.get("n_zeros_required", 4)))
                rmin = fragment["range_min"]
                rmax = fragment["range_max"]
                log.info("pool %s auto-minando ventana %s rango [%d, %d)",
                         self.pool_id, wid, rmin, rmax)
                nonce, hash_hex = self.mine(base, prefix, rmin, rmax)
                if nonce is not None:
                    self.submit_result(self.pool_id, {
                        "voting_window_id": wid,
                        "nonce": nonce,
                        "block_hash_candidato": hash_hex,
                    })
                    log.info("pool %s auto-miner nonce %d para ventana %s",
                             self.pool_id, nonce, wid)
            else:
                time.sleep(0.5)

    def emit_keepalive(self) -> None:
        fresh = self._fresh_miners()
        total_capacity = sum(m["capacity"] for _, m in fresh)
        has_gpu = any(m["has_gpu"] for _, m in fresh)
        self.m.publish_keepalive({
            "worker_id": self.pool_id,
            "capacity": max(total_capacity, self.capacity),
            "has_gpu": has_gpu,
            "ts": self.now(),
        })
        log.debug("pool %s keepalive: %d miners, capacity %d, gpu=%s",
                  self.pool_id, len(fresh), total_capacity, has_gpu)
        if self.redis is not None:
            try:
                import json
                health = {
                    "pool": "ok",
                    "rabbitmq": "ok",
                    "miners": len(fresh),
                    "voting_policy": self._voting_policy,
                }
                self.redis.set(f"pool:health:{self.pool_id}", json.dumps(health), ex=15)
            except Exception:
                pass

    def start(self) -> None:
        self._running = True
        self._auto_miner_thread = threading.Thread(target=self._auto_mine_loop, daemon=True)
        self._auto_miner_thread.start()

    def release_leadership(self) -> None:
        """Suelta el lease al dejar de coordinar, en vez de esperar el TTL.

        Sin esto, un coordinador que se apaga ordenadamente deja su pool sin
        nadie durante lo que queda del TTL (hasta 10 s) aunque haya un sucesor
        listo. Sólo borra la clave **si es nuestra**: entre el GET y el DELETE
        hay una ventana mínima en la que el lease podría haber cambiado de dueño,
        acotada por el propio TTL y muy preferible a no soltarlo nunca.
        """
        if self.redis is None or not self.is_leader:
            return
        try:
            if lease_holder(self.redis.get(self.lease_key)) == self.pool_id:
                self.redis.delete(self.lease_key)
                log.info("pool %s soltó el lease %s", self.pool_id, self.lease_key)
        except Exception:  # noqa: BLE001
            log.debug("no se pudo soltar el lease %s", self.lease_key, exc_info=True)
        finally:
            self.is_leader = False
            pool_is_leader.set(0)

    def stop(self) -> None:
        self._running = False
        self.release_leadership()
        if self._auto_miner_thread:
            self._auto_miner_thread.join(timeout=5)
            self._auto_miner_thread = None

    def _run_election(self) -> None:
        from worker_pkg.pool_coordinator.election import run_pool_election
        try:
            won = run_pool_election(
                self.redis,
                self.pool_id,
                n_zeros=self._election_n_zeros,
                lease_key=self.lease_key,
                lease_ttl=self.lease_ttl,
                lease_rank=self.lease_rank,
                clock=self.now,
            )
            self._election_result = won
        except Exception:
            log.exception("pool %s: error inesperado durante la elección", self.pool_id)
            self._election_result = False
        finally:
            self._election_in_progress = False

    def _maybe_start_election(self) -> None:
        if self.redis is None:
            return
        if self._election_in_progress:
            return
        rank, holder = decode_lease(self.redis.get(self.lease_key))
        # Que el lease esté ocupado sólo nos frena si su dueño no es de rango
        # menor: contra uno menor sí competimos, porque ganar la elección es el
        # camino por el que un coordinador designado recupera su pool.
        if holder and holder != self.pool_id and not outranks(self.lease_rank, rank):
            return
        self._election_in_progress = True
        self._election_result = False
        t = threading.Thread(target=self._run_election, daemon=True,
                             name=f"pool-election-{self.pool_id}")
        self._election_thread = t
        t.start()

    def _sync_voting_policy(self) -> None:
        """Relee la política de voto que el backend dejó en Redis.

        Se normaliza **antes** de comparar con la vigente: si comparáramos el
        JSON crudo, un orden distinto de categorías o un ``decision`` implícito
        harían parecer que cambió en cada tick y ensuciarían el log cada 3 s.
        """
        try:
            import json

            raw = self.redis.get(f"pool:policy:{self.pool_id}")
            if not raw:
                # Sin clave = sin agenda. Volvemos al default en vez de conservar
                # la última que vimos: borrar la clave es la forma obvia de decir
                # "este pool ya no tiene agenda", y quedarnos con la vieja dejaba
                # al coordinador filtrando por algo que ya nadie pidió.
                if self._voting_policy != _DEFAULT_POLICY:
                    log.info("pool %s: sin política en Redis, vuelvo a votar todo",
                             self.pool_id)
                    self._voting_policy = dict(_DEFAULT_POLICY)
                return
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            policy = json.loads(raw)
            if not isinstance(policy, dict):
                return
            policy = dict(policy)
            policy.setdefault("decision", "accept")
            policy["categories"] = parse_categories(policy.get("categories"))
            if policy != self._voting_policy:
                self.set_voting_policy(policy)
        except Exception:  # noqa: BLE001
            log.debug("no se pudo leer pool:policy:%s", self.pool_id, exc_info=True)

    def tick(self) -> None:
        now = self.now()
        self._purge_stale_miners()

        # Recoger resultado de elección completada (solo si Redis disponible)
        if self.redis is not None:
            # Sincronizar política de voto desde Redis. NO se condiciona al
            # liderazgo: `pool:policy:<pool_id>` es de este pool, y quien decide
            # si se fragmenta una ventana es `handle_challenge`, que tampoco mira
            # el lease. Gatearlo por is_leader dejaba a un coordinador sin lease
            # minando con la agenda por defecto (todas las categorías) en vez de
            # con la que su equipo eligió.
            self._sync_voting_policy()

            if (self.elect_leader
                    and not self.is_leader
                    and self._election_thread is not None
                    and not self._election_thread.is_alive()):
                if self._election_result:
                    self.is_leader = True
                    pool_is_leader.set(1)
                    log.info("pool coordinator %s ganó la elección, asumiendo liderazgo",
                             self.pool_id)
                self._election_thread = None

            if now - self._last_lease_renew >= 3.0:
                self._last_lease_renew = now
                if self.is_leader:
                    if not self.renew_leadership():
                        return
                elif self.elect_leader:
                    self._maybe_start_election()

        if not self.is_leader:
            return
        if now - self._last_keepalive >= self.keepalive_interval:
            self.emit_keepalive()
            self._last_keepalive = now
