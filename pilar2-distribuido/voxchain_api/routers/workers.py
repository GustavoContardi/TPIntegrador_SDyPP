import hashlib
import hmac
import json
import os
import re
import secrets
import httpx
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Header

from voxchain_api.config import config
from voxchain_api.models import (
    PoolHealth,
    PoolPolicy,
    WorkerStatus,
    WorkerSwitchRequest,
    RegisterWorkerRequest,
    EnrollNodeRequest,
)
from voxchain_api.services import docker_spawner
from voxchain_api.services.redis_reader import RedisReader
from voxchain_api.services.rabbitmq_publisher import RabbitMQPublisher
from voxchain_api.services.teams_store import TeamsStore
from voxchain_api.services.worker_control import (
    clear_desired_mode,
    dispatch_switch_mode,
)
from common.identity import verify
from voxchain_api.services.revocation import reject_if_revoked
from datetime import datetime, timezone

router = APIRouter(prefix="/api/workers", tags=["workers"])

logger = logging.getLogger("voxchain.api.workers")

# Kubernetes dynamic pod spawning configuration
K8S_ENABLED = False

# Namespace DESTINO de los workers spawneados. Con KUBECONFIG apuntando al
# cluster k3s externo, el namespace del propio pod de la API (voxchain, en
# GKE) NO es el destino: hay que fijarlo explícitamente vía env.
NAMESPACE = os.getenv("WORKER_NAMESPACE", "")
if not NAMESPACE:
    NAMESPACE = "g-git-push-cv"
    if os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/namespace"):
        try:
            with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace", "r") as f:
                NAMESPACE = f.read().strip()
        except Exception as e:
            logger.warning(f"Failed to read Kubernetes namespace from secret: {e}")

# Imagen del worker y si el spawn reserva GPU (los workers del cluster k3s
# actual NO reservan nvidia.com/gpu; pedirla dejaría el pod Pending).
WORKER_IMAGE = os.getenv(
    "WORKER_IMAGE",
    "southamerica-east1-docker.pkg.dev/voxchain-unlu/voxchain-images/worker-gpu:latest",
)
SPAWN_REQUEST_GPU = os.getenv("SPAWN_REQUEST_GPU", "false").lower() == "true"

# URL con la que el pod del minero alcanza a esta API para enrolar su identidad.
INTERNAL_API_URL = os.getenv("INTERNAL_API_URL", "http://voxchain-api.voxchain.svc.cluster.local:8000")

# Dónde el pod del minero encuentra sus tokens de enrolamiento (Secret montado).
ENROLL_TOKEN_DIR = "/etc/voxchain/enroll"

# Vida del token de enrolamiento. Corta a propósito: sólo tiene que sobrevivir el
# arranque del pod. Un pod que llega con el token vencido o gastado recibe uno
# nuevo en su Secret (`reissue_enrollment_token`); un minero levantado a mano se
# resuelve re-registrando el mismo id.
ENROLL_TOKEN_TTL = int(os.getenv("ENROLL_TOKEN_TTL", "900"))


def _is_valid_pubkey(pubkey_b64: str) -> bool:
    """¿Es ``pubkey_b64`` una clave pública EC P-256 en SPKI DER base64?"""
    import base64

    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import load_der_public_key

        pub = load_der_public_key(base64.b64decode(pubkey_b64))
        return (isinstance(pub, ec.EllipticCurvePublicKey)
                and isinstance(pub.curve, ec.SECP256R1))
    except Exception:
        return False


# Clave del token de enrolamiento de un minero. Hay dos: la del pod original y
# la de la réplica en espera de un coordinador de equipo. Son dos porque los
# pods de un Deployment montan el mismo Secret y el token es de un solo uso: con
# uno solo, la primera réplica en enrolarse lo quemaba y la otra quedaba sin
# dueño — y si ésa llegaba a líder, los bloques de su equipo dejaban de
# imputarse al fundador.
ENROLL_KEY = "worker:enroll:{worker_id}"
ENROLL_REPLICA_KEY = "worker:enroll:{worker_id}:replica"


def _issue_enrollment_token(redis_client, worker_id: str, *,
                            replica: bool = False, only_if_free: bool = False) -> str:
    """Emite un token de un solo uso para que un pod reclame el slot de nodo.

    En Redis queda sólo el SHA-256 del token: si alguien lee la base no obtiene
    un token usable. El valor en claro existe únicamente en la respuesta a este
    alta y en el Secret que monta el pod. ``replica`` elige el segundo slot
    (ver ``ENROLL_REPLICA_KEY``).

    ``only_if_free`` no pisa un token vigente en ese slot y devuelve ``""`` si lo
    había. Es atómico (SET NX): dos pedidos simultáneos no pueden emitir dos
    tokens y dejar en el Secret uno distinto del que quedó en Redis.
    """
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    key = ENROLL_REPLICA_KEY if replica else ENROLL_KEY
    stored = redis_client.set(key.format(worker_id=worker_id), digest,
                              ex=ENROLL_TOKEN_TTL, nx=only_if_free)
    return token if stored else ""


def _consume_enrollment_token(redis_client, worker_id: str, token: str) -> bool:
    """Valida y **quema** el token. Un token sirve para un solo enrolamiento.

    Se prueba contra los dos slots; se quema sólo el que coincidió.
    """
    digest = hashlib.sha256((token or "").encode()).hexdigest()
    for key in (ENROLL_KEY, ENROLL_REPLICA_KEY):
        key = key.format(worker_id=worker_id)
        stored = redis_client.get(key)
        if not stored:
            continue
        if isinstance(stored, bytes):
            stored = stored.decode("utf-8")
        # compare_digest y no ==: el token es un secreto y la comparación no debe
        # filtrar por dónde difiere.
        if hmac.compare_digest(stored, digest):
            redis_client.delete(key)
            return True
    return False

try:
    from kubernetes import client, config as k8s_config
    
    # 1. Prioritize custom Kubeconfig env var (for remote cluster connection)
    kubeconfig_env = os.getenv("KUBECONFIG")
    if kubeconfig_env and os.path.exists(kubeconfig_env):
        try:
            k8s_config.load_kube_config(config_file=kubeconfig_env)
            K8S_ENABLED = True
            logger.info(f"Loaded remote Kubernetes config from KUBECONFIG={kubeconfig_env}. Namespace: {NAMESPACE}")
        except Exception as e:
            logger.error(f"Failed to load remote Kubernetes config from {kubeconfig_env}: {e}")

    # 2. Fallback to in-cluster context
    if not K8S_ENABLED:
        try:
            k8s_config.load_incluster_config()
            K8S_ENABLED = True
            logger.info(f"Loaded in-cluster Kubernetes config. Namespace: {NAMESPACE}")
        except Exception:
            pass

    # 3. Fallback to local default kubeconfig
    if not K8S_ENABLED:
        try:
            k8s_config.load_kube_config()
            K8S_ENABLED = True
            logger.info(f"Loaded default local kube config. Namespace: {NAMESPACE}")
        except Exception:
            logger.warning("No Kubernetes config found. Dynamic Pod spawning is disabled.")
except ImportError:
    logger.warning("kubernetes python package not installed. Dynamic Pod spawning is disabled.")


_NO_RFC1123 = re.compile(r"[^a-z0-9-]+")


def _k8s_slug(worker_id: str) -> str:
    """Nombre de objeto de Kubernetes derivado del ``worker_id``.

    Los nombres de recursos son subdominios RFC 1123: sólo ``[a-z0-9-]`` y
    empiezan y terminan en alfanumérico. El ``worker_id`` lo elige el usuario y
    puede traer mayúsculas o cualquier otra cosa, así que usarlo crudo hacía
    fallar el alta con un 422 del API server ("GustavoContardi" es el caso que
    lo destapó) y el error salía a la cara del usuario en el formulario.

    El ``worker_id`` original NO se toca: sigue siendo el de las claves de Redis,
    el env ``WORKER_ID`` y la UI. Esto es sólo el nombre de los objetos.

    Cuando la normalización cambia el id se le agrega un sufijo con su hash,
    porque si no ``GustavoContardi`` y ``gustavo.contardi`` colapsarían en el
    mismo Secret y el segundo registro fallaría con un AlreadyExists.
    """
    slug = _NO_RFC1123.sub("-", worker_id.lower()).strip("-")[:40].strip("-")
    if slug != worker_id:
        digest = hashlib.sha1(worker_id.encode()).hexdigest()[:8]
        slug = f"{slug}-{digest}".lstrip("-")
    return slug


POOL_PORT = 9001


