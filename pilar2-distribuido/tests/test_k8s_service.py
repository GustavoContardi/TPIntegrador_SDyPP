"""Service estable por minero desplegado en Kubernetes.

Los miembros de un equipo le piden trabajo a su coordinador por HTTP. Antes lo
hacían contra la IP del pod, que cambia cada vez que el pod se reemplaza, y la
URL de los miembros sólo se corregía cuando alguien consultaba los equipos en
el API: sin nadie mirando la UI, el equipo quedaba pidiéndole trabajo a una
dirección muerta. Ahora el alta crea un Service por minero y el pod anuncia su
nombre, que sobrevive a cualquier reemplazo.
"""

from __future__ import annotations

import re

import pytest

pytest.importorskip("kubernetes")

from voxchain_api.routers import workers  # noqa: E402

# Un Service es una etiqueta RFC 1035: como RFC 1123 pero empezando con letra.
RFC1035 = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")

IDS = ["GustavoContardi", "gustavo.contardi", "-raro-", "###", "a" * 60,
       "worker-1", "2026-minero"]


class ErrorDeApi(Exception):
    def __init__(self, status):
        super().__init__(f"status {status}")
        self.status = status


class CoreFalso:
    def __init__(self, error_service=None):
        self.error_service = error_service
        self.services, self.secrets = [], []
        self.borrados = []

    def create_namespaced_service(self, namespace, body):
        if self.error_service:
            raise ErrorDeApi(self.error_service)
        self.services.append(body)

    def create_namespaced_secret(self, namespace, body):
        self.secrets.append(body)

    def delete_namespaced_secret(self, name, namespace):
        self.borrados.append(("secret", name))

    def delete_namespaced_service(self, name, namespace):
        self.borrados.append(("service", name))

    def patch_namespaced_secret(self, name, namespace, body):
        self.parches = getattr(self, "parches", []) + [(name, body)]


class AppsFalso:
    def __init__(self, existe=True):
        self.deployments = []
        self.borrados = []
        self.existe = existe
        self.escalas = []

    def read_namespaced_deployment(self, name, namespace):
        if not self.existe:
            raise ErrorDeApi(404)
        return object()

    def patch_namespaced_deployment_scale(self, name, namespace, body):
        self.escalas.append((name, body["spec"]["replicas"]))

    def create_namespaced_deployment(self, namespace, body):
        self.deployments.append(body)

    def delete_namespaced_deployment(self, name, namespace):
        self.borrados.append(("deployment", name))


@pytest.fixture
def cluster(monkeypatch):
    """Devuelve una función que arma el clúster falso con el error pedido."""
    def armar(error_service=None, existe=True):
        core, apps = CoreFalso(error_service), AppsFalso(existe)
        monkeypatch.setattr(workers, "K8S_ENABLED", True)
        monkeypatch.setattr(workers, "NAMESPACE", "g-git-push-cv")
        monkeypatch.setattr(workers.client, "CoreV1Api", lambda: core)
        monkeypatch.setattr(workers.client, "AppsV1Api", lambda: apps)
        return core, apps
    return armar


def _env(deployment) -> dict:
    contenedor = deployment.spec.template.spec.containers[0]
    return {e.name: e.value for e in contenedor.env}


@pytest.mark.parametrize("worker_id", IDS)
def test_el_nombre_del_service_es_valido(worker_id):
    nombre = workers._k8s_service_name(workers._k8s_slug(worker_id))
    assert RFC1035.match(nombre), f"{worker_id!r} -> {nombre!r}"
    assert len(nombre) <= 63


def test_el_alta_crea_el_service_y_el_pod_anuncia_su_nombre(cluster):
    core, apps = cluster()
    workers._spawn_k8s_worker("GustavoContardi", "token")

    [service] = core.services
    [deployment] = apps.deployments
    slug = workers._k8s_slug("GustavoContardi")
    # El Service apunta a los pods de ESTE minero, por el puerto del pool.
    assert service.spec.selector == deployment.spec.template.metadata.labels
    assert service.spec.ports[0].port == workers.POOL_PORT
    # Y el pod anuncia el nombre del Service, no su IP.
    assert _env(deployment)["WORKER_ADDRESS"] == (
        f"http://worker-svc-{slug}.g-git-push-cv.svc:{workers.POOL_PORT}")


