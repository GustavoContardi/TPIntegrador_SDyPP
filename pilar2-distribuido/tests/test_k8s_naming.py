"""Nombres de objetos de Kubernetes derivados del ``worker_id`` (Register Worker).

El ``worker_id`` lo elige el usuario en el formulario y se usaba crudo como
nombre del Secret y del Deployment. Los nombres de recursos son subdominios
RFC 1123 —sólo ``[a-z0-9-]``, empezando y terminando en alfanumérico— así que
un id con mayúsculas hacía que el API server devolviera un 422 y el alta
fallara a la cara del usuario ("GustavoContardi" fue el caso que lo destapó).
"""

import re

import pytest

from voxchain_api.routers.workers import _k8s_slug

# El mismo patrón que aplica el API server a metadata.name.
RFC1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

IDS = [
    "GustavoContardi",       # el que rompía: mayúsculas
    "Gustavo Contardi",      # espacios
    "gustavo.contardi",      # punto (válido en subdominio, no en estos nombres)
    "mi_worker_2026",        # guión bajo
    "UPPER",
    "-raro-",                # no puede empezar ni terminar con guión
    "###",                   # no queda nada tras normalizar
    "a" * 60,                # más largo que el límite de un nombre
    "worker-1",              # ya válido
    "worker-pool-miner-1",   # ya válido
]


@pytest.mark.parametrize("worker_id", IDS)
def test_el_slug_es_un_nombre_valido(worker_id):
    slug = _k8s_slug(worker_id)
    assert RFC1123.match(slug), f"{worker_id!r} -> {slug!r} no es RFC 1123"
    # Los dos nombres que se construyen a partir del slug.
    assert RFC1123.match(f"secret-{slug}")
    assert RFC1123.match(f"worker-dep-{slug}")


@pytest.mark.parametrize("worker_id", IDS)
def test_el_nombre_del_deployment_entra_en_63_caracteres(worker_id):
    # Los pods heredan el nombre del Deployment más los sufijos del ReplicaSet,
    # así que conviene no acercarse al límite.
    assert len(f"worker-dep-{_k8s_slug(worker_id)}") <= 63


@pytest.mark.parametrize("worker_id", ["worker-1", "worker-pool-miner-1", "abc123"])
def test_un_id_ya_valido_no_se_toca(worker_id):
    # Si no, un redespliegue renombraría los objetos de los workers existentes
    # y el borrado dejaría de encontrarlos.
    assert _k8s_slug(worker_id) == worker_id


def test_ids_distintos_no_colapsan_en_el_mismo_nombre():
    # Sin el sufijo de hash, estos cuatro caían todos en "gustavo-contardi" y el
    # segundo registro fallaba con AlreadyExists en vez de crear su worker.
    ids = ["GustavoContardi", "gustavocontardi", "gustavo.contardi", "Gustavo Contardi"]
    slugs = [_k8s_slug(i) for i in ids]
    assert len(set(slugs)) == len(ids)


def test_el_slug_es_estable():
    # El borrado recalcula el nombre a partir del worker_id: si no fuera
    # determinístico, unregister no encontraría lo que creó register.
    assert _k8s_slug("GustavoContardi") == _k8s_slug("GustavoContardi")