def _k8s_service_name(slug: str) -> str:
    """Nombre del Service del minero.

    Un Service es una etiqueta DNS RFC 1035, más estricta que el resto de los
    nombres: tiene que **empezar con letra**. El slug puede empezar con un
    dígito, así que el prefijo no es decorativo.
    """
    return f"worker-svc-{slug}"


def _k8s_service_address(slug: str) -> str:
    """URL estable del coordinador embebido del minero, vía su Service.

    Se usa ``<svc>.<namespace>.svc`` y no el FQDN con ``cluster.local``: el
    dominio del clúster es configurable, y la búsqueda DNS del pod completa el
    resto en cualquier clúster.
    """
    return f"http://{_k8s_service_name(slug)}.{NAMESPACE}.svc:{POOL_PORT}"


def _create_k8s_service(core_api, slug: str) -> Optional[str]:
    """Crea el Service del minero y devuelve su URL, o ``None`` si no se pudo.

    **Qué resuelve.** Los miembros de un equipo le piden trabajo a su
    coordinador por HTTP, y antes lo hacían contra la IP del pod. Esa IP cambia
    cada vez que el pod se reemplaza (un nodo que se cae, una evicción, un
    redespliegue), y la corrección de la URL de los miembros sólo corría cuando
    alguien consultaba los equipos en el API: si nadie abría la UI, el equipo
    quedaba pidiéndole trabajo a una dirección muerta indefinidamente. Con un
    Service el nombre no cambia nunca y el pod nuevo lo hereda solo.

    **Por qué uno por minero y no uno por equipo.** Cualquier minero puede
    terminar coordinando un equipo, y su dirección tiene que existir desde que
    arranca: el worker la fija al iniciar y la publica en su estado. Crearlo al
    fundar el equipo obligaría a reiniciar el pod para que la anuncie. Un
    Service sin tráfico cuesta una regla de red, no un proceso.

    **Readiness.** El Service manda tráfico sólo a pods listos, y el pod de un
    `pool-coordinator` está listo sólo si tiene el lease de su pool (``/ready``
    del worker). Con un solo pod es casi siempre él; si hay dos a la vez —un
    rollout— los mineros van al que manda.

    **Si falla, se sigue.** La cuenta con la que el API opera sobre el clúster
    externo no es nuestra y puede no tener permiso sobre Services. Sin Service
    el minero anuncia la IP de su pod, que es como funcionaba antes: peor ante
    un reemplazo del pod, pero el alta no se cae por esto. Un 409 es el Service
    de un alta anterior del mismo id, y sirve igual.
    """
    name = _k8s_service_name(slug)
    body = client.V1Service(
        api_version="v1",
        kind="Service",
        metadata=client.V1ObjectMeta(name=name, namespace=NAMESPACE),
        spec=client.V1ServiceSpec(
            selector={"app": f"worker-gpu-{slug}"},
            ports=[client.V1ServicePort(name="pool", port=POOL_PORT,
                                        target_port=POOL_PORT)],
        ),
    )
    try:
        logger.info("Creating K8s Service %s in namespace %s...", name, NAMESPACE)
        core_api.create_namespaced_service(namespace=NAMESPACE, body=body)
    except Exception as e:  # noqa: BLE001
        if getattr(e, "status", None) != 409:
            logger.warning("no se pudo crear el Service %s (%s): el minero "
                           "anunciará la IP de su pod", name, e)
            return None
    return _k8s_service_address(slug)


