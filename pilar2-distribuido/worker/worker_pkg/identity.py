"""Identidad propia del worker/pool para firmar respuestas de nonce (A-01 fase 2).

El minero firma con una clave **suya**, generada dentro de su propio proceso y
nunca transmitida (AGENT.md 3.1). No es la clave del ciudadano que lo registró:
antes sí lo era —el alta subía la privada del individuo al backend, que la
montaba como Secret del pod— y eso le daba a cualquiera con acceso al clúster la
capacidad de proponer y votar como esa persona, para siempre.

El vínculo con el dueño se rehace por el otro lado, sin mover claves: el
ciudadano registra el minero firmando con su clave desde el navegador, y el
minero reclama su slot presentando el token de un solo uso que recibió al
desplegarse (``WORKER_ENROLL_TOKEN``). El backend guarda ``node → dueño``, que es
lo que el NCT necesita para seguir aplicando la regla 3.4.

Configuración:

- ``WORKER_PRIVKEY_PEM``: path del PEM del nodo. Si el archivo no existe, se
  genera uno nuevo ahí. Sin la variable, el worker corre sin identidad y publica
  su ``worker_id`` textual como ``winning_node_or_pool``, como siempre.
- ``WORKER_ENROLL_TOKEN`` + ``VOXCHAIN_API_URL``: para enrolar la pubkey.
- ``WORKER_ENROLL_TOKEN_DIR``: directorio con los tokens como archivos (el Secret
  del minero montado como volumen, en los pods que despliega el API). Tiene
  prioridad sobre las variables y, a diferencia de ellas, cambia con el pod
  vivo: por eso con este directorio el worker reintenta el enrolamiento hasta
  lograrlo (``enroll_until_bound``).
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("voxchain.worker.identity")


class WorkerSigner:
    """Firma respuestas de nonce con la identidad del nodo; no-op si no hay clave."""

    def __init__(self, privkey_pem_path: str = ""):
        self._key = None
        self.pubkey = None
        self.path = privkey_pem_path
        if privkey_pem_path:
            self._key = _load_or_create(privkey_pem_path)
            from common.identity import public_key_b64

            self.pubkey = public_key_b64(self._key)
            log.info("worker firma nonces con identidad de nodo %s…", self.pubkey[:16])

    @classmethod
    def from_env(cls) -> "WorkerSigner":
        return cls(os.getenv("WORKER_PRIVKEY_PEM", ""))

    @property
    def enabled(self) -> bool:
        return self._key is not None

    def identity(self, fallback_id: str) -> str:
        """``winning_node_or_pool`` a publicar: la pubkey si firmamos, si no el id."""
        return self.pubkey if self.enabled else fallback_id

    def sign_nonce(self, voting_window_id: str, nonce: int, winner: str):
        """Firma ``voting_window_id|nonce|winner`` o devuelve ``None`` si no hay clave."""
        if not self.enabled:
            return None
        from common.identity import nonce_message, sign

        return sign(self._key, nonce_message(voting_window_id, nonce, winner))


def _load_or_create(path: str):
    """Devuelve la clave del nodo, generándola en ``path`` si todavía no existe.

    Generar es el caso normal, no el excepcional: el volumen donde vive esto es
    efímero (tmpfs), así que cada pod nace con una identidad nueva. Persistirla
    entre reinicios no aportaría nada —la identidad del nodo no acumula derechos,
    los acumula el ciudadano dueño— y sí obligaría a un volumen durable con una
    clave privada adentro.
    """
    from common.identity import generate_private_key, load_private_key, private_key_pem

    if os.path.exists(path):
        return load_private_key(path)

    key = generate_private_key()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # 0600 antes de escribir: crear con permisos laxos y ajustarlos después deja
    # una ventana en la que otro proceso del contenedor puede leer la clave.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(private_key_pem(key))
    log.info("identidad de nodo generada en %s", path)
    return key


# Archivos del Secret montado, en el orden en que se prueban.
_TOKEN_FILES = ("enrollment-token", "enrollment-token-replica")


def enrollment_tokens() -> list[str]:
    """Tokens de enrolamiento disponibles ahora, sin repetir.

    Primero los archivos de ``WORKER_ENROLL_TOKEN_DIR`` —se releen en cada
    llamada, porque el API puede reponer un token con el pod corriendo— y
    después las variables de entorno, que es como llegan en los despliegues
    viejos y en un minero levantado a mano.
    """
    tokens: list[str] = []
    directorio = os.getenv("WORKER_ENROLL_TOKEN_DIR", "")
    if directorio:
        for nombre in _TOKEN_FILES:
            try:
                with open(os.path.join(directorio, nombre), encoding="utf-8") as fh:
                    tokens.append(fh.read().strip())
            except OSError:
                continue
    tokens += [os.getenv("WORKER_ENROLL_TOKEN", ""),
               os.getenv("WORKER_ENROLL_TOKEN_REPLICA", "")]
    return list(dict.fromkeys(t for t in tokens if t))


def enroll_until_bound(worker_id: str, signer: WorkerSigner, *,
                       first_wait: float = 30.0, max_wait: float = 600.0,
                       stop=None) -> bool:
    """Reintenta el enrolamiento hasta que quede vinculado, con espera creciente.

    Es el camino del pod que **reemplaza** a otro: arranca con los tokens que
    dejó el alta, ya gastados, y el primer intento da 401. Ese rechazo hace que
    el API le deje un token nuevo en su Secret; Kubernetes actualiza el archivo
    montado en uno o dos minutos, y el reintento siguiente lo encuentra.

    La espera se duplica en cada fallo hasta ``max_wait``: si el minero se dio
    de baja, el API va a contestar 404 para siempre y no tiene sentido
    preguntarle cada 30 s. ``stop`` es un ``threading.Event`` opcional para
    cortar el ciclo (los tests; en producción el hilo muere con el proceso).
    """
    import threading

    stop = stop or threading.Event()
    espera = first_wait
    while not stop.wait(espera):
        if enroll(worker_id, signer):
            return True
        espera = min(espera * 2, max_wait)
    return False


def enroll(worker_id: str, signer: WorkerSigner) -> bool:
    """Publica la pubkey del nodo y la vincula al ciudadano dueño del minero.

    Best-effort: si falla, el worker mina igual y firma con su identidad, pero sus
    bloques quedan sin dueño imputable (y la regla 3.4 no puede alcanzarlo). Es
    preferible a que un minero no arranque por un problema de red con el API.

    Puede haber dos tokens: ``WORKER_ENROLL_TOKEN`` y
    ``WORKER_ENROLL_TOKEN_REPLICA``. Los pods de un mismo minero montan el mismo
    Secret, y cuando el minero coordina un equipo corre en dos (el que manda y
    uno en espera): cada token sirve una sola vez, así que se prueban en orden y
    el primero que el API rechaza por usado deja paso al siguiente.
    """
    tokens = enrollment_tokens()
    api = os.getenv("VOXCHAIN_API_URL", "").rstrip("/")
    if not (signer.enabled and tokens and api):
        if signer.enabled and not tokens:
            log.info("sin WORKER_ENROLL_TOKEN: el nodo firma como identidad anónima")
        return False

    import json
    import urllib.error
    import urllib.request

    ok = False
    for token in tokens:
        body = json.dumps({
            "worker_id": worker_id,
            "node_pubkey": signer.pubkey,
            "enrollment_token": token,
        }).encode()
        req = urllib.request.Request(f"{api}/api/workers/enroll", data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                ok = 200 <= resp.status < 300
        except urllib.error.HTTPError as exc:
            log.warning("enrolamiento rechazado (%s): %s", exc.code,
                        exc.read()[:200].decode("utf-8", "replace"))
            # 401 = token ya usado o vencido: el otro puede servir. Cualquier
            # otro error lo sería también con el otro token.
            if exc.code == 401:
                continue
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("no se pudo enrolar la identidad de nodo: %s", exc)
            return False
        if ok:
            break

    if ok:
        log.info("identidad de nodo %s… enrolada para %s", signer.pubkey[:16], worker_id)
        # Los tokens son de un solo uso: no dejarlos en el entorno del proceso,
        # donde cualquier volcado o subproceso los arrastraría.
        os.environ.pop("WORKER_ENROLL_TOKEN", None)
        os.environ.pop("WORKER_ENROLL_TOKEN_REPLICA", None)
    return ok
