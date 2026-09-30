"""Logging estructurado en JSON para Prometheus/Grafana (DOC.md: registros).
Cada servicio llama ``setup_logging("nct")`` una vez al arrancar. Escribe a stdout
(JSON, lo recoge Docker / kubectl / Alloy en GKE) y opcionalmente a un archivo
rotativo. Si está ``LOKI_PUSH_URL``, además manda cada registro directo a Loki.

Variables de entorno:
  LOG_DIR            — directorio para archivos rotativos (default ``/var/log/voxchain``)
  LOG_FORMAT         — ``json`` (default) o ``text`` (para desarrollo local)
  LOG_LEVEL          — nivel de logging (default ``INFO``)
  LOKI_PUSH_URL      — endpoint de push de Loki (``.../loki/api/v1/push``); vacío = no se usa
  LOKI_PUSH_USER     — usuario de basic auth para ese endpoint
  LOKI_PUSH_PASSWORD — contraseña de basic auth
  LOKI_CLUSTER       — valor del label ``cluster`` (default ``externo``)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import socket
import sys
import threading
import urllib.request
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

_EXTRA_ATTRS = frozenset({
    "service",
})


class JsonFormatter(logging.Formatter):
    """Formatea cada registro como una línea JSON.

    Campos incluidos:
      - ``timestamp``  ISO 8601 (UTC)
      - ``level``      nivel del log (INFO, WARNING, …)
      - ``logger``     nombre del logger (voxchain.nct, …)
      - ``service``    nombre del servicio (pasado en setup_logging)
      - ``message``    mensaje formateado
      - ``exception``  traceback completo (sólo si hay excepción)
    """

    def __init__(self, service: str = ""):
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "service": self._service,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


class LokiHandler(logging.Handler):
    """Manda los registros a Loki por HTTP, en lotes y sin frenar al que loguea.

    Existe por los mineros del k3s: ese clúster no nos deja crear Roles, así que
    ningún colector puede leer los logs de sus pods (en GKE lo hace Alloy). El
    proceso los empuja él mismo al endpoint de push de Loki, que en GKE está
    detrás del Ingress de logs con TLS y basic auth.

    ``emit`` sólo encola: el envío va en un hilo aparte. Si Loki no responde o la
    cola se llena, los registros se descartan y se cuentan (``failed``,
    ``dropped``): siguen en stdout y en el archivo rotativo, y un Loki caído no
    puede trabar la minería. Los errores propios van a stderr y no al logging,
    que volvería a pasar por este handler.
    """

    def __init__(self, url: str, *, labels: dict, user: str = "", password: str = "",
                 batch_size: int = 200, flush_interval: float = 2.0,
                 max_queue: int = 10_000, timeout: float = 5.0):
        super().__init__()
        self.url = url
        self.labels = dict(labels)
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.timeout = timeout
        self._headers = {"Content-Type": "application/json"}
        if user or password:
            token = base64.b64encode(f"{user}:{password}".encode()).decode()
            self._headers["Authorization"] = f"Basic {token}"
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self.dropped = 0
        self.failed = 0
        self._fallando = False
        self._thread = threading.Thread(target=self._run, name="loki-push", daemon=True)
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entrada = (str(int(record.created * 1e9)), record.levelname, self.format(record))
        except Exception:  # noqa: BLE001 — lo mismo que hace logging con un formato roto
            self.handleError(record)
            return
        try:
            self._queue.put_nowait(entrada)
        except queue.Full:
            self.dropped += 1

    def close(self) -> None:
        """Manda lo que quedó en la cola y termina el hilo (lo llama logging.shutdown)."""
        if not self._stop.is_set():
            self._stop.set()
            self._thread.join(timeout=self.timeout + 1)
        super().close()

    def _run(self) -> None:
        # Espera sobre el Event y no sobre la cola: así close() despierta al hilo
        # en el acto, en vez de esperar a que venza un get() con timeout.
        while not self._stop.wait(self.flush_interval):
            self._vaciar()
        self._vaciar()  # lo que haya quedado al cerrar

    def _vaciar(self) -> None:
        while True:
            lote = []
            try:
                while len(lote) < self.batch_size:
                    lote.append(self._queue.get_nowait())
            except queue.Empty:
                pass
            if not lote:
                return
            self._enviar(lote)

    def _enviar(self, lote: list) -> None:
        # Un stream por nivel: `level` es label, igual que en lo que junta Alloy.
        streams: dict[str, list] = {}
        for ts, nivel, linea in lote:
            streams.setdefault(nivel, []).append([ts, linea])
        cuerpo = {"streams": [{"stream": {**self.labels, "level": nivel}, "values": valores}
                              for nivel, valores in streams.items()]}
        pedido = urllib.request.Request(self.url, data=json.dumps(cuerpo).encode(),
                                        headers=self._headers, method="POST")
        try:
            with urllib.request.urlopen(pedido, timeout=self.timeout) as respuesta:
                respuesta.read()
        except Exception as exc:  # noqa: BLE001 — cualquier falla descarta el lote
            self.failed += len(lote)
            if not self._fallando:
                print(f"[logging] no se pudo enviar a Loki ({type(exc).__name__}: {exc}); "
                      "se descartan hasta que vuelva", file=sys.stderr)
                self._fallando = True
            return
        if self._fallando:
            print("[logging] envío a Loki restablecido", file=sys.stderr)
            self._fallando = False


def _level_from_env() -> int:
    raw = os.getenv("LOG_LEVEL", "INFO").upper()
    return getattr(logging, raw, logging.INFO)


def setup_logging(service_name: str, level: int | None = None) -> logging.Logger:
    log_dir = os.getenv("LOG_DIR", "/var/log/voxchain")
    log_fmt = os.getenv("LOG_FORMAT", "json")
    effective_level = level if level is not None else _level_from_env()

    root = logging.getLogger()
    root.setLevel(effective_level)

    for h in list(root.handlers):
        root.removeHandler(h)
        # El único con un hilo propio que quedaría vivo si no se cierra.
        if isinstance(h, LokiHandler):
            h.close()

    if log_fmt == "json":
        formatter: logging.Formatter = JsonFormatter(service=service_name)
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s"
        )

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)

    try:
        os.makedirs(log_dir, exist_ok=True)
        file_handler = RotatingFileHandler(
            os.path.join(log_dir, f"{service_name}.log"),
            maxBytes=5 * 1024 * 1024, backupCount=3,
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        root.warning(
            "LOG_DIR %s no escribible; sólo stdout",
            log_dir,
        )

    loki_url = os.getenv("LOKI_PUSH_URL", "").strip()
    if loki_url:
        loki = LokiHandler(
            loki_url,
            # Los mismos nombres de label que pone Alloy a los pods de GKE, para
            # que una consulta por `service` o `level` encuentre las dos fuentes.
            labels={
                "service": service_name,
                "cluster": os.getenv("LOKI_CLUSTER", "externo"),
                "pod": socket.gethostname(),
            },
            user=os.getenv("LOKI_PUSH_USER", ""),
            password=os.getenv("LOKI_PUSH_PASSWORD", ""),
        )
        loki.setFormatter(formatter)
        root.addHandler(loki)

    return logging.getLogger(service_name)