def _spawn_k8s_worker(worker_id: str, enrollment_token: str):
    """Levanta el Deployment del minero con un token de enrolamiento de un solo uso.

    Antes acá se montaba la **clave privada del ciudadano**, subida desde el
    navegador en el alta: cualquiera con acceso al Secret podía proponer y votar
    como esa persona para siempre, que es exactamente lo que 3.1 prohíbe. Ahora
    el pod arranca sin ninguna clave: genera la suya al iniciar y usa el token
    sólo para reclamar el slot de nodo de este `worker_id`. Si el token se filtra,
    el daño máximo es que otro proceso ocupe ese slot — no la suplantación
    permanente de un individuo.
    """
    if not K8S_ENABLED:
        logger.info("Kubernetes is not enabled, skipping dynamic spawner.")
        return

    slug = _k8s_slug(worker_id)
    core_api = client.CoreV1Api()
    apps_api = client.AppsV1Api()

    # 0. Service con nombre estable para el coordinador embebido. Va antes que
    # el Deployment porque su dirección entra en el env del pod. Si el alta
    # falla más adelante no se borra: sin pods no enruta nada, y la próxima alta
    # del mismo id lo reutiliza.
    service_address = _create_k8s_service(core_api, slug)

    # 1. Secret con el token de enrolamiento (NO una clave privada).
    secret_name = f"secret-{slug}"
    secret_body = client.V1Secret(
        api_version="v1",
        kind="Secret",
        metadata=client.V1ObjectMeta(name=secret_name, namespace=NAMESPACE),
        string_data={"enrollment-token": enrollment_token}
    )
    
    # 2. Create Deployment for the worker pod
    deployment_name = f"worker-dep-{slug}"
    
    # El env replica el de worker-deployment.yaml del gpu-cluster: AMQPS 5671
    # con CA propia (el LB externo no expone el puerto plano) y Redis de GKE
    # con password, para que el worker reporte su estado al backend.
    resources = (client.V1ResourceRequirements(limits={"nvidia.com/gpu": "1"})
                 if SPAWN_REQUEST_GPU
                 else client.V1ResourceRequirements(
                     requests={"cpu": "100m", "memory": "256Mi"},
                     limits={"cpu": "500m", "memory": "512Mi"}))
    container = client.V1Container(
        name="gpu-miner",
        image=WORKER_IMAGE,
        image_pull_policy="Always",
        env=[
            client.V1EnvVar(name="WORKER_ID", value=worker_id),
            client.V1EnvVar(name="WORKER_MODE", value="standalone"),
            # La clave la genera el propio pod en este path (volumen efímero,
            # no un Secret): nace y muere con el minero y nunca la vio nadie más.
            client.V1EnvVar(name="WORKER_PRIVKEY_PEM", value="/app/keys/node-key.pem"),
            # Los tokens de enrolamiento se leen de archivos del Secret montado
            # como volumen, no de variables de entorno. Una variable se fija al
            # arrancar el contenedor; el volumen, Kubernetes lo actualiza en el
            # pod vivo. Es lo que deja enrolarse a un pod que reemplaza a otro:
            # arranca con tokens gastados, el API le repone uno en el Secret
            # (`reissue_enrollment_token`) y el worker lo ve al reintentar.
            client.V1EnvVar(name="WORKER_ENROLL_TOKEN_DIR", value=ENROLL_TOKEN_DIR),
            client.V1EnvVar(name="VOXCHAIN_API_URL", value=INTERNAL_API_URL),
            # Dirección con la que los mineros de su equipo lo alcanzan si el
            # usuario lo promueve a coordinador: el nombre de su Service, que
            # sobrevive al reemplazo del pod. Sin Service (la cuenta del clúster
            # no pudo crearlo) se cae a la IP del pod, que cambia en cada
            # reemplazo. El worker_id no resuelve a nada dentro del clúster.
            client.V1EnvVar(
                name="MY_POD_IP",
                value_from=client.V1EnvVarSource(
                    field_ref=client.V1ObjectFieldSelector(field_path="status.podIP")
                )
            ),
            client.V1EnvVar(name="WORKER_ADDRESS",
                            value=service_address or f"http://$(MY_POD_IP):{POOL_PORT}"),
            client.V1EnvVar(
                name="RABBITMQ_USER",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(name="rabbitmq-credentials", key="username")
                )
            ),
            client.V1EnvVar(
                name="RABBITMQ_PASS",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(name="rabbitmq-credentials", key="password")
                )
            ),
            client.V1EnvVar(
                name="RABBITMQ_HOST",
                value_from=client.V1EnvVarSource(
                    config_map_key_ref=client.V1ConfigMapKeySelector(name="worker-config", key="rabbitmq-host")
                )
            ),
            client.V1EnvVar(name="RABBITMQ_URL", value="amqps://$(RABBITMQ_USER):$(RABBITMQ_PASS)@$(RABBITMQ_HOST):5671/"),
            client.V1EnvVar(name="RABBITMQ_TLS_CA_PATH", value="/etc/rabbitmq-ca/ca.crt"),
            client.V1EnvVar(name="RABBITMQ_TLS_SERVER_NAME", value="rabbitmq.voxchain.svc.cluster.local"),
            client.V1EnvVar(
                name="REDIS_HOST",
                value_from=client.V1EnvVarSource(
                    config_map_key_ref=client.V1ConfigMapKeySelector(name="worker-config", key="redis-host", optional=True)
                )
            ),
            client.V1EnvVar(
                name="REDIS_PASSWORD",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(name="redis-credentials", key="password", optional=True)
                )
            ),
            client.V1EnvVar(name="REDIS_URL", value="redis://:$(REDIS_PASSWORD)@$(REDIS_HOST):6379/0"),
            client.V1EnvVar(name="WORKER_CAPACITY", value="1"),
            client.V1EnvVar(name="LOG_DIR", value="/var/log/voxchain"),
            # Logs directo a Loki, igual que los mineros de gpu-cluster/: la URL
            # y la contraseña las carga 04-gpu-workers, y sin ellas el minero
            # loguea sólo a stdout y al archivo.
            client.V1EnvVar(
                name="LOKI_PUSH_URL",
                value_from=client.V1EnvVarSource(
                    config_map_key_ref=client.V1ConfigMapKeySelector(name="worker-config", key="loki-push-url", optional=True)
                )
            ),
            client.V1EnvVar(name="LOKI_PUSH_USER", value="voxchain-logs"),
            client.V1EnvVar(
                name="LOKI_PUSH_PASSWORD",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(name="loki-push-credentials", key="password", optional=True)
                )
            ),
            client.V1EnvVar(name="LOKI_CLUSTER", value="k3s"),
        ],
        ports=[
            client.V1ContainerPort(container_port=8080, name="health"),
            # Puerto del coordinator embebido: el pod lo abre en cuanto pasa a
            # coordinar un equipo.
            client.V1ContainerPort(container_port=POOL_PORT, name="pool"),
        ],
        # `/ready` y no `/health`: decide si el Service le manda tráfico, y un
        # coordinador sin el lease de su pool está sano pero no debe recibirlo.
        # Sin livenessProbe sobre `/ready` a propósito: si no, Kubernetes
        # reiniciaría en bucle a un coordinador que sólo está esperando el lease.
        readiness_probe=client.V1Probe(
            http_get=client.V1HTTPGetAction(path="/ready", port=8080),
            initial_delay_seconds=5,
            period_seconds=5,
            failure_threshold=2,
        ),
        resources=resources,
        security_context=client.V1SecurityContext(
            allow_privilege_escalation=False,
            # Escribibles sólo los emptyDir: la clave, /tmp y los logs.
            read_only_root_filesystem=True,
            capabilities=client.V1Capabilities(drop=["ALL"]),
        ),
        volume_mounts=[
            client.V1VolumeMount(name="key-volume", mount_path="/app/keys"),
            client.V1VolumeMount(name="tmp", mount_path="/tmp"),
            client.V1VolumeMount(name="rabbitmq-ca", mount_path="/etc/rabbitmq-ca", read_only=True),
            client.V1VolumeMount(name="enroll", mount_path=ENROLL_TOKEN_DIR, read_only=True),
            client.V1VolumeMount(name="logs", mount_path="/var/log/voxchain"),
        ]
    )

    template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels={"app": f"worker-gpu-{slug}"}),
        spec=client.V1PodSpec(
            termination_grace_period_seconds=10,
            security_context=client.V1PodSecurityContext(
                run_as_non_root=True, run_as_user=1000, run_as_group=1000,
                fs_group=1000,
                seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
            ),
            containers=[container],
            # Si el minero coordina un equipo corre con una réplica en espera
            # (`scale_k8s_coordinator`), y una réplica en el mismo nodo no
            # sobrevive a la caída del nodo, que es justo el caso que cubre.
            # *Preferred* y no *required*: en un clúster de un solo nodo la
            # réplica tiene que poder programarse igual.
            affinity=client.V1Affinity(
                pod_anti_affinity=client.V1PodAntiAffinity(
                    preferred_during_scheduling_ignored_during_execution=[
                        client.V1WeightedPodAffinityTerm(
                            weight=100,
                            pod_affinity_term=client.V1PodAffinityTerm(
                                topology_key="kubernetes.io/hostname",
                                label_selector=client.V1LabelSelector(
                                    match_labels={"app": f"worker-gpu-{slug}"}),
                            ),
                        )
                    ]
                )
            ),
            volumes=[
                # Escribible y efímero: el worker genera acá su par al arrancar.
                client.V1Volume(
                    name="key-volume",
                    empty_dir=client.V1EmptyDirVolumeSource(medium="Memory")
                ),
                client.V1Volume(
                    name="rabbitmq-ca",
                    secret=client.V1SecretVolumeSource(secret_name="rabbitmq-ca")
                ),
                # Un archivo por slot: `enrollment-token` y, mientras el minero
                # coordina un equipo, `enrollment-token-replica`. Sin `items`:
                # un slot que no existe es un archivo que no está, no un error.
                client.V1Volume(
                    name="enroll",
                    secret=client.V1SecretVolumeSource(secret_name=secret_name,
                                                       default_mode=0o440)
                ),
                client.V1Volume(
                    name="logs",
                    empty_dir=client.V1EmptyDirVolumeSource()
                ),
                client.V1Volume(
                    name="tmp",
                    empty_dir=client.V1EmptyDirVolumeSource()
                ),
            ]
        )
    )
    
    spec = client.V1DeploymentSpec(
        replicas=1,
        selector=client.V1LabelSelector(match_labels={"app": f"worker-gpu-{slug}"}),
        template=template
    )
    
    deployment_body = client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(name=deployment_name, namespace=NAMESPACE),
        spec=spec
    )
    
    try:
        logger.info(f"Creating K8s Secret {secret_name} in namespace {NAMESPACE}...")
        core_api.create_namespaced_secret(namespace=NAMESPACE, body=secret_body)
        
        logger.info(f"Creating K8s Deployment {deployment_name} in namespace {NAMESPACE}...")
        apps_api.create_namespaced_deployment(namespace=NAMESPACE, body=deployment_body)
    except Exception as e:
        logger.error(f"Failed to create K8s resources for worker {worker_id}: {e}")
        try:
            core_api.delete_namespaced_secret(name=secret_name, namespace=NAMESPACE)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Failed to spawn Kubernetes worker node: {e}")


# Pods del coordinador de un equipo: el que manda y uno en espera. 1 = sin
# réplica, que es como corre cualquier minero que no coordina.
TEAM_COORDINATOR_REPLICAS = max(1, int(os.getenv("TEAM_COORDINATOR_REPLICAS", "2")))


def scale_k8s_coordinator(worker_id: str, replicas: int, redis_client) -> bool:
    """Ajusta los pods del minero: ``replicas`` al coordinar un equipo, 1 al dejarlo.

    **Por qué.** El coordinador de un equipo lo designa una persona, así que
    ningún miembro puede tomar su lugar: si su pod cae, el equipo no mina hasta
    que Kubernetes lo reponga (programarlo, bajar la imagen, arrancar). Con una
    segunda réplica del mismo minero en espera, la caída dura lo que tarda en
    vencer el lease del pool (10 s), y la réplica ya tiene fragmentada la
    ventana en curso. Cuál de las dos manda lo decide el lease; el Service del
    minero sólo le manda tráfico a ésa (readiness en `/ready`).

    **Por qué sólo al coordinar.** Réplicas de un standalone no suman nada:
    barren el mismo rango y encuentran el mismo nonce. Por eso esto se llama al
    fundar y al disolver un equipo, no al dar de alta el minero.

    **Enrolamiento.** Antes de escalar se emite un token nuevo para la réplica y
    se escribe en el Secret del minero (`enrollment-token-replica`): los pods
    montan el mismo Secret y el token es de un solo uso, así que sin un segundo
    token la réplica minaría sin dueño.

    Best-effort: sin Kubernetes, o si el minero no tiene Deployment nuestro (lo
    levantó el usuario a mano), no hace nada. Devuelve si escaló.
    """
    if not K8S_ENABLED:
        return False
    slug = _k8s_slug(worker_id)
    deployment_name = f"worker-dep-{slug}"
    secret_name = f"secret-{slug}"
    core_api = client.CoreV1Api()
    apps_api = client.AppsV1Api()
    try:
        apps_api.read_namespaced_deployment(name=deployment_name, namespace=NAMESPACE)
    except Exception as e:  # noqa: BLE001
        # 404: el minero no lo desplegamos nosotros. Emitir un token acá pisaría
        # el que el usuario todavía no usó para enrolar su proceso manual.
        logger.info("minero %s sin Deployment en %s (%s): sin réplica",
                    worker_id, NAMESPACE, getattr(e, "status", e))
        return False

    if replicas > 1:
        token = _issue_enrollment_token(redis_client, worker_id, replica=True)
        try:
            core_api.patch_namespaced_secret(
                name=secret_name, namespace=NAMESPACE,
                body={"stringData": {"enrollment-token-replica": token}})
        except Exception as e:  # noqa: BLE001
            # Sin token la réplica mina igual, sólo que como nodo anónimo.
            redis_client.delete(ENROLL_REPLICA_KEY.format(worker_id=worker_id))
            logger.warning("no se pudo dejar el token de la réplica de %s: %s",
                           worker_id, e)
    try:
        apps_api.patch_namespaced_deployment_scale(
            name=deployment_name, namespace=NAMESPACE,
            body={"spec": {"replicas": replicas}})
    except Exception as e:  # noqa: BLE001
        logger.warning("no se pudo escalar %s a %d réplicas: %s",
                       deployment_name, replicas, e)
        return False
    logger.info("minero %s: %d réplica(s)", worker_id, replicas)
    return True


