"""Pydantic response models for voxchain-api."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from common.blockchain import DEFAULT_CATEGORY


class Law(BaseModel):
    law_id: str
    author_pubkey: str
    text_hash: str
    text_ref: Optional[str] = None
    text_compressed: Optional[str] = None
    text_original_len: Optional[int] = None
    status: str
    action: str
    # Área de gobierno (AGENT.md 3.10). Las leyes anteriores a las categorías no
    # la tienen guardada y el store las lee como "general", así que el default
    # acá es sólo una red por si el dato llega de otra fuente.
    category: str = DEFAULT_CATEGORY
    created_at: str


class Window(BaseModel):
    voting_window_id: str
    law_id: str
    action: str
    category: str = DEFAULT_CATEGORY
    n_zeros_required: int
    opened_at: str
    deadline: str
    partial_hash_base: str
    result: Optional[str] = None
    winning_nonce: Optional[int] = None
    winning_node_or_pool: Optional[str] = None


class Block(BaseModel):
    previous_hash: str
    law_id: str
    action: str
    n_zeros_required: int
    nonce: int
    winning_node_or_pool: str
    voting_window_id: str
    block_hash: str
    timestamp: str


class LawProposalRequest(BaseModel):
    law_id: Optional[str] = None
    author_pubkey: str
    text: str
    action: str = "promulgacion"
    # Área de gobierno que el autor declara y firma. En una derogación se ignora:
    # manda la categoría de la ley original (AGENT.md 3.10).
    category: str = DEFAULT_CATEGORY
    # Campos firmados por el cliente (A-01). El cliente calcula text_hash/created_at
    # y firma `author_pubkey|action|text_hash|law_id|created_at|category`. Si vienen,
    # el API verifica la firma y que text_hash == sha256(text); si no, usa el camino
    # legacy (server-side) salvo que REQUIRE_SIGNATURES esté activo.
    text_hash: Optional[str] = None
    created_at: Optional[str] = None
    signature: Optional[str] = None


class HealthResponse(BaseModel):
    api: str
    nct: str
    redis: str
    workers: str = "unknown"


class WorkerStatus(BaseModel):
    worker_id: str
    mode: str
    pool_url: str = ""
    running: bool
    # Pubkey del **ciudadano dueño**: quien registró el minero y responde por él.
    pubkey: Optional[str] = None
    # Pubkey del **nodo**: la identidad propia con la que este minero firma los
    # nonces que encuentra. Distinta de `pubkey` a propósito (3.1). Vacía hasta
    # que el worker completa su enrolamiento.
    node_pubkey: Optional[str] = None
    # Dirección HTTP con la que el worker es alcanzable si actúa de coordinador.
    # La reporta el propio worker; el backend la usa para armar los equipos.
    address: Optional[str] = None
    # Equipo al que pertenece, resuelto por el backend desde `worker:team:*`.
    team_id: Optional[str] = None
    team_name: Optional[str] = None
    team_role: Optional[str] = None  # 'coordinator' | 'member' | None


class RegisterWorkerRequest(BaseModel):
    worker_id: str
    pubkey: str
    timestamp: str
    signature: str
    # Si el alta además levanta el pod del minero en el clúster. Antes esto se
    # deducía de venir o no una `private_key` en el request; el campo ya no
    # existe (3.1: ninguna clave privada de individuo viaja por la red), así que
    # la intención de desplegar se declara explícitamente.
    deploy: bool = False


class EnrollNodeRequest(BaseModel):
    """Alta de la identidad **propia** de un minero ya registrado.

    La envía el worker desde adentro de su proceso, con la pubkey que él mismo
    generó y el token de un solo uso que el API le dejó en su Secret al
    desplegarlo. El token no autoriza a firmar como el ciudadano: lo único que
    permite es reclamar el slot de nodo de ese `worker_id`.
    """

    worker_id: str
    node_pubkey: str
    enrollment_token: str


class WorkerSwitchRequest(BaseModel):
    target: str
    pool_url: str = ""


class PoolPolicy(BaseModel):
    decision: str
    action: str | None = None
    law_id: str | None = None
    # Agenda temática del pool (AGENT.md 3.10): las categorías a cuyas ventanas
    # aporta cómputo. `None` significa "no la toques" en un PATCH de política;
    # la lista vacía significa "todas". La escribe el flujo de equipos, no el
    # usuario a mano.
    categories: list[str] | None = None


class PoolHealth(BaseModel):
    pool: str
    rabbitmq: str
    miners: int
    voting_policy: dict


class TeamMember(BaseModel):
    worker_id: str
    role: str  # 'coordinator' | 'member'
    mode: str
    running: bool
    # Pubkey del **ciudadano dueño**: quien registró el minero y responde por él.
    pubkey: Optional[str] = None
    # Pubkey del **nodo**: la identidad propia con la que este minero firma los
    # nonces que encuentra. Distinta de `pubkey` a propósito (3.1). Vacía hasta
    # que el worker completa su enrolamiento.
    node_pubkey: Optional[str] = None


class Team(BaseModel):
    team_id: str
    name: str
    owner: str
    coordinator_worker_id: str
    coordinator_url: str
    created_at: str
    # Categorías de ley sobre las que el equipo vota. Vacío = todas (el pool
    # clásico que mina lo que venga). Es la decisión política del equipo: si
    # entra una ley de un área que no eligió, ni el coordinador ni sus mineros
    # aportan un solo hash a esa ventana.
    categories: list[str] = []
    members: list[TeamMember] = []
    member_count: int = 0
    # Mineros que el coordinator tiene efectivamente registrados por HTTP. Es un
    # número distinto de member_count: éste cuenta la intención (quién se anotó
    # al equipo) y aquél la realidad (quién está mandando keep-alive).
    miners_connected: Optional[int] = None
    coordinator_online: bool = False
    # Sólo en la respuesta de crear un equipo que además dio de alta un minero
    # sin desplegarlo (el compose local): es el token con el que ese minero, al
    # levantarlo a mano, vincula la identidad que genera con la de su dueño.
    # Nunca se persiste en el equipo ni se devuelve al listarlo.
    enrollment_token: Optional[str] = None


class CreateTeamRequest(BaseModel):
    name: str
    worker_id: str
    categories: list[str] = []
    # Alta del minero en el mismo paso, para el usuario que todavía no tiene
    # ninguno. Mismos campos que RegisterWorkerRequest. El nombre del campo
    # evita `register`, que pisa un atributo de BaseModel.
    new_worker: Optional[RegisterWorkerRequest] = None


class TeamMembershipRequest(BaseModel):
    worker_id: str


class TeamCategoriesRequest(BaseModel):
    """Cambio de agenda del equipo. Lista vacía = vuelve a votar todas."""

    categories: list[str] = []


class SSEEvent(BaseModel):
    event_type: str
    data: dict


class DemoAccount(BaseModel):
    username: str
    worker_id: str
    mode: str
    pubkey: str
    status: str  # 'available' | 'occupied'
    occupied_by: Optional[str] = None  # session_id
    occupied_at: Optional[str] = None


class ReserveAccountRequest(BaseModel):
    username: str
    session_id: str


class ReleaseAccountRequest(BaseModel):
    username: str
    session_id: str
