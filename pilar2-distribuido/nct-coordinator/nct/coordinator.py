"""Núcleo del NCT: cola de leyes, apertura/cierre de ventanas, verificación y sellado.

El NCT gestiona **exclusivamente** ventanas de votación (AGENT.md 3.3, P4): una
sola ventana activa a la vez, orden round-robin por autor, dificultad fija n/n+1,
verificación de nonce contra el desafío y sellado del bloque en Redis. No arbitra
contenido ni ajusta dificultad por carga de red.

Es agnóstico del transporte y del backend: recibe un ``Messaging`` y un
``VoxChainStore``, de modo que el mismo código corre con RabbitMQ+Redis reales o
con el bus en memoria + fakeredis en los tests.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

from common.blockchain import (
    ACTION_DEROGACION,
    ACTION_PROMULGACION,
    build_partial_hash_base,
    espacio_insuficiente,
    n_zeros_for_action,
    seal_block,
    verify_nonce,
)
from common.blockchain.availability import Availability, assess
from common.blockchain.categories import (
    DEFAULT_CATEGORY,
    normalize_category,
    validate_category,
)
from common.blockchain.difficulty import (
    DifficultyRatchet,
    difficulty_for,
    effective_hashrate,
    nonce_space_for,
)
from common.blockchain.challenge import VALID_ACTIONS
from common.identity import nonce_message, proposal_message, verify
from common.messaging import QUEUE_PROPUESTAS, QUEUE_RESPUESTA_NONCE
from common.storage import LawStatus, WindowResult
from common.metrics import (
    nct_blocks_sealed_total,
    nct_is_leader,
    nct_nonce_validation_seconds,
    nct_proposals_total,
    nct_windows_opened_total,
)
from common.queue import TURN_QUOTA_MAX_SHARE, TURN_QUOTA_WINDOWS, turn_holder
from .queue_logic import classify_proposal, cooldown_until, select_next_law

log = logging.getLogger("voxchain.nct")

# Cuánto vale una medición de quórum antes de volver a leer Redis. El tick corre
# a 1 Hz y medir cuesta un scan de `worker:status:*`; los workers reportan cada
# 5 s con TTL de 15 s, así que refrescar más seguido no aporta dato nuevo.
AVAILABILITY_CACHE_SECONDS = 5.0


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class NCTCoordinator:
    def __init__(self, messaging, store, *, n_zeros: int,
                 window_seconds_promulgacion: int, window_seconds_derogacion: int,
                 cooldown_new: int, cooldown_reproposed: int, clock=time.time,
                 nct_id: str = "nct", is_leader: bool = True,
                 heartbeat_interval: float = 0.0, on_stepdown=None,
                 require_signatures: bool = False, proposal_max_age: int = 0,
                 nonce_space: int = 0,
                 dynamic_difficulty: bool = False,
                 difficulty_target_seconds: float = 30.0,
                 difficulty_decay_windows: int = 3,
                 hps_cpu: float = 954_180.0, hps_gpu: float = 5e8,
                 turn_quota_windows: int = TURN_QUOTA_WINDOWS,
                 turn_quota_max_share: float = TURN_QUOTA_MAX_SHARE,
                 min_workers_for_window: int = 0,
                 quorum_by_category: bool = True):
        self.m = messaging
        self.store = store
        self.n_zeros = n_zeros
        self.window_seconds = {
            ACTION_PROMULGACION: window_seconds_promulgacion,
            ACTION_DEROGACION: window_seconds_derogacion,
        }
        self.cooldown_new = cooldown_new
        self.cooldown_reproposed = cooldown_reproposed
        self.now = clock
        self.nct_id = nct_id
        self.is_leader = is_leader
        self.heartbeat_interval = heartbeat_interval
        # Firma de propuestas (A-01, AGENT.md 3.1). require_signatures=False permite
        # migración gradual: verifica si hay firma pero acepta no firmadas.
        self.require_signatures = require_signatures
        self.proposal_max_age = proposal_max_age
        # Cuota de turnos por identidad (ver `common/queue.py`). Acota el
        # monopolio de una identidad sobre las ventanas; no cierra Sybil, que
        # AGENT.md 9 deja explícitamente como limitación aceptada.
        self.turn_quota_windows = turn_quota_windows
        self.turn_quota_max_share = turn_quota_max_share
        # Espacio de nonces base. Con dificultad dinámica lo recalcula cada
        # ventana a partir de `n`; con `n` fijo es el de config y sólo sirve para
        # la verificación de coherencia. 0 = no verificar (los tests construyen
        # el NCT sin config de despliegue).
        self.nonce_space = nonce_space
        self._aviso_dificultad_emitido = False
        # Dificultad dinámica: `n` deja de ser constante y pasa a ser la variable
        # que mantiene constante el *tiempo* de promulgación ante una población
        # de mineros que cambia. Ver `common/blockchain/difficulty.py`.
        self.dynamic_difficulty = dynamic_difficulty
        self.difficulty_target_seconds = difficulty_target_seconds
        self.hps_cpu = hps_cpu
        self.hps_gpu = hps_gpu
        self._ratchet = DifficultyRatchet(difficulty_decay_windows)
        # Quórum de mineros para abrir una ventana (ver
        # `common/blockchain/availability.py`). 0 = desactivado, que es el
        # comportamiento histórico y el default de los tests: sin este gate la
        # ventana se abre aunque no haya nadie del otro lado y la ley termina
        # descartada por una ausencia de infraestructura, no por falta de apoyo.
        self.min_workers_for_window = min_workers_for_window
        # Si el quórum mira la agenda de los equipos. En False sólo protege
        # contra la red vacía y un área que nadie vota vuelve a expirar como
        # veto político (AGENT.md 3.10); ver el docstring de `availability.py`.
        self.quorum_by_category = quorum_by_category
        # Se cachea la **población** (el scan de Redis, que es lo caro) y no el
        # veredicto: el veredicto depende de la ley concreta —área, acción y
        # hasta el law_id, porque un equipo puede vetar una ley puntual— así que
        # cachearlo por área daría la respuesta de otra ley.
        self._poblacion_cache: tuple[float, list, list] | None = None
        self._availability_publicada: tuple | None = None
        self._postergacion_avisada: tuple | None = None
        # Callback invocado al ceder el liderazgo: lo usa el monitor para
        # activarse y empezar a observar heartbeats del nuevo líder.
        self._on_stepdown = on_stepdown

        # Estado en memoria de la ventana activa (no se persiste para recuperación:
        # ante caída del NCT la ventana se pierde, AGENT.md 4).
        self._last_author = store.get_last_author()
        self._active = None  # dict con datos de la ventana en curso, o None
        self._last_heartbeat_pub = 0.0

    # -- registro de handlers ----------------------------------------------
    def wire(self) -> None:
        """Suscribe los handlers según el rol.

        Las colas de trabajo (``propuestas``, ``respuesta_nonce``) se consumen
        **sólo siendo líder** (BUG 1 / AGENT.md 4): un follower que también las
        consumiera competiría con el líder por el reparto round-robin de RabbitMQ
        y se "tragaría" la mitad de los mensajes sin actuar. Mientras es follower
        sólo escucha ``nct.heartbeat`` (vía el monitor); la elección del sucesor
        no viaja por mensajería sino por el lease de Redis (AGENT.md 4.1)."""
        if self.is_leader:
            self._subscribe_work_queues()

    def _subscribe_work_queues(self) -> None:
        self.m.on_proposal(self.handle_proposal)
        self.m.on_nonce_response(self.handle_nonce_response)

    def _unsubscribe_work_queues(self) -> None:
        self.m.unsubscribe(QUEUE_PROPUESTAS)
        self.m.unsubscribe(QUEUE_RESPUESTA_NONCE)

    def consumed_work_queues(self) -> set[str]:
        """Colas de trabajo que este NCT consume hoy (gateadas por liderazgo)."""
        return self.m.consumed_queues() & {QUEUE_PROPUESTAS, QUEUE_RESPUESTA_NONCE}

    # -- flujo 1: propuestas (nodo → NCT) ----------------------------------
    def handle_proposal(self, law: dict) -> None:
        if not self.is_leader:
            log.debug("propuesta ignorada (no somos el líder)")
            return
        author = law.get("author_pubkey")
        text_hash = law.get("text_hash")
        action = law.get("action", ACTION_PROMULGACION)
        law_id = law.get("law_id") or str(uuid.uuid4())
        created_at = law.get("created_at") or _iso(self.now())

        nct_proposals_total.inc()

        if not author or not text_hash:
            log.warning("propuesta inválida (faltan author/text_hash): %s", law)
            return
        if action not in VALID_ACTIONS:
            log.warning("propuesta con action inválida: %s", action)
            return
        # Categoría declarada por el autor (AGENT.md 3.10). Se valida ANTES de la
        # firma para no encolar nada con un área inventada: es el dato que decide
        # qué equipos van a aportar cómputo a esta ventana.
        try:
            declared_category = validate_category(law.get("category"))
        except ValueError as exc:
            log.warning("propuesta rechazada: %s", exc)
            return

        # Firma del autor (A-01 / AGENT.md 3.1): nadie propone en nombre de otro.
        if not self._signature_ok(law, author, action, text_hash, law_id,
                                  created_at, declared_category):
            return

        # Cooldown del autor (3.4): no puede proponer mientras esté en cooldown.
        if self.store.is_in_cooldown(author):
            cd = self.store.get_cooldown(author)
            log.info("propuesta rechazada: autor %s en cooldown hasta ventana %s",
                     author[:12], cd["cooldown_until_window"])
            return

        text_compressed = law.get("text_compressed", "")
        text_original_len = int(law.get("text_original_len", 0))

        if action == ACTION_DEROGACION:
            self._enqueue_derogacion(law_id, author, action)
        else:
            self._enqueue_promulgacion(law_id, author, text_hash, created_at,
                                       declared_category,
                                       text_compressed, text_original_len)

        self.maybe_open_window()

    def _signature_ok(self, law: dict, author: str, action: str, text_hash: str,
                      law_id: str, created_at: str, category: str) -> bool:
        """Valida la firma ECDSA de la propuesta contra ``author`` (A-01).

        - ``require_signatures=False``: si no hay firma, se acepta y se loguea
          (migración); si la hay, debe ser válida.
        - ``require_signatures=True``: rechaza toda propuesta sin firma válida.
        - Anti-replay: si ``proposal_max_age`` > 0, ``created_at`` no puede estar
          fuera de esa ventana respecto del reloj del NCT.
        """
        signature = law.get("signature")
        if not signature:
            if self.require_signatures:
                log.warning("propuesta rechazada: sin firma (autor %s)", author[:12])
                return False
            log.info("propuesta sin firma aceptada (migración, autor %s)", author[:12])
            return True
        msg = proposal_message(author, action, text_hash, law_id, created_at,
                               category)
        if not verify(author, msg, signature):
            log.warning("propuesta rechazada: firma inválida (autor %s)", author[:12])
            return False
        if self.proposal_max_age > 0 and not self._created_at_fresh(created_at):
            log.warning("propuesta rechazada: created_at fuera de ventana (autor %s)",
                        author[:12])
            return False
        return True

    def _nonce_signature_ok(self, voting_window_id: str, nonce, winner: str,
                            signature) -> bool:
        """Valida la firma de una respuesta de nonce (A-01 fase 2).

        Sin firma: rechaza sólo si require_signatures; en migración acepta (el
        PoW se verifica igual con verify_nonce). Con firma: ``winner`` debe ser la
        pubkey que firmó ``voting_window_id|nonce|winner``.
        """
        if not signature:
            if self.require_signatures:
                log.warning("nonce rechazado: sin firma (%s)",
                            (winner or "?")[:12])
                return False
            return True
        msg = nonce_message(voting_window_id, nonce, winner)
        if not verify(winner, msg, signature):
            log.warning("nonce rechazado: firma inválida (%s)", (winner or "?")[:12])
            return False
        return True

    def _citizen_behind(self, node_pubkey: str) -> str:
        """Ciudadano al que se imputa un nodo ganador (para las reglas de 3.4).

        Si el nodo no está vinculado a ningún dueño devuelve la propia pubkey del
        nodo: un participante anónimo se representa a sí mismo.
        """
        try:
            return self.store.owner_of_node(node_pubkey) or node_pubkey
        except AttributeError:
            # Stores viejos/dobles de test sin el índice: comportamiento previo.
            return node_pubkey

    def _created_at_fresh(self, created_at: str) -> bool:
        try:
            ts = datetime.fromisoformat(created_at).timestamp()
        except (ValueError, TypeError):
            return False
        return abs(self.now() - ts) <= self.proposal_max_age

    def _enqueue_promulgacion(self, law_id, author, text_hash, created_at,
                              category, text_compressed="",
                              text_original_len=0) -> None:
        # Reproposición idéntica (3.5): mismo hash de texto que algo descartado.
        reason = classify_proposal(self.store.is_text_hash_discarded(text_hash))
        self.store.save_law(law_id=law_id, author_pubkey=author,
                            text_hash=text_hash, created_at=created_at,
                            status=LawStatus.PENDING_QUEUE,
                            action=ACTION_PROMULGACION,
                            category=category,
                            text_compressed=text_compressed,
                            text_original_len=text_original_len)
        self.store.set_law_requested_by(law_id, author)
        self.store.enqueue_law(law_id)
        until = cooldown_until(self.store.current_window_number(), reason,
                               cooldown_new=self.cooldown_new,
                               cooldown_reproposed=self.cooldown_reproposed)
        self.store.set_cooldown(author, until, reason)
        log.info("ley %s encolada (promulgacion, %s, categoría %s, cooldown→ventana %d)",
                 law_id, reason, category, until)

    def _enqueue_derogacion(self, law_id, author, action) -> None:
        target = self.store.get_law(law_id)
        if not target or target.get("status") != LawStatus.PROMULGATED:
            log.warning("derogacion rechazada: ley %s no existe o no está promulgada",
                        law_id)
            return
        # La derogación reutiliza la ley promulgada cambiando su action; se reencola.
        # La categoría NO se toca: es la de la ley original, aunque el que propone
        # la derogación haya declarado otra. Derogar convoca a los mismos equipos
        # que en su momento promulgaron, que es lo que hace simétrico el juego —
        # si el que deroga pudiera reetiquetar, elegiría el área donde su facción
        # es fuerte y la ajena no mina.
        self.store.set_law_action(law_id, ACTION_DEROGACION)
        # El turno es de quien pide derogar, no del autor original: la ley se
        # reutiliza pero la ventana la pidió otro (ver `queue.turn_holder`).
        self.store.set_law_requested_by(law_id, author)
        self.store.enqueue_law(law_id)
        until = cooldown_until(self.store.current_window_number(),
                               classify_proposal(False),
                               cooldown_new=self.cooldown_new,
                               cooldown_reproposed=self.cooldown_reproposed)
        self.store.set_cooldown(author, until, classify_proposal(False))
        log.info("ley %s encolada para derogacion (cooldown→ventana %d)", law_id, until)

    # -- apertura de ventana (round-robin, dificultad fija) ----------------
    def maybe_open_window(self) -> None:
        if not self.is_leader:
            return  # sólo el líder abre ventanas (el follower no toca la cola)
        if self._active is not None:
            return
        # La cuota mira el pasado reciente: sin este dato `select_next_law`
        # degrada al round-robin de siempre (ver `common/queue.py`).
        recientes = self.store.recent_window_authors(self.turn_quota_windows)
        pendientes = self.store.queued_laws()
        # Las leyes sin red que las mine se saltean, no bloquean la cola: una
        # ley de un área que nadie vota congelaría el parlamento entero, y eso
        # sería una denegación de servicio más barata que la ventana vencida que
        # este gate viene a evitar.
        law = select_next_law(pendientes, self._last_author, recientes,
                              max_share=self.turn_quota_max_share,
                              sample=self.turn_quota_windows,
                              can_open=self._hay_quorum)
        if law is not None:
            self._publicar_disponibilidad(self._quorum(self._como_ventana(law)),
                                          law=law)
            self.open_window(law)
            return
        # Nada abrible. Se informa sobre la ley postergada más antigua —la que
        # el ciudadano está esperando— y, con la cola vacía, sobre la red en
        # general, para que el cliente sepa antes de proponer si su ley va a
        # salir o a quedarse esperando.
        postergada = next((ley for ley in pendientes
                           if not self._hay_quorum(ley)), None)
        quorum = self._quorum(self._como_ventana(postergada) if postergada
                              else {"category": DEFAULT_CATEGORY})
        self._publicar_disponibilidad(quorum, law=postergada)
        if postergada is not None:
            self._avisar_postergacion(postergada, quorum)

    # -- quórum de mineros -------------------------------------------------
    @staticmethod
    def _como_ventana(law: dict) -> dict:
        """La ley vista como la ventana que se abriría para ella.

        Es lo que se le muestra a los mineros en el desafío, y por lo tanto lo
        único con lo que se puede anticipar su decisión. La acción importa tanto
        como el área: una **derogación** conserva la categoría de la ley original
        (AGENT.md 3.10) pero puede estar vetada por equipos que sí minan esa
        área, así que preguntar sólo por la categoría daría un sí falso.
        """
        return {"category": law.get("category"),
                "action": law.get("action", ACTION_PROMULGACION),
                "law_id": law.get("law_id")}

    def _hay_quorum(self, law: dict) -> bool:
        """¿Hay hoy mineros dispuestos a minar la ventana de ``law``?"""
        quorum = self._quorum(self._como_ventana(law))
        return quorum is None or quorum.ok

    def _poblacion(self):
        """Mineros vivos y equipos, cacheados unos segundos. None si no se pudo.

        `maybe_open_window` corre en cada tick (1 Hz) y evalúa cada ley de la
        cola; leer Redis por ley y por tick sería absurdo cuando el dato de fondo
        —los latidos de los mineros— se refresca cada 5 s. Lo que se cachea es la
        lectura, no la conclusión: la conclusión depende de la ley.
        """
        ahora = self.now()
        cache = self._poblacion_cache
        if cache is not None and ahora - cache[0] < AVAILABILITY_CACHE_SECONDS:
            return cache[1], cache[2]
        try:
            workers = self.store.live_workers()
            teams = self.store.teams_composition()
        except Exception:  # noqa: BLE001
            log.exception("no se pudo leer la población de mineros")
            return None
        self._poblacion_cache = (ahora, workers, teams)
        return workers, teams

    def _quorum(self, challenge) -> Availability | None:
        """Mineros dispuestos a minar esta ventana, o None si no se evalúa.

        ``challenge`` lleva ``category``, ``action`` y ``law_id``: las tres
        hacen falta porque un equipo puede vetar un área, una acción entera
        —todas las derogaciones— o una ley puntual, y cualquiera de las tres lo
        deja fuera. Con ``min_workers_for_window <= 0`` el gate está desactivado.

        Si medir falla se devuelve None, es decir **se abre igual**. Es la misma
        decisión que toma la dificultad dinámica ante un Redis caído: una falla
        de observación no puede paralizar el gobierno, porque eso convierte
        cualquier hipo de la infraestructura en una denegación de servicio.
        """
        if self.min_workers_for_window <= 0:
            return None
        poblacion = self._poblacion()
        if poblacion is None:
            return None
        workers, teams = poblacion
        try:
            return assess(workers, teams, challenge,
                          minimum=self.min_workers_for_window,
                          by_category=self.quorum_by_category)
        except Exception:  # noqa: BLE001
            log.exception("no se pudo medir el quórum de mineros; abro igual")
            return None

    def _avisar_postergacion(self, law: dict, quorum: Availability) -> None:
        """Loguea que la ley queda en cola, una vez por situación y no por tick.

        El aviso se rearma cuando cambia la ley postergada o el recuento de
        mineros: la transición es lo informativo. A 1 Hz, repetirlo en cada tick
        ahogaría el log justo cuando hay que leerlo para entender por qué el
        sistema no avanza.
        """
        firma = (law.get("law_id"), quorum.category, quorum.eligible, quorum.live)
        if firma == self._postergacion_avisada:
            return
        self._postergacion_avisada = firma
        log.warning("ley %s pospuesta: %s — queda en cola hasta que haya red",
                    law.get("law_id"), quorum.reason())

    def _publicar_disponibilidad(self, quorum: Availability | None,
                                 *, law: dict | None) -> None:
        """Deja el veredicto en Redis para que el API se lo explique al ciudadano.

        Escribe sólo cuando el veredicto cambia: a 1 Hz esto sería un hset por
        segundo para decir lo mismo. ``since`` conserva el momento en que empezó
        la indisponibilidad actual, que es el dato que la UI necesita para decir
        hace cuánto está caído el sistema.
        """
        if quorum is None:
            return
        try:
            encoladas = len(self.store.queued_law_ids())
        except Exception:  # noqa: BLE001
            encoladas = 0
        firma = (quorum.ok, quorum.category, quorum.live, quorum.eligible,
                 quorum.required, encoladas,
                 law.get("law_id") if law else None)
        if firma == self._availability_publicada:
            return
        anterior = self._availability_publicada
        self._availability_publicada = firma
        # `since` sólo se renueva al entrar en indisponibilidad; mientras siga
        # caído conserva el instante original aunque cambie el recuento.
        seguia_caido = anterior is not None and not anterior[0] and not quorum.ok
        since = ""
        if not quorum.ok:
            previo = self.store.get_availability_state().get("since") if seguia_caido else ""
            since = previo or _iso(self.now())
        try:
            self.store.save_availability_state({
                "available": quorum.ok,
                "category": quorum.category,
                "live_workers": quorum.live,
                "eligible_workers": quorum.eligible,
                "required_workers": quorum.required,
                "queued_laws": encoladas,
                "reason": quorum.reason(),
                "since": since,
                "updated_at": _iso(self.now()),
            })
        except Exception:  # noqa: BLE001
            log.exception("no se pudo publicar el estado de disponibilidad")
            return
        if quorum.ok and anterior is not None and not anterior[0]:
            log.info("quórum restablecido (%d minero(s) elegible(s) para '%s'): "
                     "reanudo la apertura de ventanas",
                     quorum.eligible, quorum.category)

    def _n_zeros_para_esta_ventana(self) -> int:
        """`n` a usar ahora: fijo por config, o medido si la dificultad es dinámica.

        El cómputo se mide sobre la población **viva** y con el modelo de
        buscadores independientes (los standalone no se suman entre sí; los pools
        sí agregan internamente). El trinquete se encarga de que `n` suba en el
        acto y baje con histéresis.

        Si medir falla —Redis caído, datos corruptos— se sigue con el `n` de
        config en vez de propagar la excepción: la dificultad es un parámetro de
        la ventana, y no abrirla porque no se pudo medir sería peor que abrirla
        con el valor anterior.
        """
        if not self.dynamic_difficulty:
            return self.n_zeros
        try:
            hashrate = effective_hashrate(self.store.live_workers(),
                                          self.store.teams_composition(),
                                          self.hps_cpu, self.hps_gpu)
            medido = difficulty_for(hashrate,
                                    target_seconds=self.difficulty_target_seconds)
            # Se relee en cada ventana en vez de sólo al arrancar: así un NCT
            # recién promovido toma el estado sin necesitar un hook de promoción,
            # y dos réplicas no divergen. Es un HGETALL por ventana, y las
            # ventanas duran minutos.
            self._ratchet.restore(self.store.get_difficulty_state())
            # El `n` anterior es el del trinquete, no `self.n_zeros`: tras un
            # reinicio este último vuelve al valor de config y loguearlo como
            # "anterior" haría parecer que la dificultad bajó cuando no lo hizo.
            anterior = self._ratchet.current if self._ratchet.current is not None \
                else self.n_zeros
            n = self._ratchet.update(medido)
            self.store.save_difficulty_state(self._ratchet.state())
            if n != anterior:
                log.info("dificultad dinámica: %.0f H/s efectivos ⇒ n=%d "
                         "(medido %d, anterior %d)", hashrate, n, medido, anterior)
            elif medido < n:
                # El trinquete sosteniendo: la red se achicó pero `n` no cede
                # todavía. Es el momento que hay que poder ver en el log, porque
                # es la defensa contra apagar mineros para promulgar barato.
                log.info("dificultad dinámica: %.0f H/s efectivos ⇒ medido n=%d, "
                         "sostengo n=%d (%d/%d ventanas para bajar)",
                         hashrate, medido, n, self._ratchet._low_streak,
                         self._ratchet.decay_windows)
            self.n_zeros = n
            # El espacio de nonces acompaña a `n` o las derogaciones vencen sin
            # solución. Viaja en el desafío para que ningún minero pueda quedar
            # con un rango que no alcanza.
            self.nonce_space = nonce_space_for(n)
            return n
        except Exception:  # noqa: BLE001
            log.exception("no se pudo medir la red; sigo con n=%d", self.n_zeros)
            return self.n_zeros

    def _avisar_si_dificultad_incoherente(self) -> None:
        """Avisa una sola vez si `n` y el espacio de nonces no se corresponden.

        Una vez y no por ventana: con el sistema mal configurado el aviso se
        repetiría en cada ley y ahogaría el log justo cuando hay que leerlo. Se
        rearma si la config vuelve a estar bien, para que un segundo desajuste
        vuelva a avisar.
        """
        if not self.nonce_space:
            return
        aviso = espacio_insuficiente(self.n_zeros, self.nonce_space)
        if aviso and not self._aviso_dificultad_emitido:
            log.warning("dificultad mal calibrada: %s", aviso)
            self._aviso_dificultad_emitido = True
        elif not aviso:
            self._aviso_dificultad_emitido = False

    def open_window(self, law: dict) -> None:
        action = law.get("action", ACTION_PROMULGACION)
        category = normalize_category(law.get("category"))
        law_id = law["law_id"]
        window_num = self.store.next_window_number()
        voting_window_id = f"W{window_num}-{law_id}"
        n_zeros_required = n_zeros_for_action(self._n_zeros_para_esta_ventana(),
                                              action)
        opened = self.now()
        deadline = opened + self.window_seconds[action]
        base = build_partial_hash_base(law_id, law["text_hash"],
                                       voting_window_id, action)

        self.store.save_window(voting_window_id=voting_window_id, law_id=law_id,
                               action=action, n_zeros_required=n_zeros_required,
                               opened_at=_iso(opened), deadline=_iso(deadline),
                               partial_hash_base=base, category=category)
        self.store.set_law_status(law_id, LawStatus.IN_WINDOW)
        self.store.remove_from_queue(law_id)
        self.store.set_active_window(voting_window_id)

        self._active = {
            "voting_window_id": voting_window_id, "law_id": law_id,
            "action": action, "n_zeros_required": n_zeros_required,
            "partial_hash_base": base, "deadline_epoch": deadline,
            "author_pubkey": law.get("author_pubkey"),
            "category": category,
        }
        self._last_author = turn_holder(law)
        self.store.set_last_author(self._last_author)
        self.store.push_window_author(self._last_author)

        # Verificación previa a publicar el desafío: `n` y el espacio de nonces
        # se mueven juntos o el sistema falla mudo (las ventanas vencen y parece
        # falta de mineros). Se chequea acá además de al arrancar porque la
        # config puede cambiar con el sistema andando —un ConfigMap parcheado— y
        # el NCT no se entera hasta el próximo reinicio.
        self._avisar_si_dificultad_incoherente()

        nct_windows_opened_total.inc()

        self.m.publish_challenge({
            "voting_window_id": voting_window_id, "law_id": law_id,
            "n_zeros_required": n_zeros_required, "deadline": _iso(deadline),
            "partial_hash_base": base, "action": action,
            # Área de gobierno de la ley (AGENT.md 3.10): NO entra en el
            # partial_hash_base (el desafío no cambia), viaja para que cada
            # coordinador de equipo decida si esta ventana le interesa.
            "category": category,
            # Epoch de publicación: los workers miden con esto la latencia
            # RabbitMQ → worker (métrica voxchain_worker_challenge_latency_seconds).
            # Espacio de nonces de ESTA ventana. Con dificultad dinámica cambia
            # con `n`: el minero tiene que barrer un rango que contenga la
            # solución, y su variable de entorno quedó fijada al arrancar.
            "nonce_space": self.nonce_space,
            "published_at": opened,
        })
        log.info("ventana %s abierta (%s de %s, %d ceros, deadline %s)",
                 voting_window_id, action, category, n_zeros_required,
                 _iso(deadline))

    # -- flujo 3: respuesta_nonce (red → NCT) ------------------------------
    def handle_nonce_response(self, sol: dict) -> None:
        if not self.is_leader:
            log.debug("nonce ignorado (no somos el líder)")
            return
        if self._active is None:
            log.info("nonce descartado: no hay ventana activa (%s)", sol)
            return
        active = self._active
        wid = sol.get("voting_window_id")
        if wid != active["voting_window_id"]:
            log.info("nonce descartado (tardío/otra ventana): %s ≠ %s",
                     wid, active["voting_window_id"])
            return
        if self.now() > active["deadline_epoch"]:
            log.info("nonce descartado: llegó después del deadline de %s", wid)
            return

        winner = sol.get("winning_node_or_pool", "")
        nonce = sol.get("nonce")
        # Firma del solver (A-01 fase 2): si está firmada, winner es la pubkey y
        # la firma debe validar; con require_signatures es obligatoria. Esto hace
        # exigible la regla 3.4 (un atacante no puede declarar winner==autor ajeno).
        if not self._nonce_signature_ok(active["voting_window_id"], nonce, winner,
                                        sol.get("signature")):
            return
        # Regla 3.4: el autor pierde el voto en la ventana de su propia ley.
        #
        # ``winner`` es la pubkey del **nodo** que resolvió, no la del ciudadano:
        # desde que el minero firma con identidad propia (3.1), comparar contra
        # ``author_pubkey`` a secas dejaría pasar al autor minando su propia ley
        # con su minero. Se resuelve nodo → dueño; un nodo sin vincular no se
        # imputa a nadie y se compara consigo mismo, como antes.
        if winner and self._citizen_behind(winner) == active["author_pubkey"]:
            log.info("nonce descartado: el autor no puede ganar su propia ventana")
            return

        validation_started = time.perf_counter()
        ok, block_hash_input = verify_nonce(active["partial_hash_base"],
                                            int(nonce), active["n_zeros_required"])
        if not ok:
            log.warning("nonce inválido descartado (no cumple %d ceros): %s",
                        active["n_zeros_required"], nonce)
            return

        # Cierre atómico (BUG 2 / AGENT.md 5): el PRIMER nonce válido recibido
        # cierra la ventana. El guard vive en Redis (SETNX) para ser autoritativo
        # ante failover; toda solución válida posterior para la misma ventana ve
        # la clave ya puesta y se descarta como tardía (no sobrescribe nada).
        wid = active["voting_window_id"]
        if not self.store.try_seal_window(wid, winner or "desconocido"):
            log.info("nonce tardío descartado: ventana %s ya sellada por %s",
                     wid, self.store.get_window_sealer(wid))
            return

        self._seal(active, int(nonce), winner)
        nct_nonce_validation_seconds.observe(
            time.perf_counter() - validation_started)

    def _seal(self, active: dict, nonce: int, winner: str) -> None:
        action = active["action"]
        law_id = active["law_id"]
        law = self.store.get_law(law_id) or {}
        text_compressed = str(law.get("text_compressed", ""))
        text_original_len = int(law.get("text_original_len", 0))
        block = seal_block(
            previous_hash=self.store.last_block_hash(),
            law_id=law_id, action=action,
            n_zeros_required=active["n_zeros_required"], nonce=nonce,
            winning_node_or_pool=winner or "desconocido",
            voting_window_id=active["voting_window_id"], timestamp=_iso(self.now()),
            text_compressed=text_compressed,
            text_original_len=text_original_len,
        )
        # CAS atómico (A-04): si otro NCT ya avanzó el tip durante el solapamiento
        # de split-brain, append_block devuelve False y abortamos para evitar fork.
        if not self.store.append_block(block):
            log.warning(
                "append_block rechazado por CAS (tip cambió): split-brain detectado "
                "en ventana %s — re-encolando ley %s",
                active["voting_window_id"], law_id,
            )
            self.store.set_law_status(law_id, LawStatus.PENDING_QUEUE)
            self.store.enqueue_law(law_id)
            self.store.clear_active_window()
            self._active = None
            self.maybe_open_window()
            return
        self.store.set_window_result(active["voting_window_id"],
                                     result=WindowResult.SUCCESS, winning_nonce=nonce,
                                     winning_node_or_pool=winner)
        new_status = (LawStatus.REPEALED if action == ACTION_DEROGACION
                      else LawStatus.PROMULGATED)
        self.store.set_law_status(law_id, new_status)
        self.store.clear_active_window()
        self._active = None
        nct_blocks_sealed_total.inc()
        log.info("bloque sellado %s (ley %s → %s, nonce %d, por %s)",
                 block.block_hash[:12], law_id, new_status, nonce, winner)
        self.maybe_open_window()

    # -- cierre por deadline (ley pendiente → discarded) -------------------
    def check_deadline(self) -> None:
        if self._active is None:
            return
        if self.now() <= self._active["deadline_epoch"]:
            return
        active = self._active
        law = self.store.get_law(active["law_id"])
        # Vencer sin quórum no es lo mismo que vencer sin apoyo. Si al expirar la
        # red está por debajo del mínimo —los mineros se cayeron durante la
        # ventana—, la ley vuelve a la cola en vez de descartarse: nadie pudo
        # minarla, así que el vencimiento no dice nada sobre la ley y
        # descartarla le cobraría al autor el cooldown largo de reproposición
        # idéntica (3.5) por una falla de infraestructura ajena.
        # Se evalúa la ventana que venció, con su acción: si lo que expiró fue
        # una derogación que los equipos del área rechazan, eso es abstención y
        # no falta de red — pero eso ya lo decidió `_hay_quorum` al abrir, y
        # llegar acá con la misma respuesta significa que la red se cayó.
        quorum = self._quorum({"category": active.get("category"),
                               "action": active.get("action"),
                               "law_id": active.get("law_id")})
        if quorum is not None and not quorum.ok:
            self._expirar_sin_quorum(active, quorum)
            return
        # Ley pendiente (3.2/3.4): se descarta, NO se reencola automáticamente.
        self.store.set_window_result(active["voting_window_id"],
                                     result=WindowResult.EXPIRED_PENDING)
        self.store.set_law_status(active["law_id"], LawStatus.DISCARDED)
        if law:
            self.store.mark_text_hash_discarded(law.get("text_hash", ""))
        self.store.clear_active_window()
        self._active = None
        log.info("ventana %s vencida sin solución: ley %s descartada",
                 active["voting_window_id"], active["law_id"])
        self.maybe_open_window()

    def _expirar_sin_quorum(self, active: dict, quorum: Availability) -> None:
        """Cierra la ventana vencida devolviendo la ley a la cola.

        No se marca el ``text_hash`` como descartado a propósito: reproponer no
        es acá una reproposición idéntica sino la misma ley que nunca llegó a
        ser juzgada. Lo que sí queda gastado es el turno que la ley consumió al
        abrir (``push_window_author``); revertirlo exigiría deshacer el historial
        de la cuota y se acepta como costo menor frente a perder la ley.
        """
        law_id = active["law_id"]
        self.store.set_window_result(active["voting_window_id"],
                                     result=WindowResult.EXPIRED_NO_QUORUM)
        self.store.set_law_status(law_id, LawStatus.PENDING_QUEUE)
        self.store.enqueue_law(law_id)
        self.store.clear_active_window()
        self._active = None
        log.warning("ventana %s vencida sin quórum (%s): ley %s reencolada, "
                    "no descartada", active["voting_window_id"], quorum.reason(),
                    law_id)
        self.maybe_open_window()

    def become_leader(self) -> None:
        """Transiciona este NCT de follower a líder tras ganar la elección.

        Recién acá abre los consumidores de las colas de trabajo (BUG 1): siendo
        follower no estaba suscrito a ``propuestas`` ni ``respuesta_nonce``."""
        if self.is_leader:
            return
        nct_is_leader.set(1)
        log.info("asumiendo como líder NCT (%s): abriendo colas de trabajo", self.nct_id)
        self.is_leader = True
        self._subscribe_work_queues()
        self._last_author = self.store.get_last_author()
        self.store.clear_active_window()
        self._active = None
        # La ventana en curso al momento de la caída se pierde (AGENT.md 4);
        # si hay leyes pendientes en Redis, abrimos una ventana nueva.
        self.maybe_open_window()

    def step_down(self) -> None:
        """Líder → follower: cierra las colas de trabajo y suelta la ventana.

        Se invoca al detectar pérdida de liderazgo en Redis (split-brain,
        AGENT.md 11.4): no basta con ignorar mensajes en memoria, hay que dejar
        de consumir ``propuestas``/``respuesta_nonce`` para no robarlos del
        reparto round-robin. La ventana en curso se pierde por diseño (AGENT.md 4).
        Tras ceder el liderazgo, notifica al monitor (``_on_stepdown``) para que
        empiece a observar heartbeats del nuevo líder."""
        if not self.is_leader:
            return
        nct_is_leader.set(0)
        log.warning("step_down (%s): liderazgo perdido, cerrando colas de trabajo",
                    self.nct_id)
        self.is_leader = False
        self._unsubscribe_work_queues()
        self._active = None
        if self._on_stepdown is not None:
            self._on_stepdown()

    # -- tick periódico para el loop de consumo ----------------------------
    def tick(self) -> None:
        self.check_deadline()
        self.maybe_open_window()
        self._maybe_publish_heartbeat()

    def _maybe_publish_heartbeat(self) -> None:
        if not self.is_leader or self.heartbeat_interval <= 0:
            return
        now = self.now()
        if now - self._last_heartbeat_pub < self.heartbeat_interval:
            return
        self._last_heartbeat_pub = now
        # Renovar liderazgo en Redis. Si otro NCT ya lo adquirió (split-brain,
        # AGENT.md 11.4), renovar falla y nos retiramos cerrando las colas.
        if not self.store.renew_leadership(self.nct_id):
            self.step_down()
            return
        self.m.publish_heartbeat({
            "nct_id": self.nct_id,
            "ts": now,
            "active_window_id": (self._active["voting_window_id"]
                                 if self._active else None),
            "last_block_hash": self.store.last_block_hash(),
        })