def reissue_enrollment_token(redis_client, worker_id: str) -> bool:
    """Deja un token nuevo en el Secret de un minero cuyo pod no pudo enrolarse.

    **El problema.** Los tokens de enrolamiento son de un solo uso y vencen a los
    `ENROLL_TOKEN_TTL` segundos. Un pod que **reemplaza** a otro —se cayó el
    nodo, hubo una evicción, se reinició el pod entero— arranca con los tokens
    que dejó el alta, ya gastados o vencidos, y queda sin dueño: mina, pero el
    NCT no puede atribuir sus bloques a nadie ni aplicarle la regla 3.4 (el
    autor no gana su propia ley). Con la réplica del coordinador es lo esperable,
    no un caso raro.

    **La solución.** El pod monta el Secret como volumen, que Kubernetes
    actualiza en pods que ya corren, y reintenta el enrolamiento releyéndolo.
    Cuando el API le rechaza un token, llama acá, y esto deja uno nuevo en el
    Secret. En uno o dos minutos el archivo cambia en el pod y el reintento pasa.

    **Por qué no le da nada a quien no debe.** El token nuevo aterriza en el
    Secret, no en la respuesta: usarlo exige poder leer el Secret, exactamente
    como el del alta. Quien no pueda, a lo sumo provoca que se emita uno que no
    puede ver. Y no se emite si ya hay uno vigente en cualquiera de los dos
    slots —puede ser el que el pod todavía no terminó de recibir—, así que es
    como mucho un token cada `ENROLL_TOKEN_TTL` por minero.

    Sólo aplica a mineros con Deployment nuestro en Kubernetes: a uno levantado
    a mano no tenemos dónde dejarle el token, y el del compose local lo relanza
    `reconcile_docker_workers` con un token nuevo. Devuelve si emitió.
    """
    if not K8S_ENABLED:
        return False
    if redis_client.exists(ENROLL_REPLICA_KEY.format(worker_id=worker_id)):
        return False
    slug = _k8s_slug(worker_id)
    try:
        client.AppsV1Api().read_namespaced_deployment(
            name=f"worker-dep-{slug}", namespace=NAMESPACE)
    except Exception:  # noqa: BLE001
        return False
    token = _issue_enrollment_token(redis_client, worker_id, only_if_free=True)
    if not token:
        return False
    try:
        client.CoreV1Api().patch_namespaced_secret(
            name=f"secret-{slug}", namespace=NAMESPACE,
            body={"stringData": {"enrollment-token": token}})
    except Exception as e:  # noqa: BLE001
        redis_client.delete(ENROLL_KEY.format(worker_id=worker_id))
        logger.warning("no se pudo reponer el token de enrolamiento de %s: %s",
                       worker_id, e)
        return False
    logger.info("minero %s: token de enrolamiento repuesto en su Secret", worker_id)
    return True


def _delete_k8s_worker(worker_id: str):
    if not K8S_ENABLED:
        return
        
    slug = _k8s_slug(worker_id)
    secret_name = f"secret-{slug}"
    deployment_name = f"worker-dep-{slug}"

    core_api = client.CoreV1Api()
    apps_api = client.AppsV1Api()
    
    try:
        logger.info(f"Deleting K8s Deployment {deployment_name} in namespace {NAMESPACE}...")
        apps_api.delete_namespaced_deployment(name=deployment_name, namespace=NAMESPACE)
    except Exception as e:
        logger.warning(f"Failed to delete Deployment {deployment_name}: {e}")
        
    try:
        logger.info(f"Deleting K8s Secret {secret_name} in namespace {NAMESPACE}...")
        core_api.delete_namespaced_secret(name=secret_name, namespace=NAMESPACE)
    except Exception as e:
        logger.warning(f"Failed to delete Secret {secret_name}: {e}")

    service_name = _k8s_service_name(slug)
    try:
        logger.info(f"Deleting K8s Service {service_name} in namespace {NAMESPACE}...")
        core_api.delete_namespaced_service(name=service_name, namespace=NAMESPACE)
    except Exception as e:
        logger.warning(f"Failed to delete Service {service_name}: {e}")


# Worker admin URLs - these should be configured via environment variables
# Use Docker service names when running in docker-compose
WORKER_ADMIN_URLS = [
    "http://worker-pool-coordinator:9090",
    "http://worker-1:9090",
    "http://worker-2:9090",
]

# Mapping from worker_id to Docker service name for pool coordinators
POOL_COORDINATOR_MAPPING = {
    "pool-coordinator-1": "worker-pool-coordinator",
    "worker-2": "worker-2",  # worker-2 can also become a pool coordinator
}

# Mineros precargados del docker-compose (dueño `default`). En Kubernetes la
# lista viene vacía (PRELOADED_WORKER_IDS en voxchain-config): ahí no existen, y
# listarlos mostraba mineros fantasma y dejaba abierto el camino por cabecera.
ALL_REGISTERED_WORKER_IDS = list(config.PRELOADED_WORKER_IDS)

# Mapping from owner_id to their owned workers
OWNER_WORKERS_MAPPING = {"default": ALL_REGISTERED_WORKER_IDS} if ALL_REGISTERED_WORKER_IDS else {}


# --- Autorización de acciones de administración (AGENT.md 3.1) ---------------
#
# Estas acciones se autorizaban comparando la cabecera ``X-Owner-Id`` contra el
# dueño guardado. La cabecera la elige quien llama y la pubkey es pública —está
# en cada bloque de la cadena—, así que era autorización por **declarar** una
# identidad, no por probarla: cualquiera podía sacarle un minero de su equipo a
# otro, o reescribirle la agenda a su pool, con un curl.
#
# Ahora se prueba la posesión de la clave, con la misma forma de mensaje que ya
# usan el alta y la baja: ``<recurso>|<acción>|<timestamp>``.

ADMIN_SWITCH_MODE = "switch-mode"
ADMIN_POOL_POLICY = "pool-policy"
ADMIN_JOIN_TEAM = "join-team"
ADMIN_LEAVE_TEAM = "leave-team"
ADMIN_CREATE_TEAM = "create-team"
ADMIN_SET_CATEGORIES = "set-categories"
ADMIN_SET_DEFAULT_DECISION = "set-default-decision"
ADMIN_DISSOLVE_TEAM = "dissolve-team"


def get_signature(x_signature: str = Header(None, alias="X-Signature")) -> Optional[str]:
    return x_signature


def get_signature_timestamp(x_timestamp: str = Header(None, alias="X-Timestamp")) -> Optional[str]:
    return x_timestamp


def _burn_signature(redis_client, signature: str) -> None:
    """Consume una firma: la misma no autoriza dos veces.

    Sin esto, la firma sigue siendo válida durante toda la ventana de frescura
    (``PROPOSAL_MAX_AGE_SECONDS``), y quien la haya visto pasar puede repetir la
    acción — volver a sacar del equipo a un minero que su dueño acaba de meter,
    por ejemplo. El TTL es el mismo de la ventana: pasado ese punto la firma se
    rechaza por vieja y la marca ya no hace falta.
    """
    ttl = max(int(float(os.getenv("PROPOSAL_MAX_AGE_SECONDS", "300"))), 1)
    marca = f"sig:used:{hashlib.sha256(signature.encode()).hexdigest()}"
    if not redis_client.set(marca, "1", nx=True, ex=ttl):
        raise HTTPException(status_code=401, detail="Firma ya utilizada")


