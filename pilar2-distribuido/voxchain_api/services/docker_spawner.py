"""Despliegue de mineros como contenedores Docker, para el compose local.

Es la contraparte local de ``_spawn_k8s_worker``: sin Kubernetes, registrar un
minero en la UI lo anotaba en Redis y nada más, y el usuario tenía que copiar un
comando y correr ``./run.sh worker <id>`` a mano. Ahora el API le habla al
daemon de Docker por su socket y levanta el contenedor en el mismo paso.

El contenedor es un **clon del servicio ``worker-1``** del compose (misma
imagen, mismo env, misma red) con la identidad cambiada. Clonarlo en vez de
declarar acá el env completo es lo que mantiene a los mineros dinámicos
sincronizados con el compose: si mañana cambia ``N_ZEROS``, el próximo minero
que se registre lo hereda sin tocar este archivo.

Se habla la API HTTP del Engine directo con httpx (ya es dependencia del API)
en vez de sumar el SDK de Docker por cuatro llamadas.

Seguridad: montar el socket de Docker le da al API control total del host. Es
aceptable en el compose local de desarrollo —que es el único que lo monta— y
por eso esto se habilita sólo si el socket existe y ``DOCKER_SPAWN_ENABLED``
lo prende (el compose lo hace). Apagado por defecto: correr los tests en una
máquina con Docker no debe levantar contenedores. En Kubernetes no hay socket y
el camino es el del clúster.
"""

from __future__ import annotations

import json
import logging
import os
import re

import httpx

log = logging.getLogger("voxchain.api.docker_spawner")
# httpx loguea cada request en INFO, y la reconciliación le pregunta al daemon
# cada 15 s: sin esto el log del API es una lista de GET /containers/json.
logging.getLogger("httpx").setLevel(logging.WARNING)

DOCKER_SOCKET = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
# Proyecto y servicio del compose que sirven de plantilla del minero.
COMPOSE_PROJECT = os.getenv("DOCKER_COMPOSE_PROJECT", "voxchain-pilar2")
TEMPLATE_SERVICE = os.getenv("DOCKER_WORKER_TEMPLATE_SERVICE", "worker-1")
# Cómo alcanza el minero a este API para enrolar su identidad de nodo.
WORKER_API_URL = os.getenv("DOCKER_WORKER_API_URL", "http://voxchain-api:8000")

# Etiqueta propia para reconocer los contenedores que levantó el API.
SPAWNED_LABEL = "voxchain.spawned-by"

# Variables que el clon NO hereda de la plantilla: son la identidad de
# `worker-1`, y heredarlas haría que el minero nuevo se presentara como él.
# WORKER_MODE tampoco: el modo lo decide `worker:desired_mode:<id>` y, sin
# nada ahí, el default del worker ya es standalone. Ni la respuesta por defecto
# en deliberación: `worker-1` acepta todo porque no tiene dueño que responda
# (ver docker-compose.yml), y el minero de un ciudadano lo decide su dueño.
_OVERRIDDEN = {"WORKER_ID", "WORKER_ADDRESS", "WORKER_MODE",
               "WORKER_ENROLL_TOKEN", "WORKER_PRIVKEY_PEM", "VOXCHAIN_API_URL",
               "STANDALONE_DEFAULT_DECISION"}

_NO_DOCKER_NAME = re.compile(r"[^a-zA-Z0-9_.-]+")


class DockerSpawnError(RuntimeError):
    """El daemon de Docker no pudo levantar o bajar el contenedor."""


def enabled() -> bool:
    if os.getenv("DOCKER_SPAWN_ENABLED", "false").lower() not in ("true", "1", "yes"):
        return False
    return os.path.exists(DOCKER_SOCKET)


def container_name(worker_id: str) -> str:
    """Mismo nombre que usa ``./run.sh worker``: así ``./run.sh stop`` los baja.

    Los nombres de contenedor sólo admiten ``[a-zA-Z0-9_.-]``; el ID del minero
    lo elige una persona y puede traer cualquier cosa. El nombre además es el
    DNS con el que otros mineros alcanzan a éste si coordina un equipo.
    """
    return "voxchain-worker-" + _NO_DOCKER_NAME.sub("-", worker_id).strip("-")


