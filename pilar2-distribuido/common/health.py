"""Endpoint de salud HTTP y métricas Prometheus (DOC.md: health JSON, U5.5).

Levanta un servidor ``http.server`` en un hilo daemon que responde en:
- ``/health`` → JSON ``{servicio: status}``
- ``/ready`` → 200/503 según si el proceso debe recibir tráfico de su Service
- ``/metrics`` → texto plano Prometheus
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from prometheus_client import exposition

class _QuietHTTPServer(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` que no vuelca traceback si el cliente ya cerró.

    ``BaseHTTPRequestHandler`` habla HTTP/1.0, así que cada respuesta cierra la
    conexión: el kubelet lee el status de la probe y cierra sin leer el cuerpo.
    Cuando el server escribe el body el socket ya no está y salta
    ``BrokenPipeError``, que el handler por defecto imprime entero a stderr —
    un traceback por cada probe, cada pocos segundos, en Cloud Logging.
    La probe funciona igual; el error es del cliente que se fue, no nuestro.
    """

    def handle_error(self, request, client_address):  # noqa: D102
        exc_type = sys.exc_info()[0]
        if exc_type is not None and issubclass(exc_type, ConnectionError):
            return
        super().handle_error(request, client_address)

def json_response(handler, data, status=200):
    """Escribe una respuesta JSON en un BaseHTTPRequestHandler.

    data=None → cuerpo vacío (para 204 No Content).
    """
    body = b"" if data is None else json.dumps(data).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if body:
        handler.wfile.write(body)

def start_health_server(
    port: int,
    status_provider: Callable[[], dict],
    ok_values: tuple[str, ...] = ("ok",),
    readiness_provider: Callable[[], tuple[bool, dict]] | None = None,
) -> ThreadingHTTPServer:
    """Arranca el server en un hilo daemon y devuelve la instancia.

    ``ok_values`` son los valores de estado que cuentan como sanos. Existe
    porque no todo campo del status describe una dependencia: el NCT reporta
    además su *rol* (``"ok"`` si es líder, ``"standby"`` si no). Con el criterio
    ingenuo de exigir que todos los valores sean ``"ok"``, un standby —que está
    conectado, al día y listo para tomar el relevo— respondía 503, su
    readinessProbe fallaba para siempre y su Deployment nunca terminaba de
    desplegarse. Un rol no es un diagnóstico.

    ``readiness_provider`` responde otra pregunta: no "¿está vivo?" sino "¿le
    tiene que llegar tráfico?". Devuelve ``(listo, detalle)`` y se sirve en
    ``/ready``, separado de ``/health`` a propósito: ``/health`` es la
    livenessProbe, y un proceso que no debe recibir tráfico —un coordinador de
    pool sin el lease— está perfectamente sano; si fallara la liveness,
    Kubernetes lo reiniciaría en bucle. Sin provider, ``/ready`` responde lo
    mismo que ``/health``.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/metrics":
                body = exposition.generate_latest()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path in ("/ready", "/readyz") and readiness_provider is not None:
                ready, detail = readiness_provider()
                json_response(self, {"ready": ready, **detail},
                              200 if ready else 503)
                return
            if self.path not in ("/health", "/", "/healthz", "/ready", "/readyz"):
                self.send_response(404)
                self.end_headers()
                return
            status = status_provider()
            all_ok = all(v in ok_values for v in status.values())
            body = json.dumps(status).encode()
            self.send_response(200 if all_ok else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silenciar logs de acceso
            pass

    server = _QuietHTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