def require_signed_action(redis_client, resource_id: str, action: str,
                          owner_pubkey: str, signature: Optional[str],
                          timestamp: Optional[str]) -> None:
    """Exige una firma válida del dueño sobre ``resource_id|action|timestamp``."""
    if not signature or not timestamp:
        raise HTTPException(
            status_code=401,
            detail=(f"La acción '{action}' requiere tu firma. Enviá las cabeceras "
                    "X-Signature y X-Timestamp."),
        )
    _verify_timestamp_freshness(timestamp)
    reject_if_revoked(redis_client, owner_pubkey)
    msg = f"{resource_id}|{action}|{timestamp}".encode()
    if not verify(owner_pubkey, msg, signature):
        raise HTTPException(status_code=401, detail="Firma inválida")
    _burn_signature(redis_client, signature)


def authorize_worker_action(redis_client, worker_id: str, action: str,
                            owner_id: str, signature: Optional[str],
                            timestamp: Optional[str]) -> str:
    """Autoriza una acción sobre un minero y devuelve el dueño autenticado.

    Los mineros precargados del desarrollo local siguen el camino viejo: su
    dueño es ``default``, no una clave, y se autorizan por cabecera. Los dos
    caminos son **disjuntos**: esos ids están reservados y el alta los rechaza
    con 409, así que ningún minero de un ciudadano real cae acá.
    """
    if worker_id in OWNER_WORKERS_MAPPING.get(owner_id, []):
        return owner_id

    registered = redis_client.get(f"worker:owner:{worker_id}")
    if isinstance(registered, bytes):
        registered = registered.decode("utf-8")
    if not registered or registered != owner_id:
        # Cortesía, no defensa: la cabecera la elige quien llama, así que este
        # chequeo no detiene a nadie. Existe para que quien se equivocó de
        # identidad lea "no es tuyo" en vez de "firma inválida". La defensa es
        # la firma de abajo, y va contra el dueño guardado en Redis — declarar
        # ser otro en la cabecera no cambia contra qué clave se verifica.
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to modify this worker")

    require_signed_action(redis_client, worker_id, action, registered,
                          signature, timestamp)
    return registered


def authorize_owner_action(redis_client, resource_id: str, action: str,
                           owner_of_resource: str, owner_id: str,
                           signature: Optional[str],
                           timestamp: Optional[str]) -> None:
    """Igual que la anterior, para recursos cuyo dueño ya se conoce (un equipo).

    Un dueño que no es una clave P-256 es el ``default`` del desarrollo local:
    se compara por igualdad, como antes. Uno que sí lo es tiene que firmar.
    """
    if owner_of_resource != owner_id:
        # Cortesía, no defensa (ver `authorize_worker_action`).
        raise HTTPException(status_code=403,
                            detail="No sos el dueño de este recurso")
    if not _is_valid_pubkey(owner_of_resource):
        return
    require_signed_action(redis_client, resource_id, action, owner_of_resource,
                          signature, timestamp)


def get_owner_id(owner_id: str = Header(None, alias="X-Owner-Id")) -> str:
    """Get the owner ID from the header, default to 'default' for local dev."""
    if owner_id is None:
        return "default"
    return owner_id


def get_redis_reader() -> RedisReader:
    return RedisReader()


def get_rabbitmq_publisher() -> RabbitMQPublisher:
    return RabbitMQPublisher()


def _annotate_team(statuses: list[WorkerStatus], redis_client) -> list[WorkerStatus]:
    """Anota cada minero con el equipo al que pertenece, si pertenece a alguno.

    La UI necesita distinguir de un vistazo quién mina solo (competitivo) y
    quién está en un equipo (cooperativo), y el modo del worker por sí solo no
    alcanza: ``pool-worker`` dice *cómo* mina, no *con quién*.
    """
    teams = TeamsStore(redis_client)
    cache: dict[str, dict] = {}
    for status in statuses:
        team_id = teams.team_of_worker(status.worker_id)
        if not team_id:
            continue
        team = cache.get(team_id)
        if team is None:
            team = teams.get_team(team_id) or {}
            cache[team_id] = team
        if not team:
            continue
        status.team_id = team_id
        status.team_name = team.get("name")
        status.team_role = ("coordinator"
                            if team.get("coordinator_worker_id") == status.worker_id
                            else "member")
    return _annotate_node_identity(_annotate_owner(statuses, redis_client), redis_client)


def owner_of(redis_client, worker_id: str) -> Optional[str]:
    """Pubkey del ciudadano que registró el minero, o ``None`` si no es dinámico."""
    try:
        owner = redis_client.get(f"worker:owner:{worker_id}")
    except Exception:  # noqa: BLE001
        return None
    if isinstance(owner, bytes):
        owner = owner.decode("utf-8")
    return owner or None


def _annotate_owner(statuses: list[WorkerStatus], redis_client) -> list[WorkerStatus]:
    """``pubkey`` es la del **dueño**, también mientras el minero corre.

    El estado que reporta el worker trae la clave con la que *él* firma (la de
    nodo), y se devolvía tal cual. Como la UI decide "este minero es mío"
    comparando ``pubkey`` con la identidad del usuario, un minero encendido
    dejaba de ser de nadie: desaparecía "Dar de baja", Equipos decía "no tenés
    ningún minero registrado" y registrar otro daba 409 porque el backend sí
    sabía que era tuyo. La del nodo va en ``node_pubkey``, que es su lugar.
    """
    for status in statuses:
        owner = owner_of(redis_client, status.worker_id)
        if owner:
            status.pubkey = owner
    return statuses


def _annotate_node_identity(statuses: list[WorkerStatus], redis_client) -> list[WorkerStatus]:
    """Completa la pubkey **de nodo** de cada minero desde el vínculo de enrolamiento.

    Vacía significa "todavía no enroló": el pod puede estar arrancando, o ser un
    minero levantado a mano al que nunca se le pasó el token. Firma sus nonces
    igual, pero como identidad anónima — el bloque no queda imputado a su dueño.
    """
    for status in statuses:
        if status.node_pubkey:
            continue
        try:
            npk = redis_client.get(f"worker:node_pubkey:{status.worker_id}")
        except Exception:
            continue
        if npk:
            status.node_pubkey = npk.decode("utf-8") if isinstance(npk, bytes) else npk
    return statuses


@router.get("/status", response_model=list[WorkerStatus])
async def get_all_workers_status(redis: RedisReader = Depends(get_redis_reader)):
    """Get status of all registered workers."""
    statuses = []
    redis_client = redis.store.r

    # Dynamic worker discovery via Redis keys matching worker:status:*
    discovered_worker_ids = set()
    try:
        # scan_iter is non-blocking (non-blocking alternative to KEYS)
        for key in redis_client.scan_iter("worker:status:*"):
            key_str = key.decode("utf-8") if isinstance(key, bytes) else key
            parts = key_str.split("worker:status:")
            if len(parts) > 1:
                discovered_worker_ids.add(parts[1])
    except Exception:
        pass

    # Registered workers set from Redis
    registered_worker_ids = set()
    try:
        members = redis_client.smembers("registered_workers")
        for member in members:
            member_str = member.decode("utf-8") if isinstance(member, bytes) else member
            registered_worker_ids.add(member_str)
    except Exception:
        pass

    # Union of all worker IDs: preconfigured + discovered + registered
    all_worker_ids = set(ALL_REGISTERED_WORKER_IDS) | discovered_worker_ids | registered_worker_ids

    for worker_id in all_worker_ids:
        # 1. Try to read from Redis
        try:
            status_data = redis_client.get(f"worker:status:{worker_id}")
            if status_data:
                data = json.loads(status_data)
                # Map dynamic pubkey if stored in Redis but not reported in status
                if "pubkey" not in data or not data["pubkey"]:
                    pk = redis_client.get(f"worker:pubkey:{worker_id}")
                    if pk:
                        data["pubkey"] = pk.decode("utf-8") if isinstance(pk, bytes) else pk
                statuses.append(WorkerStatus(**data))
                continue
        except Exception:
            pass

        # 2. Fallback to HTTP (for local development/backwards compatibility)
        local_url_mapping = {
            "worker-1": "http://worker-1:9090",
            "worker-2": "http://worker-2:9090",
            "pool-coordinator-1": "http://worker-pool-coordinator:9090"
        }
        # Sólo los precargados del compose tienen servicio HTTP con ese nombre.
        base_url = local_url_mapping.get(worker_id) if worker_id in ALL_REGISTERED_WORKER_IDS else None
        if base_url:
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(f"{base_url}/status", timeout=1.0)
                    if response.status_code == 200:
                        data = response.json()
                        statuses.append(WorkerStatus(**data))
                        continue
            except Exception:
                pass

        # 3. If stopped but pre-configured or registered, show as stopped
        stored_pubkey = None
        try:
            pk = redis_client.get(f"worker:pubkey:{worker_id}")
            if pk:
                stored_pubkey = pk.decode("utf-8") if isinstance(pk, bytes) else pk
        except Exception:
            pass

        if worker_id in ALL_REGISTERED_WORKER_IDS or worker_id in registered_worker_ids:
            statuses.append(
                WorkerStatus(
                    worker_id=worker_id,
                    mode="unknown",
                    running=False,
                    pubkey=stored_pubkey
                )
            )

    return _annotate_team(statuses, redis_client)