def test_la_readiness_mira_ready_y_la_liveness_no(cluster):
    _core, apps = cluster()
    workers._spawn_k8s_worker("worker-1", "token")
    contenedor = apps.deployments[0].spec.template.spec.containers[0]
    assert contenedor.readiness_probe.http_get.path == "/ready"
    # Una liveness en /ready reiniciaría en bucle al coordinador que espera el
    # lease de su pool: sano, pero sin tráfico.
    assert (contenedor.liveness_probe is None
            or contenedor.liveness_probe.http_get.path != "/ready")


def test_sin_permiso_para_services_cae_a_la_ip_del_pod(cluster):
    # La cuenta del clúster externo no es nuestra: puede no poder crear
    # Services. El alta no se cae por eso.
    core, apps = cluster(error_service=403)
    workers._spawn_k8s_worker("worker-1", "token")
    assert core.services == []
    assert len(apps.deployments) == 1
    assert _env(apps.deployments[0])["WORKER_ADDRESS"] == (
        f"http://$(MY_POD_IP):{workers.POOL_PORT}")


def test_un_service_que_ya_existia_se_reutiliza(cluster):
    # 409: quedó de un alta anterior del mismo id. Sirve igual.
    _core, apps = cluster(error_service=409)
    workers._spawn_k8s_worker("worker-1", "token")
    assert "worker-svc-worker-1" in _env(apps.deployments[0])["WORKER_ADDRESS"]


def test_la_baja_borra_tambien_el_service(cluster):
    core, apps = cluster()
    workers._delete_k8s_worker("worker-1")
    assert ("service", "worker-svc-worker-1") in core.borrados
    assert ("deployment", "worker-dep-worker-1") in apps.borrados


# -- réplica en espera del coordinador de un equipo ---------------------------


def test_los_tokens_llegan_por_volumen_y_no_por_entorno(cluster):
    # Una variable de entorno se fija al arrancar el contenedor; el volumen,
    # Kubernetes lo actualiza en el pod vivo. Sin eso, un pod que reemplaza a
    # otro no podría recibir el token que le repone el API.
    _core, apps = cluster()
    workers._spawn_k8s_worker("worker-1", "token")
    pod = apps.deployments[0].spec.template.spec
    contenedor = pod.containers[0]
    env = {e.name: e for e in contenedor.env}
    assert "WORKER_ENROLL_TOKEN" not in env
    assert env["WORKER_ENROLL_TOKEN_DIR"].value == workers.ENROLL_TOKEN_DIR

    [volumen] = [v for v in pod.volumes if v.name == "enroll"]
    assert volumen.secret.secret_name == "secret-worker-1"
    # Sin `items`: el slot de la réplica sólo existe mientras el minero
    # coordina, y un archivo que falta no puede impedir que el pod arranque.
    assert volumen.secret.items is None
    [montaje] = [m for m in contenedor.volume_mounts if m.name == "enroll"]
    assert montaje.mount_path == workers.ENROLL_TOKEN_DIR and montaje.read_only


def test_las_replicas_prefieren_nodos_distintos(cluster):
    _core, apps = cluster()
    workers._spawn_k8s_worker("worker-1", "token")
    afinidad = apps.deployments[0].spec.template.spec.affinity.pod_anti_affinity
    # Preferred, no required: en un clúster de un nodo la réplica tiene que
    # poder programarse igual.
    assert afinidad.required_during_scheduling_ignored_during_execution is None
    [termino] = afinidad.preferred_during_scheduling_ignored_during_execution
    assert termino.pod_affinity_term.topology_key == "kubernetes.io/hostname"


def test_escalar_deja_el_token_de_la_replica_y_sube_a_dos(cluster):
    import fakeredis

    r = fakeredis.FakeRedis(decode_responses=True)
    core, apps = cluster()
    assert workers.scale_k8s_coordinator("worker-1", 2, r) is True

    [(secreto, cuerpo)] = core.parches
    assert secreto == "secret-worker-1"
    token = cuerpo["stringData"]["enrollment-token-replica"]
    # En Redis queda el hash en el slot de la réplica, listo para consumirse.
    assert workers._consume_enrollment_token(r, "worker-1", token)
    assert apps.escalas == [("worker-dep-worker-1", 2)]


