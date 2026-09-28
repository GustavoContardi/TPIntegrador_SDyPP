"""Fixtures compartidas por todos los tests de Pilar 2."""

import sys

import fakeredis
import pytest

from common.messaging import InMemoryBus
from common.storage import VoxChainStore


@pytest.fixture
def store():
    """VoxChainStore respaldado por un Redis en memoria (fakeredis)."""
    return VoxChainStore(fakeredis.FakeRedis(decode_responses=True))


@pytest.fixture
def bus():
    """Bus de mensajería en memoria (despacho síncrono)."""
    return InMemoryBus()


@pytest.fixture(autouse=True)
def _sin_kubernetes_real(monkeypatch):
    """Ningún test habla con un clúster de verdad.

    El API habilita Kubernetes al importarse si encuentra un kubeconfig, y en
    una máquina de desarrollo suele haber uno (``~/.kube/config``). Sin esto,
    fundar un equipo en un test intentaría escalar un Deployment en ese
    clúster. Los tests que ejercitan el camino de Kubernetes lo vuelven a
    habilitar con un cliente falso (``tests/test_k8s_service.py``).
    """
    workers = sys.modules.get("voxchain_api.routers.workers")
    if workers is not None:
        monkeypatch.setattr(workers, "K8S_ENABLED", False)