@router.get("/{worker_id}/status", response_model=WorkerStatus)
async def get_worker_status(worker_id: str, redis: RedisReader = Depends(get_redis_reader)):
    """Get status of a specific worker by ID."""
    redis_client = redis.store.r

    # 1. Try to read from Redis
    try:
        status_data = redis_client.get(f"worker:status:{worker_id}")
        if status_data:
            data = json.loads(status_data)
            if "pubkey" not in data or not data["pubkey"]:
                pk = redis_client.get(f"worker:pubkey:{worker_id}")
                if pk:
                    data["pubkey"] = pk.decode("utf-8") if isinstance(pk, bytes) else pk
            return _annotate_team([WorkerStatus(**data)], redis_client)[0]
    except Exception:
        pass

    # 2. Fallback to HTTP
    local_url_mapping = {
        "worker-1": "http://worker-1:9090",
        "worker-2": "http://worker-2:9090",
        "pool-coordinator-1": "http://worker-pool-coordinator:9090"
    }
    # Sólo los precargados del compose tienen servicio HTTP con ese nombre.
    base_url = local_url_mapping.get(worker_id) if worker_id in ALL_REGISTERED_WORKER_IDS else None
    if base_url:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{base_url}/status", timeout=1.0)
                if response.status_code == 200:
                    data = response.json()
                    return WorkerStatus(**data)
        except Exception:
            pass

    # 3. Check if registered in Redis
    try:
        is_member = redis_client.sismember("registered_workers", worker_id)
        if is_member or worker_id in ALL_REGISTERED_WORKER_IDS:
            pk = redis_client.get(f"worker:pubkey:{worker_id}")
            stored_pubkey = pk.decode("utf-8") if isinstance(pk, bytes) else pk if pk else None
            return WorkerStatus(
                worker_id=worker_id,
                mode="unknown",
                running=False,
                pubkey=stored_pubkey
            )
    except Exception:
        pass

    raise HTTPException(status_code=404, detail="Worker not found")


@router.post("/{worker_id}/switch-mode", response_model=dict)
async def switch_worker_mode(
    worker_id: str,
    request: WorkerSwitchRequest,
    owner_id: str = Depends(get_owner_id),
    signature: Optional[str] = Depends(get_signature),
    timestamp: Optional[str] = Depends(get_signature_timestamp),
    redis: RedisReader = Depends(get_redis_reader),
    publisher: RabbitMQPublisher = Depends(get_rabbitmq_publisher)
):
    """Devuelve un minero a modo competitivo (``standalone``).

    Entrar al modo cooperativo **no** se hace por acá: se hace creando un equipo
    o uniéndose a uno (``/api/teams``). Antes este endpoint aceptaba
    ``pool-coordinator`` y ``pool-worker`` con una ``pool_url`` escrita a mano, y
    eso permitía dos cosas malas: apuntar a un coordinador inexistente, y sacar
    a un minero de un equipo sin que el equipo se enterara — la lista de
    miembros quedaba mintiendo. El modo y la membresía se mueven juntos.
    """
    authorize_worker_action(redis.store.r, worker_id, ADMIN_SWITCH_MODE,
                            owner_id, signature, timestamp)

    if request.target != "standalone":
        raise HTTPException(
            status_code=400,
            detail=("Para minar en cooperativo, creá un equipo o unite a uno "
                    "desde /api/teams. Este endpoint sólo devuelve un minero a "
                    "modo competitivo."),
        )

    redis_client = redis.store.r
    teams = TeamsStore(redis_client)

    # Salir del modo cooperativo implica salir del equipo. El coordinador es el
    # único caso que no se resuelve solo: si se fuera, sus miembros quedarían
    # pidiendo fragmentos a un HTTP que ya no reparte, así que exigimos la
    # disolución explícita en lugar de romper el equipo por un lado.
    membership = teams.detach_worker(worker_id)
    if membership and membership[1] == "coordinator":
        raise HTTPException(
            status_code=409,
            detail=(f"'{worker_id}' coordina un equipo. Disolvé el equipo para "
                    "devolverlo a modo competitivo."),
        )

    try:
        await dispatch_switch_mode(worker_id, "standalone", "", redis_client,
                                   publisher)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"Failed to publish switch-mode command to RabbitMQ: {e}",
        )
    finally:
        publisher.close()

    return {"ok": True, "mode": "standalone",
            "left_team": membership[0] if membership else None}


@router.get("/pool/{pool_id}/health", response_model=PoolHealth)
async def get_pool_health(pool_id: str, redis: RedisReader = Depends(get_redis_reader)):
    """Get health status of a specific pool coordinator."""
    redis_client = redis.store.r

    # 1. Try to read from Redis
    try:
        health_data = redis_client.get(f"pool:health:{pool_id}")
        if health_data:
            data = json.loads(health_data)
            return PoolHealth(**data)
    except Exception:
        pass

    # 2. Fallback to HTTP
    service_name = POOL_COORDINATOR_MAPPING.get(pool_id, pool_id)
    pool_url = f"http://{service_name}:9001"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{pool_url}/health", timeout=2.0)
            if response.status_code == 200:
                return PoolHealth(**response.json())
    except Exception:
        pass
    raise HTTPException(status_code=404, detail="Pool not found")


@router.post("/pool/{pool_id}/policy")
async def set_pool_policy(
    pool_id: str,
    policy: PoolPolicy,
    owner_id: str = Depends(get_owner_id),
    signature: Optional[str] = Depends(get_signature),
    timestamp: Optional[str] = Depends(get_signature_timestamp),
    redis: RedisReader = Depends(get_redis_reader)
):
    """Set voting policy for a specific pool coordinator.

    El rechazo puntual (por ``action`` o ``law_id``) y la **agenda temática** del
    equipo (``categories``, AGENT.md 3.10) son dos filtros distintos que viven en
    la misma clave de Redis. Si la petición no trae ``categories``, se conserva la
    que ya estaba: la agenda la escribe el flujo de equipos y no tiene por qué
    perderse porque alguien tocó la política de acciones desde la otra pantalla.
    """
    authorize_worker_action(redis.store.r, pool_id, ADMIN_POOL_POLICY,
                            owner_id, signature, timestamp)

    # 1. Write the policy to Redis so the remote coordinator can read it periodically
    redis_client = redis.store.r
    payload = policy.model_dump()
    try:
        if payload.get("categories") is None:
            payload["categories"] = _stored_categories(redis_client, pool_id)
        redis_client.set(f"pool:policy:{pool_id}", json.dumps(payload))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save policy to Redis: {e}")

    # 2. Fallback to HTTP for local dev
    service_name = POOL_COORDINATOR_MAPPING.get(pool_id, pool_id)
    pool_url = f"http://{service_name}:9001"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{pool_url}/pool/policy",
                json=payload,
                timeout=2.0,
            )
            if response.status_code == 200:
                return response.json()
    except Exception:
        pass

    return {"ok": True, "policy": payload}


def _stored_categories(redis_client, pool_id: str) -> list[str]:
    """Agenda temática ya escrita para este pool, o vacía si no hay ninguna."""
    try:
        raw = redis_client.get(f"pool:policy:{pool_id}")
        if not raw:
            return []
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        previous = json.loads(raw)
        if isinstance(previous, dict):
            return list(previous.get("categories") or [])
    except Exception:  # noqa: BLE001
        logger.debug("política previa de %s ilegible", pool_id, exc_info=True)
    return []


def _verify_timestamp_freshness(timestamp_str: str) -> None:
    try:
        ts = datetime.fromisoformat(timestamp_str).timestamp()
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid timestamp format")
    
    now_ts = datetime.now(timezone.utc).timestamp()
    max_age = float(os.getenv("PROPOSAL_MAX_AGE_SECONDS", "300"))
    if abs(now_ts - ts) > max_age:
        raise HTTPException(status_code=400, detail="Timestamp expired or too far in the future")