def test_volver_a_un_pod_no_emite_token(cluster):
    import fakeredis

    r = fakeredis.FakeRedis(decode_responses=True)
    core, apps = cluster()
    workers.scale_k8s_coordinator("worker-1", 1, r)
    assert getattr(core, "parches", []) == []
    assert apps.escalas == [("worker-dep-worker-1", 1)]


def test_un_minero_sin_deployment_nuestro_no_se_toca(cluster):
    # Lo levantó el usuario a mano: emitir un token pisaría el que todavía no
    # usó para enrolar su proceso.
    import fakeredis

    r = fakeredis.FakeRedis(decode_responses=True)
    r.set("worker:enroll:worker-1", "digest-del-alta")
    core, apps = cluster(existe=False)
    assert workers.scale_k8s_coordinator("worker-1", 2, r) is False
    assert apps.escalas == []
    assert not r.exists("worker:enroll:worker-1:replica")
    assert r.get("worker:enroll:worker-1") == "digest-del-alta"


# -- reposición del token para un pod que reemplaza a otro --------------------


def _redis():
    import fakeredis
    return fakeredis.FakeRedis(decode_responses=True)


def test_sin_token_vigente_se_repone_uno_en_el_secret(cluster):
    r = _redis()
    core, _apps = cluster()
    assert workers.reissue_enrollment_token(r, "worker-1") is True

    [(secreto, cuerpo)] = core.parches
    assert secreto == "secret-worker-1"
    token = cuerpo["stringData"]["enrollment-token"]
    # El token va al Secret, no a quien preguntó; y en Redis queda su hash.
    assert workers._consume_enrollment_token(r, "worker-1", token)


@pytest.mark.parametrize("slot", ["worker:enroll:worker-1",
                                  "worker:enroll:worker-1:replica"])
def test_con_un_token_vigente_no_se_emite_otro(cluster, slot):
    # Puede ser el que el pod todavía no terminó de recibir en su volumen.
    # También acota el abuso: un token cada ENROLL_TOKEN_TTL por minero.
    r = _redis()
    r.set(slot, "digest-pendiente")
    core, _apps = cluster()
    assert workers.reissue_enrollment_token(r, "worker-1") is False
    assert getattr(core, "parches", []) == []
    assert r.get(slot) == "digest-pendiente"


def test_a_un_minero_sin_deployment_nuestro_no_se_le_repone(cluster):
    # Levantado a mano: no hay Secret donde dejarle nada.
    r = _redis()
    core, _apps = cluster(existe=False)
    assert workers.reissue_enrollment_token(r, "worker-1") is False
    assert not r.exists("worker:enroll:worker-1")


def test_si_no_se_puede_escribir_el_secret_no_queda_un_token_huerfano(cluster):
    r = _redis()
    core, _apps = cluster()

    def falla(**_kw):
        raise ErrorDeApi(403)

    core.patch_namespaced_secret = falla
    assert workers.reissue_enrollment_token(r, "worker-1") is False
    # Si quedara en Redis bloquearía la próxima reposición durante todo su TTL.
    assert not r.exists("worker:enroll:worker-1")


def test_el_alta_manda_los_logs_a_loki_si_el_pipeline_lo_configuro(cluster):
    """Mismas variables que los manifests de gpu-cluster/, y opcionales: si 04
    no cargó la URL o la contraseña, el pod tiene que arrancar igual."""
    _core, apps = cluster()
    workers._spawn_k8s_worker("worker-1", "token")

    env = {e.name: e for e in apps.deployments[0].spec.template.spec.containers[0].env}
    url = env["LOKI_PUSH_URL"].value_from.config_map_key_ref
    assert (url.name, url.key, url.optional) == ("worker-config", "loki-push-url", True)
    clave = env["LOKI_PUSH_PASSWORD"].value_from.secret_key_ref
    assert (clave.name, clave.key, clave.optional) == ("loki-push-credentials", "password", True)
    assert env["LOKI_PUSH_USER"].value == "voxchain-logs"
    assert env["LOKI_CLUSTER"].value == "k3s"
