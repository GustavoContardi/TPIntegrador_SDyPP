"""Puente al minero de Pilar 1 (no se reimplementa el hashing).

El worker invoca como subproceso el binario CUDA
(``pilar1-minero/gpu/bin/05_brute_force_range``) y, si no hay GPU disponible,
hace fallback al minero CPU (``pilar1-minero/cpu/src/brute_force.py``). Ambos
comparten la misma interfaz de línea de comandos y el mismo formato de salida
(``Nonce = N``), por lo que el parseo es común.

Puente desafío → minero: ``n_zeros_required`` ceros ⇒ prefijo de n caracteres '0'.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from typing import Optional

from common.metrics import (
    worker_hashrate_hps,
    worker_mining_duration_seconds,
    worker_mining_success_total,
    worker_mining_tasks_total,
)

log = logging.getLogger("voxchain.worker.miner")

_NONCE_RE = re.compile(r"Nonce\s*=\s*(\d+)")
_HASH_RE = re.compile(r"=\s*([0-9a-fA-F]{32})")


def parse_miner_output(text: str):
    """Extrae ``(nonce, hash_hex)`` de la salida del minero, o ``(None, None)``."""
    m = _NONCE_RE.search(text)
    if not m:
        return None, None
    nonce = int(m.group(1))
    hashes = _HASH_RE.findall(text)
    hash_hex = hashes[-1].lower() if hashes else None
    return nonce, hash_hex


def _gpu_available(gpu_bin: str) -> bool:
    return bool(gpu_bin) and os.path.exists(gpu_bin) and os.access(gpu_bin, os.X_OK)


def _record_attempt(resource: str, prefix: str, started: float,
                    nonce, range_min: int, range_max: int) -> None:
    """Registra métricas de un intento de minería (checklist §1).

    El hashrate se estima con los nonces efectivamente probados: hasta el
    nonce encontrado, o el rango completo si no hubo solución.
    """
    duration = max(time.perf_counter() - started, 1e-9)
    attempts = (nonce - range_min + 1) if nonce is not None else (range_max - range_min)
    worker_mining_tasks_total.labels(resource=resource).inc()
    worker_mining_duration_seconds.labels(
        resource=resource, prefix_len=str(len(prefix))).observe(duration)
    if attempts > 0:
        worker_hashrate_hps.labels(resource=resource).set(attempts / duration)
    if nonce is not None:
        worker_mining_success_total.labels(resource=resource).inc()


def run_miner(base: str, prefix: str, range_min: int, range_max: int, *,
              gpu_bin: Optional[str] = None, cpu_script: Optional[str] = None,
              prefer_gpu: bool = True, timeout: Optional[float] = None):
    """Ejecuta el minero sobre ``[range_min, range_max)`` buscando ``prefix``.

    Devuelve ``(nonce, hash_hex)`` o ``(None, None)`` si no hay solución en el
    rango. Intenta GPU si está disponible; ante cualquier fallo cae a CPU.
    """
    gpu_bin = gpu_bin if gpu_bin is not None else os.getenv("MINER_GPU_BIN", "")
    cpu_script = cpu_script if cpu_script is not None else os.getenv(
        "MINER_CPU_SCRIPT",
        "/app/pilar1-minero/cpu/src/brute_force.py",
    )

    if prefer_gpu and _gpu_available(gpu_bin):
        started = time.perf_counter()
        try:
            cmd = [gpu_bin, base, prefix, str(range_min), str(range_max)]
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=timeout, check=False)
            nonce, hash_hex = parse_miner_output(out.stdout)
            _record_attempt("gpu", prefix, started, nonce, range_min, range_max)
            if nonce is not None:
                log.info("GPU encontró nonce %d", nonce)
            return nonce, hash_hex
        except Exception as exc:  # noqa: BLE001
            worker_mining_tasks_total.labels(resource="gpu").inc()
            log.warning("minero GPU falló (%s); fallback a CPU", exc)

    cmd = [sys.executable, cpu_script, base, prefix, str(range_min), str(range_max)]
    started = time.perf_counter()
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                         check=False)
    nonce, hash_hex = parse_miner_output(out.stdout)
    _record_attempt("cpu", prefix, started, nonce, range_min, range_max)
    if nonce is not None:
        log.info("CPU encontró nonce %d", nonce)
    return nonce, hash_hex