def _client() -> httpx.Client:
    return httpx.Client(transport=httpx.HTTPTransport(uds=DOCKER_SOCKET),
                        base_url="http://docker", timeout=15.0)


def _check(resp: httpx.Response, what: str) -> None:
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("message", resp.text)
        except Exception:  # noqa: BLE001
            detail = resp.text
        raise DockerSpawnError(f"{what}: {detail}")


def _template(c: httpx.Client) -> dict:
    """Inspección del contenedor de ``worker-1`` que sirve de molde."""
    filters = json.dumps({"label": [
        f"com.docker.compose.project={COMPOSE_PROJECT}",
        f"com.docker.compose.service={TEMPLATE_SERVICE}",
        "com.docker.compose.oneoff=False",
    ]})
    resp = c.get("/containers/json", params={"all": "true", "filters": filters})
    _check(resp, "no se pudo listar contenedores")
    found = resp.json()
    if not found:
        raise DockerSpawnError(
            f"no encontré el servicio '{TEMPLATE_SERVICE}' del proyecto "
            f"'{COMPOSE_PROJECT}' para usarlo de plantilla")
    resp = c.get(f"/containers/{found[0]['Id']}/json")
    _check(resp, "no se pudo inspeccionar la plantilla")
    return resp.json()


def _remove(c: httpx.Client, name: str) -> None:
    resp = c.delete(f"/containers/{name}", params={"force": "true"})
    if resp.status_code != 404:
        _check(resp, f"no se pudo borrar {name}")


def spawn_worker(worker_id: str, enrollment_token: str) -> str:
    """Levanta (o reemplaza) el contenedor del minero. Devuelve su nombre."""
    name = container_name(worker_id)
    with _client() as c:
        tpl = _template(c)
        networks = list((tpl.get("NetworkSettings") or {}).get("Networks") or {})
        if not networks:
            raise DockerSpawnError("la plantilla no está en ninguna red")
        network = networks[0]

        env = [e for e in (tpl["Config"].get("Env") or [])
               if e.split("=", 1)[0] not in _OVERRIDDEN]
        env += [
            f"WORKER_ID={worker_id}",
            f"WORKER_ADDRESS=http://{name}:9001",
            # La identidad de nodo nace adentro del contenedor, nunca viaja.
            "WORKER_PRIVKEY_PEM=/tmp/voxchain-node-key.pem",
            f"WORKER_ENROLL_TOKEN={enrollment_token}",
            f"VOXCHAIN_API_URL={WORKER_API_URL}",
        ]

        # Re-registrar el mismo id reemplaza el contenedor: el viejo tiene una
        # identidad de nodo que el re-registro acaba de invalidar.
        _remove(c, name)
        resp = c.post("/containers/create", params={"name": name}, json={
            "Image": tpl["Config"]["Image"],
            "Env": env,
            "Labels": {SPAWNED_LABEL: "voxchain-api",
                       "voxchain.worker-id": worker_id},
            "HostConfig": {
                "NetworkMode": network,
                "RestartPolicy": {"Name": "unless-stopped"},
            },
            "NetworkingConfig": {"EndpointsConfig": {network: {"Aliases": [name]}}},
        })
        _check(resp, f"no se pudo crear {name}")
        resp = c.post(f"/containers/{resp.json()['Id']}/start")
        _check(resp, f"no se pudo arrancar {name}")
    log.info("minero %s levantado como contenedor %s (red %s)", worker_id, name, network)
    return name


def existing_containers() -> set[str]:
    """Nombres de los contenedores de mineros que existen, corran o no."""
    filters = json.dumps({"name": ["^/voxchain-worker-"]})
    with _client() as c:
        resp = c.get("/containers/json", params={"all": "true", "filters": filters})
        _check(resp, "no se pudo listar contenedores")
        return {n.lstrip("/") for ct in resp.json() for n in ct.get("Names", [])}


def delete_worker(worker_id: str) -> None:
    """Baja el contenedor del minero, si existe. Best-effort."""
    if not enabled():
        return
    name = container_name(worker_id)
    try:
        with _client() as c:
            _remove(c, name)
        log.info("contenedor %s borrado", name)
    except Exception as exc:  # noqa: BLE001
        log.warning("no se pudo borrar el contenedor %s: %s", name, exc)
