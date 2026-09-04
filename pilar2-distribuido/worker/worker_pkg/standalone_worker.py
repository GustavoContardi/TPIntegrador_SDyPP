"""Standalone Worker: se suscribe al desafío activo del NCT.

No depende del TrP ni del Pool Coordinator. Mina el espacio completo de
nonces y publica el resultado directamente al NCT.

Puede declarar una agenda temática por entorno (``STANDALONE_CATEGORIES``), el
equivalente individual de la agenda de un equipo (AGENT.md 3.10): sin ella mina
toda ventana que pase, que es el comportamiento por defecto.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from common.blockchain.categories import normalize_category, parse_categories
from common.blockchain.challenge import prefix_for_zeros
from common.metrics import (
    observe_challenge_latency,
    worker_busy,
    worker_has_gpu,
    worker_nonces_found_total,
)

log = logging.getLogger("voxchain.worker.standalone")


class StandaloneWorker:
    def __init__(self, messaging, *, worker_id: str, mine,
                 clock=time.time, signer=None):
        self.m = messaging
        self.worker_id = worker_id
        self.mine = mine
        self.now = clock
        self.signer = signer
        self._solved: set[str] = set()
        self._running = True
        self.nonce_space = int(os.getenv("STANDALONE_NONCE_SPACE", "50000000"))
        rejected = os.getenv("STANDALONE_REJECTED_ACTIONS", "")
        self._rejected_actions = set(a.strip() for a in rejected.split(",") if a)
        if self._rejected_actions:
            log.info("standalone rechaza acciones: %s", self._rejected_actions)
        # Agenda temática del minero individual (AGENT.md 3.10). Vacía = mina
        # todo, que es lo que hace un ciudadano sin facción. Acá es una variable
        # de entorno y no una decisión de UI porque un standalone no tiene
        # equipo que la administre: el que quiere agenda arma o se suma a uno.
        self._categories = parse_categories(os.getenv("STANDALONE_CATEGORIES", ""))
        if self._categories:
            log.info("standalone sólo mina categorías: %s", self._categories)
        log.info("standalone nonce_space=%d", self.nonce_space)

    def wire(self) -> None:
        self.m.on_challenge(self.handle_challenge)

    def handle_challenge(self, challenge: dict) -> None:
        if not self._running:
            return
        wid = challenge.get("voting_window_id")
        if not wid or wid in self._solved:
            return
        observe_challenge_latency(challenge, self.now())
        action = challenge.get("action", "")
        if action in self._rejected_actions:
            log.info("%s rechaza ventana %s (acción=%s)", self.worker_id, wid, action)
            return
        category = normalize_category(challenge.get("category"))
        if self._categories and category not in self._categories:
            log.info("%s no aporta cómputo a la ventana %s (categoría=%s)",
                     self.worker_id, wid, category)
            return
        deadline_str = challenge.get("deadline", "")
        if deadline_str:
            try:
                deadline_ts = datetime.fromisoformat(deadline_str).timestamp()
                if self.now() > deadline_ts:
                    log.info("%s ventana %s ya venció, saltando", self.worker_id, wid)
                    return
            except (ValueError, TypeError):
                pass

        base = challenge["partial_hash_base"]
        prefix = prefix_for_zeros(int(challenge["n_zeros_required"]))
        log.info("%s minando ventana %s rango [0, %d) prefijo %r",
                 self.worker_id, wid, self.nonce_space, prefix)
        worker_busy.set(1)
        nonce, hash_hex = self.mine(base, prefix, 0, self.nonce_space)
        worker_busy.set(0)
        if nonce is None:
            log.info("%s sin solución para ventana %s", self.worker_id, wid)
            return

        self._solved.add(wid)
        worker_nonces_found_total.inc()
        winner = self.signer.identity(self.worker_id) if self.signer else self.worker_id
        payload = {
            "voting_window_id": wid,
            "nonce": nonce,
            "winning_node_or_pool": winner,
            "block_hash_candidato": hash_hex,
        }
        if self.signer and self.signer.enabled:
            payload["signature"] = self.signer.sign_nonce(wid, nonce, winner)
        self.m.publish_nonce_response(payload)
        log.info("%s nonce %d publicado para ventana %s", self.worker_id, nonce, wid)

    def stop(self) -> None:
        self._running = False
