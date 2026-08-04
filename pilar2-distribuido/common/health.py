"""Endpoint de salud HTTP y métricas Prometheus (DOC.md: health JSON, U5.5).

Levanta un servidor ``http.server`` en un hilo daemon que responde en:
- ``/health`` → JSON ``{servicio: status}``
- ``/metrics`` → texto plano Prometheus
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from prometheus_client import exposition

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
) -> ThreadingHTTPServer:
    """Arranca el server en un hilo daemon y devuelve la instancia.

    ``ok_values`` son los valores de estado que cuentan como sanos. Existe
    porque no todo campo del status describe una dependencia: el NCT reporta
    además su *rol* (``"ok"`` si es líder, ``"standby"`` si no). Con el criterio
    ingenuo de exigir que todos los valores sean ``"ok"``, un standby —que está
    conectado, al día y listo para tomar el relevo— respondía 503, su
    readinessProbe fallaba para siempre y su Deployment nunca terminaba de
    desplegarse. Un rol no es un diagnóstico.
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
            if self.path not in ("/health", "/", "/healthz"):
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

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