def worker_of_owner(redis_client, pubkey: str) -> Optional[str]:
    """El minero que ya registró esta identidad, o ``None`` si no registró ninguno.

    Se resuelve recorriendo ``registered_workers`` en vez de con un índice
    inverso a propósito: el conjunto ya es la fuente de verdad del alta y la baja
    lo limpia, así que esto **no puede quedar desincronizado** ni necesita migrar
    el estado que ya existe en Redis. Las altas son raras y los mineros
    dinámicos, pocos; el costo lineal no se nota.

    Un id que quedó en el conjunto sin su ``worker:owner:*`` se ignora en vez de
    contarse: es un registro a medias, y bloquear a alguien por un resto de
    estado sería peor que dejarlo registrar de nuevo.
    """
    try:
        registered = redis_client.smembers("registered_workers") or []
    except Exception:  # noqa: BLE001
        logger.warning("no se pudo leer registered_workers", exc_info=True)
        return None
    for worker_id in registered:
        if isinstance(worker_id, bytes):
            worker_id = worker_id.decode("utf-8")
        owner = redis_client.get(f"worker:owner:{worker_id}")
        if isinstance(owner, bytes):
            owner = owner.decode("utf-8")
        if owner and owner == pubkey:
            return worker_id
    return None


def persist_worker_registration(request: RegisterWorkerRequest, redis_client) -> dict:
    """Alta de un minero: verifica la firma del dueño y lo despliega.

    Está separada del endpoint porque el alta de equipos la reusa: crear un
    equipo con un minero nuevo es exactamente este mismo procedimiento seguido
    de una promoción a coordinador, y duplicarlo habría dejado dos caminos de
    alta con reglas de verificación que se iban a separar con el tiempo.
    """
    worker_id = request.worker_id.strip()
    pubkey = request.pubkey.strip()
    signature = request.signature.strip()
    timestamp = request.timestamp.strip()

    if not worker_id:
        raise HTTPException(status_code=400, detail="Worker ID cannot be empty")
    if not pubkey:
        raise HTTPException(status_code=400, detail="Public Key cannot be empty")

    # 1. Collision Protection: check against default and other owned workers
    if worker_id in ALL_REGISTERED_WORKER_IDS:
        raise HTTPException(status_code=409, detail="Worker ID is reserved for a default worker")

    try:
        existing_owner = redis_client.get(f"worker:owner:{worker_id}")
        if existing_owner:
            existing_owner_str = existing_owner.decode("utf-8") if isinstance(existing_owner, bytes) else existing_owner
            if existing_owner_str != pubkey:
                raise HTTPException(status_code=409, detail="Worker ID is already registered by another owner")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database check failed: {e}")

    # 2. Replay Protection: verify timestamp freshness
    _verify_timestamp_freshness(timestamp)

    # 3. Ownership Authentication: verify signature of `${worker_id}|register|${timestamp}`
    reject_if_revoked(redis_client, pubkey)
    msg = f"{worker_id}|register|{timestamp}".encode()
    if not verify(pubkey, msg, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    # 4. Un minero por identidad.
    #
    # No es una limitación técnica sino la misma regla que ya rige para fundar
    # equipos (AGENT.md 3.9): el sistema documenta la concentración de poder como
    # su debilidad estructural, y dejar que una sola clave acumule mineros la
    # agrava gratis. Con Sybil sigue siendo evadible —generar otra identidad
    # cuesta nada, AGENT.md 9— pero acá no se pretende cerrar ese agujero, sólo
    # no ensancharlo.
    #
    # Re-registrar el MISMO id es válido y no cuenta como un minero nuevo: es el
    # camino para recrear su despliegue o reemitir su token de enrolamiento.
    existing = worker_of_owner(redis_client, pubkey)
    if existing and existing != worker_id:
        raise HTTPException(
            status_code=409,
            detail=(f"Ya tenés registrado el minero '{existing}'. Cada identidad "
                    "puede registrar uno solo: dalo de baja si querés usar otro id."),
        )

    # 5. Save to Redis
    #
    # `worker:pubkey` es la del DUEÑO (quién responde por el minero), no la del
    # nodo: la identidad con la que el minero firma nace dentro de su proceso y
    # llega después, por /enroll.
    try:
        redis_client.sadd("registered_workers", worker_id)
        redis_client.set(f"worker:owner:{worker_id}", pubkey)
        redis_client.set(f"worker:pubkey:{worker_id}", pubkey)
        # Un re-registro invalida la identidad de nodo anterior: el pod viejo se
        # reemplaza y su clave se va con él, así que dejar el vínculo colgado
        # sólo serviría para imputarle al dueño un nodo que ya no controla.
        _clear_node_binding(redis_client, worker_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save registration: {e}")

    # 6. Despliegue dinámico en Kubernetes, si el alta lo pidió.
    #
    # `deployed` dice si además de anotar el minero se levantó un proceso para
    # él, y `deployed_on` dónde: un pod en Kubernetes o un contenedor en el
    # Docker local. Sin ninguno de los dos el alta es sólo metadata: el minero
    # queda registrado y sin correr, y la UI tiene que decirlo — antes el
    # frontend anunciaba "desplegado" aunque no hubiera desplegado nada, así que
    # el usuario se quedaba esperando un contenedor que nadie iba a crear.
    deployed = False
    deployed_on = None
    deploy_error = None
    enrollment_token = ""
    if request.deploy and K8S_ENABLED:
        enrollment_token = _issue_enrollment_token(redis_client, worker_id)
        try:
            _spawn_k8s_worker(worker_id, enrollment_token)
        except Exception:
            # Sin pod, el token no le sirve a nadie más que a un atacante.
            redis_client.delete(f"worker:enroll:{worker_id}")
            raise
        deployed = True
        deployed_on = "kubernetes"
    elif request.deploy and docker_spawner.enabled():
        # El compose local monta el socket de Docker: el API levanta el
        # contenedor del minero él mismo, en vez de pedirle al usuario que corra
        # `./run.sh worker` a mano. Si falla no se tira el alta —el minero ya
        # quedó registrado—; se cae al camino manual y se dice por qué.
        enrollment_token = _issue_enrollment_token(redis_client, worker_id)
        try:
            docker_spawner.spawn_worker(worker_id, enrollment_token)
            deployed = True
            deployed_on = "docker"
        except Exception as exc:  # noqa: BLE001
            logger.warning("no se pudo levantar el contenedor de %s: %s", worker_id, exc)
            redis_client.delete(f"worker:enroll:{worker_id}")
            deploy_error = str(exc)

    # El alta sin despliegue (sin socket de Docker, o si falló) también recibe token: es el que
    # el usuario le pasa a su propio proceso vía WORKER_ENROLL_TOKEN. En ese caso
    # sí viaja en la respuesta, porque no hay Secret donde dejárselo — pero es un
    # token de slot con TTL, no una identidad.
    if not deployed:
        enrollment_token = _issue_enrollment_token(redis_client, worker_id)

    return {"ok": True, "worker_id": worker_id, "pubkey": pubkey,
            "deployed": deployed,
            **({"deployed_on": deployed_on} if deployed else {}),
            **({"deploy_error": deploy_error} if deploy_error else {}),
            **({} if deployed else {"enrollment_token": enrollment_token})}


def reconcile_docker_workers(redis_client) -> list[str]:
    """Relanza los mineros registrados que no tienen contenedor. Devuelve cuáles.

    El registro vive en Redis, que tiene volumen y sobrevive a `./run.sh stop`;
    los contenedores no (el stop los borra para poder bajar la red). Sin esto,
    al volver a levantar la demo todos los mineros y equipos quedaban "sin
    arrancar" para siempre, y la única salida era darlos de baja y registrarlos
    de nuevo. Registrado = debe estar corriendo, igual que un Deployment de k8s.

    Se saltea un minero con token de enrolamiento vigente: significa que un alta
    lo acaba de levantar (o lo está levantando), y emitirle otro token acá
    invalidaría el del contenedor recién creado.
    """
    if not docker_spawner.enabled():
        return []
    try:
        registered = redis_client.smembers("registered_workers") or []
        existing = docker_spawner.existing_containers()
    except Exception as exc:  # noqa: BLE001
        logger.debug("reconciliación de contenedores salteada: %s", exc)
        return []

    spawned = []
    for worker_id in registered:
        if isinstance(worker_id, bytes):
            worker_id = worker_id.decode("utf-8")
        if worker_id in ALL_REGISTERED_WORKER_IDS:
            continue
        if docker_spawner.container_name(worker_id) in existing:
            continue
        if redis_client.get(f"worker:enroll:{worker_id}"):
            continue
        # Se re-chequea justo antes: una baja pudo sacarlo del conjunto mientras
        # tanto, y levantarlo igual dejaría un contenedor huérfano.
        if not redis_client.sismember("registered_workers", worker_id):
            continue
        token = _issue_enrollment_token(redis_client, worker_id)
        try:
            docker_spawner.spawn_worker(worker_id, token)
            spawned.append(worker_id)
        except Exception as exc:  # noqa: BLE001
            redis_client.delete(f"worker:enroll:{worker_id}")
            # Lo normal al arrancar: worker-1 (la plantilla) todavía no existe.
            logger.info("no se pudo relanzar %s todavía: %s", worker_id, exc)
    if spawned:
        logger.info("mineros relanzados en Docker: %s", ", ".join(spawned))
    return spawned


def _clear_node_binding(redis_client, worker_id: str) -> None:
    """Borra **todas** las identidades de nodo de un minero y su índice inverso.

    Un minero puede tener varias: una por pod que se enroló (las réplicas de un
    coordinador, o pods que reemplazaron a otros). Se guardan en el conjunto
    ``worker:node_pubkeys:<id>``; ``worker:node_pubkey:<id>`` es sólo la última,
    que es la que muestra la UI.
    """
    nodos = set(redis_client.smembers(f"worker:node_pubkeys:{worker_id}") or ())
    ultimo = redis_client.get(f"worker:node_pubkey:{worker_id}")
    if ultimo:
        nodos.add(ultimo)
    for nodo in nodos:
        if isinstance(nodo, bytes):
            nodo = nodo.decode("utf-8")
        redis_client.delete(f"node:owner:{nodo}")
    redis_client.delete(f"worker:node_pubkey:{worker_id}",
                        f"worker:node_pubkeys:{worker_id}")


@router.post("/enroll", response_model=dict)
async def enroll_node(
    request: EnrollNodeRequest,
    redis: RedisReader = Depends(get_redis_reader)
):
    """El minero publica la identidad que generó él mismo y la vincula a su dueño.

    Es la contraparte del alta: el ciudadano registra el minero firmando con su
    clave (y esa clave nunca sale de su navegador), y el minero registra su propia
    pubkey presentando el token de un solo uso que recibió al desplegarse. El
    vínculo resultante (`node:owner:*`) es lo que le permite al NCT seguir
    aplicando la regla 3.4 sin que las dos identidades sean la misma clave.
    """
    redis_client = redis.store.r
    worker_id = request.worker_id.strip()
    node_pubkey = request.node_pubkey.strip()

    if not worker_id or not node_pubkey:
        raise HTTPException(status_code=400, detail="worker_id y node_pubkey son obligatorios")

    owner = redis_client.get(f"worker:owner:{worker_id}")
    if not owner:
        raise HTTPException(status_code=404, detail="Worker registration not found")
    if isinstance(owner, bytes):
        owner = owner.decode("utf-8")

    # La pubkey tiene que ser una P-256 cargable: si no, el NCT nunca podría
    # verificar una firma suya y el vínculo sería basura permanente en el índice.
    #
    # Se valida ANTES de consumir el token: al revés, un request malformado
    # quemaba el token y dejaba al minero sin poder enrolarse hasta que su dueño
    # lo re-registrara. Un pedido que no puede prosperar no debe gastar nada.
    if not _is_valid_pubkey(node_pubkey):
        raise HTTPException(status_code=400, detail="node_pubkey no es una clave EC P-256 válida")

    # Un nodo que ya está vinculado a este minero no gasta token. Es el caso del
    # contenedor que se reinicia dentro del mismo pod: la clave sobrevive en el
    # volumen del pod y el proceso nuevo se vuelve a enrolar con ella. Si
    # consumiera un token, se llevaría el de la réplica y la dejaría sin dueño.
    # Presentar una pubkey ya vinculada no le da nada a nadie: el vínculo ya
    # existía.
    if redis_client.sismember(f"worker:node_pubkeys:{worker_id}", node_pubkey):
        return {"ok": True, "worker_id": worker_id, "owner_pubkey": owner}

    if not _consume_enrollment_token(redis_client, worker_id, request.enrollment_token):
        # El pod que reemplaza a otro llega acá con tokens ya gastados. Se le
        # deja uno nuevo en su Secret y él lo levanta en su próximo reintento
        # (ver `reissue_enrollment_token`).
        repuesto = reissue_enrollment_token(redis_client, worker_id)
        raise HTTPException(
            status_code=401,
            detail="Enrollment token inválido o ya usado"
                   + ("; se dejó uno nuevo en el Secret del minero" if repuesto else ""))

    # El vínculo se **agrega**, no reemplaza al anterior. Con réplicas del
    # coordinador hay dos pods vivos del mismo minero, cada uno con su clave:
    # reemplazar dejaría al que enroló primero —normalmente el líder, el que
    # firma los nonces— sin dueño, y sus bloques dejarían de imputarse. Las
    # claves de pods que ya no existen quedan en el conjunto, pero no sirven
    # para nada: nacieron y murieron en memoria de un pod. Se limpian todas al
    # re-registrar o dar de baja el minero (`_clear_node_binding`).
    try:
        redis_client.sadd(f"worker:node_pubkeys:{worker_id}", node_pubkey)
        redis_client.set(f"worker:node_pubkey:{worker_id}", node_pubkey)
        redis_client.set(f"node:owner:{node_pubkey}", owner)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to bind node identity: {e}")

    logger.info("minero %s enroló identidad de nodo %s… (dueño %s…)",
                worker_id, node_pubkey[:12], owner[:12])
    return {"ok": True, "worker_id": worker_id, "owner_pubkey": owner}


@router.post("/register", response_model=dict)
async def register_worker(
    request: RegisterWorkerRequest,
    redis: RedisReader = Depends(get_redis_reader)
):
    """Register a new worker ID, mapping it to the citizen owner pubkey."""
    return persist_worker_registration(request, redis.store.r)


@router.delete("/{worker_id}", response_model=dict)
async def unregister_worker(
    worker_id: str,
    x_signature: str = Header(None, alias="X-Signature"),
    x_timestamp: str = Header(None, alias="X-Timestamp"),
    redis: RedisReader = Depends(get_redis_reader)
):
    """Unregister a worker ID (only if owned by current caller)."""
    if not x_signature or not x_timestamp:
        raise HTTPException(status_code=400, detail="Missing verification headers (X-Signature / X-Timestamp)")

    # 1. Collision check: reject deleting hardcoded default workers
    if worker_id in ALL_REGISTERED_WORKER_IDS:
        raise HTTPException(status_code=403, detail="Cannot delete preconfigured default workers")

    redis_client = redis.store.r

    # 2. Fetch owner public key. If not found, 404
    owner_pubkey_bytes = redis_client.get(f"worker:owner:{worker_id}")
    if not owner_pubkey_bytes:
        raise HTTPException(status_code=404, detail="Worker registration not found")
    owner_pubkey = owner_pubkey_bytes.decode("utf-8") if isinstance(owner_pubkey_bytes, bytes) else owner_pubkey_bytes

    # 3. Replay Protection: verify timestamp freshness
    _verify_timestamp_freshness(x_timestamp)

    # 4. Verify signature of `${worker_id}|delete|${timestamp}`
    reject_if_revoked(redis_client, owner_pubkey)
    msg = f"{worker_id}|delete|{x_timestamp}".encode()
    if not verify(owner_pubkey, msg, x_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    # 5. Delete metadata
    try:
        redis_client.srem("registered_workers", worker_id)
        redis_client.delete(f"worker:owner:{worker_id}")
        redis_client.delete(f"worker:pubkey:{worker_id}")
        redis_client.delete(f"worker:status:{worker_id}")
        redis_client.delete(f"worker:enroll:{worker_id}",
                            ENROLL_REPLICA_KEY.format(worker_id=worker_id))
        _clear_node_binding(redis_client, worker_id)
        # Sin esto, volver a registrar un minero con el mismo id lo haría
        # arrancar en el equipo del que fue dado de baja.
        clear_desired_mode(redis_client, worker_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to unregister worker: {e}")

    # 6. Delete dynamic Kubernetes resources (o el contenedor del compose local)
    _delete_k8s_worker(worker_id)
    docker_spawner.delete_worker(worker_id)

    return {"ok": True}
