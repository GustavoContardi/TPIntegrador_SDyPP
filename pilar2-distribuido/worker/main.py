"""Punto de entrada del worker minero.

Modos:
  - ``standalone``: se suscribe al desafío activo del NCT, mina espacio completo.
  - ``pool-coordinator``: fragmenta espacio de nonces, acepta workers HTTP, auto-mina.
  - ``pool-worker``: se conecta a un Pool Coordinator vía HTTP.
  - ``pool-auto``: modo pool con bully election. Compite con otros workers vía
    RabbitMQ; el ganador actúa como coordinator, los demás como miners.

Hot-switch entre modos vía POST /switch-mode en puerto admin (9090).
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading

from common import config
from common.health import start_health_server
from common.logging_setup import setup_logging
from common.messaging import build_rabbitmq
from common.redis import create_redis
from worker_pkg.admin_server import start_admin_server
from worker_pkg.identity import WorkerSigner, enroll
from worker_pkg.miner import gpu_usable, run_miner, ultimo_hashrate
from worker_pkg.pool_worker import PoolWorker
from worker_pkg.pool_coordinator import PoolCoordinator
from worker_pkg.standalone_worker import StandaloneWorker
from worker_pkg.bully import PoolBully

log = logging.getLogger("voxchain.worker")


class WorkerManager:
    """Gestiona el worker activo y permite hot-switch entre modos."""

    def __init__(self, worker_id: str, has_gpu: bool, signer=None):
        self.worker_id = worker_id
        self.has_gpu = has_gpu
        self.signer = signer
        self._messaging = None
        self._worker = None
        self._thread = None
        self._mode = "idle"
        self._pool_url = ""
        self._pool_httpd = None
        # `_thread` corre siempre el loop de consumo de RabbitMQ; `_worker_thread`
        # es el bucle propio del modo cuando lo tiene (pool-worker pide trabajo
        # por HTTP en su propio ciclo). Antes eran el mismo, y el modo
        # pool-worker se quedaba sin consumidor: sus comandos remotos
        # (`switch_mode`) no llegaban nunca.
        self._worker_thread = None
        self._stop_event = threading.Event()
        self._pool_http_port = int(os.getenv("POOL_HTTP_PORT", "9001"))
        # Dirección con la que otros workers pueden alcanzar el servidor HTTP de
        # este nodo cuando actúa de pool-coordinator. Se publica en el estado
        # para que el backend la reparta a quien se una a su equipo: nadie tiene
        # que escribir la URL a mano.
        #
        # Se explicita por env (`WORKER_ADDRESS`) en todos los despliegues que
        # controlamos, porque adivinarla es frágil: en Compose el hostname del
        # contenedor no tiene por qué coincidir con el WORKER_ID (el servicio
        # `worker-pool-coordinator` corre con WORKER_ID `pool-coordinator-1`), y
        # en Kubernetes los pods de un Deployment no tienen DNS estable — ahí la
        # dirección buena es la IP del pod, que llega por `MY_POD_IP`.
        self.address = self._resolve_address()
        self._bully = None
        self._report_thread = None
        # Serializa los cambios de modo: pueden entrar a la vez por el admin
        # HTTP y por un comando de RabbitMQ, y cada uno para y arranca hilos.
        self._switch_lock = threading.Lock()

    # -- API pública para admin_server --

    def _resolve_address(self) -> str:
        explicit = os.getenv("WORKER_ADDRESS", "").strip()
        if explicit:
            return explicit.rstrip("/")
        host = os.getenv("MY_POD_IP", "").strip() or socket.gethostname()
        return f"http://{host}:{self._pool_http_port}"

    def get_status(self) -> dict:
        pool_url = self._pool_url
        if self._mode == "pool-coordinator":
            # Siendo coordinator, la "URL del pool" es la propia: es lo que el
            # backend guarda como dirección del equipo y entrega a los que se
            # unan. Antes se armaba con el worker_id como hostname, que sólo
            # resolvía por casualidad cuando el id coincidía con el nombre del
            # servicio de Compose.
            pool_url = self.address
        bully_state = self._bully.state if self._bully else None
        return {
            "mode": self._mode,
            "worker_id": self.worker_id,
            "pool_url": pool_url,
            "address": self.address,
            "bully_state": bully_state,
            "running": any(t is not None and t.is_alive()
                           for t in (self._thread, self._worker_thread)),
            "pubkey": self.signer.pubkey if (self.signer and self.signer.enabled) else None,
            # Con qué recurso mina y cuántos fragmentos atiende a la vez. Es lo
            # que hace falta para estimar el cómputo de la red desde Redis, sin
            # scrapear Prometheus, cuando hay que revisar si `n` sigue siendo
            # adecuado para la población de mineros (AGENT.md 11.3).
            "has_gpu": self.has_gpu,
            "capacity": config.get_int("WORKER_CAPACITY", 1),
            # H/s medidos: es lo que el NCT usa para calcular la dificultad
            # dinámica. 0 mientras el minero no haya minado todavía; ahí el NCT
            # le estima el cómputo por su recurso (CPU/GPU).
            "hashrate_hps": ultimo_hashrate(),
        }

    def switch_mode(self, target: str, pool_url: str = "") -> dict:
        if target not in ("pool-worker", "standalone", "pool-coordinator", "pool-auto"):
            raise ValueError(f"modo desconocido: {target}")
        if target == "pool-worker" and not pool_url:
            raise ValueError("pool_url requerido para modo pool-worker")
        with self._switch_lock:
            log.info("switching mode: %s → %s", self._mode, target)
            self._stop_current()
            if target == "pool-worker":
                self._start_pool_worker(pool_url)
            elif target == "standalone":
                self._start_standalone()
            elif target == "pool-coordinator":
                self._start_pool_coordinator()
            elif target == "pool-auto":
                self._start_pool_auto()
            log.info("modo activo: %s", self._mode)
            return {"ok": True, "mode": self._mode, "pool_url": self._pool_url}

    # -- modo deseado (intención persistida por el backend) ----------------

    DESIRED_KEY = "worker:desired_mode:{worker_id}"

    @classmethod
    def read_desired_mode(cls, redis_client, worker_id: str):
        """Modo que el dueño del minero le fijó, o ``None``.

        Es la contraparte de lo que escribe el backend al asignar un minero a un
        equipo. Existe porque el comando por RabbitMQ viaja a una cola exclusiva
        del worker: si el minero estaba apagado cuando su dueño lo asignó, esa
        orden no la recibió nadie. Leerla acá es lo que hace que un minero
        registrado hoy y encendido mañana arranque en el equipo que le tocó.
        """
        try:
            import json
            raw = redis_client.get(cls.DESIRED_KEY.format(worker_id=worker_id))
            if not raw:
                return None
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            log.debug("no se pudo leer el modo deseado", exc_info=True)
            return None

    def _reconcile_desired_mode(self, redis_client) -> None:
        """Aplica el modo deseado si difiere del actual.

        Reconciliar en vez de depender sólo del mensaje cubre tres casos que el
        comando no cubre: el minero estaba apagado cuando lo asignaron, el
        mensaje se perdió, o la dirección de su coordinador cambió (su pod se
        reinició y le tocó otra IP).
        """
        desired = self.read_desired_mode(redis_client, self.worker_id)
        if not desired:
            return
        mode = desired.get("mode", "")
        if mode not in ("standalone", "pool-worker", "pool-coordinator", "pool-auto"):
            return
        pool_url = (desired.get("pool_url") or "").rstrip("/")
        if mode == self._mode and (mode != "pool-worker"
                                   or pool_url == self._pool_url.rstrip("/")):
            return
        log.info("reconciliando modo: %s → %s (%s)", self._mode, mode,
                 pool_url or "sin pool")
        try:
            self.switch_mode(mode, pool_url)
        except Exception:  # noqa: BLE001
            log.exception("no se pudo aplicar el modo deseado %s", mode)

    def _redis_report_loop(self) -> None:
        redis_client = None
        try:
            from common.redis import create_redis
            redis_client = create_redis(config.REDIS_URL)
        except Exception:
            log.warning("No se pudo inicializar cliente de Redis para reporte de estado")

        # El fallo se avisa la primera vez y cuando se recupera, no en cada
        # vuelta: en debug era invisible —una contraseña de Redis vencida dejó
        # de reportar estado durante todo un despliegue sin una sola línea de
        # log— y en warning cada 5 s sería ruido inservible.
        fallando = False
        while not self._stop_event.is_set():
            if redis_client:
                try:
                    status = self.get_status()
                    import json
                    redis_client.set(f"worker:status:{self.worker_id}", json.dumps(status), ex=15)
                    if fallando:
                        log.info("reporte de estado a Redis restablecido")
                        fallando = False
                    self._reconcile_desired_mode(redis_client)
                except Exception as exc:
                    if not fallando:
                        log.warning("no se puede reportar estado a Redis (%s): %s",
                                    type(exc).__name__, exc)
                        fallando = True
                    else:
                        log.debug("sigue fallando el reporte a Redis", exc_info=True)
            self._stop_event.wait(5.0)

    def start(self, mode: str, pool_url: str = "") -> None:
        self.switch_mode(mode, pool_url)
        if self._report_thread is None:
            self._report_thread = threading.Thread(target=self._redis_report_loop, daemon=True)
            self._report_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._stop_current()
        if self._report_thread:
            self._report_thread.join(timeout=5)
            self._report_thread = None

    # -- interno --

    # -- ConfigMap worker-modes (persistencia de modo entre reinicios) --

    @staticmethod
    def _read_mode_from_configmap(worker_id: str) -> str | None:
        try:
            from kubernetes import config, client
            try:
                config.load_incluster_config()
            except config.ConfigException:
                return None
            v1 = client.CoreV1Api()
            ns = open("/var/run/secrets/kubernetes.io/serviceaccount/namespace").read().strip()
            cm = v1.read_namespaced_config_map("worker-modes", ns)
            return cm.data.get(worker_id)
        except Exception:
            log.debug("no se pudo leer worker-modes ConfigMap", exc_info=True)
            return None

    # -- comandos remotos (RabbitMQ worker.command) --

    def _subscribe_worker_commands(self, messaging) -> None:
        messaging.on_worker_command(self.worker_id, self._handle_command)

    def _handle_command(self, msg: dict) -> None:
        cmd = msg.get("type", "")
        if cmd == "switch_mode":
            target = msg.get("mode", "")
            if target in ("standalone", "pool-worker", "pool-auto", "pool-coordinator"):
                log.info("comando remoto: switch_mode → %s", target)
                pool_url = msg.get("pool_url", "")
                self._apply_off_thread(lambda: self.switch_mode(target, pool_url))
        elif cmd == "stop":
            log.info("comando remoto: stop")
            self._apply_off_thread(self.stop)

    def _apply_off_thread(self, fn) -> None:
        """Aplica un comando remoto FUERA del hilo de consumo.

        El callback de RabbitMQ corre en el mismo hilo que ``start_consuming``,
        que es justo el que ``switch_mode`` tiene que parar y joinear: hacerlo
        acá adentro reventaba con ``RuntimeError: cannot join current thread``.
        Y como el wrapper del consumidor loguea y sigue, el worker se quedaba en
        su modo viejo mientras el backend devolvía 200: en la UI el modo se veía
        cambiado un instante y volvía atrás cuando el worker reportaba su estado
        real. El camino del admin HTTP no lo sufría porque ya venía de otro hilo,
        y por eso sólo fallaba con los workers del k3s (los únicos sin fallback
        HTTP en el backend).
        """
        threading.Thread(
            target=self._run_off_thread, args=(fn,), daemon=True,
            name=f"worker-cmd-{self.worker_id}",
        ).start()

    @staticmethod
    def _run_off_thread(fn) -> None:
        try:
            fn()
        except Exception:
            log.exception("error aplicando comando remoto")

    def _stop_current(self) -> None:
        if self._worker:
            self._worker.stop()
            self._worker = None
        self._bully = None
        # Cerrar la mensajería ANTES del join. El hilo está bloqueado en el loop
        # de consumo y sólo sale cuando close() baja la bandera: al revés, el
        # join agotaba su timeout entero en cada cambio de modo.
        if self._messaging:
            self._messaging.close()
            self._messaging = None
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        if self._worker_thread:
            # Puede estar adentro de una tanda de minado, que no se interrumpe.
            # No lo esperamos indefinidamente: `stop()` ya bajó su bandera, así
            # que termina solo al cerrar el fragmento en curso.
            self._worker_thread.join(timeout=10)
            self._worker_thread = None
        if self._pool_httpd:
            self._pool_httpd.shutdown()
            self._pool_httpd = None
        self._mode = "idle"

    def _ensure_messaging(self):
        if self._messaging is None:
            m = build_rabbitmq(config.RABBITMQ_URL)
            m.connect()
            self._messaging = m
        return self._messaging

    def _run_messaging_loop(self, tick=None):
        try:
            self._messaging.start_consuming(tick=tick, tick_interval=1.0)
        except Exception:
            if not self._stop_event.is_set():
                raise

    def _start_pool_worker(self, pool_url: str) -> None:
        self._mode = "pool-worker"
        self._pool_url = pool_url
        m = self._ensure_messaging()
        self._subscribe_worker_commands(m)
        pw = PoolWorker(
            pool_url,
            miner_id=self.worker_id,
            capacity=config.get_int("WORKER_CAPACITY", 1),
            has_gpu=self.has_gpu,
            mine=run_miner,
        )
        self._worker = pw
        # Dos hilos, no uno: el bucle de pedir/minar fragmentos bloquea (una
        # tanda de minado puede tardar segundos), así que el consumo de
        # RabbitMQ va aparte. Si comparten hilo, el worker deja de escuchar
        # `worker.command` y ya no hay forma de sacarlo del equipo — la orden se
        # publica, nadie la consume, y el minero queda pidiéndole fragmentos a
        # su coordinador para siempre.
        self._worker_thread = threading.Thread(target=pw.run, daemon=True,
                                               name=f"pool-worker-{self.worker_id}")
        self._worker_thread.start()
        self._thread = threading.Thread(target=self._run_messaging_loop,
                                        daemon=True)
        self._thread.start()

    def _start_standalone(self) -> None:
        self._mode = "standalone"
        m = self._ensure_messaging()
        self._subscribe_worker_commands(m)
        sw = StandaloneWorker(
            m,
            worker_id=self.worker_id,
            mine=run_miner,
            signer=self.signer,
        )
        sw.wire()
        self._worker = sw
        self._thread = threading.Thread(
            target=self._run_messaging_loop, daemon=True
        )
        self._thread.start()

    def _start_pool_coordinator(self) -> None:
        self._mode = "pool-coordinator"
        m = self._ensure_messaging()
        self._subscribe_worker_commands(m)
        # Redis le da al coordinator el lease de liderazgo y la política de voto.
        # Si no hay Redis alcanzable seguimos igual con `redis=None`: el
        # PoolCoordinator se declara líder de su propio pool y reparte trabajo
        # lo mismo. Antes una URL vacía o un Redis caído tiraba una excepción
        # acá adentro y el switch_mode dejaba al worker en "idle", sin ningún
        # modo activo y sin un mensaje que lo explicara.
        try:
            redis = create_redis(config.REDIS_URL) if config.REDIS_URL else None
        except Exception as exc:  # noqa: BLE001
            log.warning("pool-coordinator sin Redis (%s): sigo como líder local",
                        exc)
            redis = None
        pc = PoolCoordinator(
            m,
            pool_id=self.worker_id,
            redis=redis,
            mine=run_miner,
            capacity=config.get_int("WORKER_CAPACITY", 1),
            signer=self.signer,
        )
        pc.wire()
        pc.start()
        self._worker = pc

        from worker_pkg.pool_coordinator.server import start_pool_http_server
        self._pool_httpd = start_pool_http_server(
            pc, port=config.get_int("POOL_HTTP_PORT", 9001)
        )
        pool_http_thread = threading.Thread(
            target=self._pool_httpd.serve_forever, daemon=True
        )
        pool_http_thread.start()

        self._thread = threading.Thread(
            target=self._run_messaging_loop, args=(pc.tick,), daemon=True
        )
        self._thread.start()

        log.info("pool-coordinator %s iniciado en puerto %d",
                 self.worker_id, config.get_int("POOL_HTTP_PORT", 9001))

    def _start_pool_auto(self) -> None:
        self._mode = "pool-auto"
        m = self._ensure_messaging()
        self._subscribe_worker_commands(m)
        pool_id = os.getenv("POOL_ID", "default")
        address = self.address

        # Redis es opcional en pool-auto: el bully arbitra por RabbitMQ y tiene
        # que seguir funcionando sin él (es su ventaja para nodos federados).
        # Cuando está, se usa sólo para compartir el árbitro final —el lease del
        # pool— con los coordinadores elegidos por la otra vía, de modo que un
        # pool mixto no termine con dos coordinadores activos.
        try:
            redis = create_redis(config.REDIS_URL) if config.REDIS_URL else None
        except Exception as exc:  # noqa: BLE001
            log.warning("pool-auto sin Redis (%s): el bully arbitra solo", exc)
            redis = None

        bully = PoolBully(
            self.worker_id,
            pool_id,
            m,
            has_gpu=self.has_gpu,
            capacity=config.get_int("WORKER_CAPACITY", 1),
            address=address,
            signer=self.signer,
            redis=redis,
        )
        bully.wire()
        self._bully = bully
        # También como `_worker` para que `_stop_current` lo detenga: sin esto,
        # salir de pool-auto dejaba vivos el coordinator o el pool-worker que el
        # bully había arrancado por dentro.
        self._worker = bully

        self._thread = threading.Thread(
            target=self._run_messaging_loop, args=(bully.tick,), daemon=True
        )
        self._thread.start()

        log.info("pool-auto %s iniciado (pool=%s, address=%s)",
                 self.worker_id, pool_id, address)


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: None)
    setup_logging("worker")
    worker_id = os.getenv("WORKER_ID", f"worker-{socket.gethostname()}")
    has_gpu = gpu_usable(os.getenv("MINER_GPU_BIN", ""))

    # Orden de precedencia del modo inicial, de más específico a más genérico:
    #
    #   1. `worker:desired_mode:<id>` en Redis — lo que su dueño decidió desde la
    #      UI. Es lo más reciente y lo único que puede haberse decidido mientras
    #      este minero estaba apagado.
    #   2. El ConfigMap `worker-modes` de Kubernetes — persistencia del hot-switch.
    #   3. `WORKER_MODE` — el default del despliegue.
    mode = ""
    pool_url = ""
    try:
        redis_client = create_redis(config.REDIS_URL) if config.REDIS_URL else None
        desired = (WorkerManager.read_desired_mode(redis_client, worker_id)
                   if redis_client else None)
        if desired and desired.get("mode"):
            mode = desired["mode"]
            pool_url = desired.get("pool_url", "") or ""
            log.info("modo deseado leído de Redis: %s", mode)
    except Exception:  # noqa: BLE001
        log.debug("sin modo deseado en Redis", exc_info=True)

    if not mode:
        mode = (WorkerManager._read_mode_from_configmap(worker_id)
                or os.getenv("WORKER_MODE", "standalone"))
    if not pool_url:
        pool_url = os.getenv("POOL_COORDINATOR_URL", "")
    log.info("iniciando %s modo=%s (gpu=%s)", worker_id, mode, has_gpu)

    signer = WorkerSigner.from_env()
    # Vincula la identidad recién generada con el ciudadano que registró este
    # minero. Best-effort a propósito: sin enrolar el worker mina igual.
    enroll(worker_id, signer)
    if mode == "pool-auto":
        pool_url = os.getenv("POOL_COORDINATOR_URL",
                             f"http://{worker_id}:{int(os.getenv('POOL_HTTP_PORT', '9001'))}")
    manager = WorkerManager(worker_id, has_gpu, signer=signer)
    manager.start(mode, pool_url)

    admin_port = int(os.getenv("ADMIN_PORT", "9090"))
    httpd = start_admin_server(manager, port=admin_port)
    admin_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    admin_thread.start()

    start_health_server(config.HEALTH_PORT, lambda: {
        "status": "ok",
    })

    signal.pause()
    log.info("deteniendo worker...")
    manager.stop()
    httpd.shutdown()


if __name__ == "__main__":
    main()
