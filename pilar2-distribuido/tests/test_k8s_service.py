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


class AppsFalso:
    def __init__(self):
        self.deployments = []
        self.borrados = []

    def create_namespaced_deployment(self, namespace, body):
        self.deployments.append(body)

    def delete_namespaced_deployment(self, name, namespace):
        self.borrados.append(("deployment", name))


@pytest.fixture
def cluster(monkeypatch):
    """Devuelve una función que arma el clúster falso con el error pedido."""
    def armar(error_service=None):
        core, apps = CoreFalso(error_service), AppsFalso()
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
