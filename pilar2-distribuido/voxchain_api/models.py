"""Pydantic response models for voxchain-api."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Law(BaseModel):
    law_id: str
    author_pubkey: str
    text_hash: str
    text_ref: Optional[str] = None
    text_compressed: Optional[str] = None
    text_original_len: Optional[int] = None
    status: str
    action: str
    created_at: str


class Window(BaseModel):
    voting_window_id: str
    law_id: str
    action: str
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
    # Campos firmados por el cliente (A-01). El cliente calcula text_hash/created_at
    # y firma `author_pubkey|action|text_hash|law_id|created_at`. Si vienen, el API
    # verifica la firma y que text_hash == sha256(text); si no, usa el camino legacy
    # (server-side) salvo que REQUIRE_SIGNATURES esté activo.
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
    pubkey: Optional[str] = None
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
    private_key: Optional[str] = None


class WorkerSwitchRequest(BaseModel):
    target: str
    pool_url: str = ""


class PoolPolicy(BaseModel):
    decision: str
    action: str | None = None
    law_id: str | None = None


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
    pubkey: Optional[str] = None


class Team(BaseModel):
    team_id: str
    name: str
    owner: str
    coordinator_worker_id: str
    coordinator_url: str
    created_at: str
    members: list[TeamMember] = []
    member_count: int = 0
    # Mineros que el coordinator tiene efectivamente registrados por HTTP. Es un
    # número distinto de member_count: éste cuenta la intención (quién se anotó
    # al equipo) y aquél la realidad (quién está mandando keep-alive).
    miners_connected: Optional[int] = None
    coordinator_online: bool = False


class CreateTeamRequest(BaseModel):
    name: str
    worker_id: str
    # Alta del minero en el mismo paso, para el usuario que todavía no tiene
    # ninguno. Mismos campos que RegisterWorkerRequest. El nombre del campo
    # evita `register`, que pisa un atributo de BaseModel.
    new_worker: Optional[RegisterWorkerRequest] = None


class TeamMembershipRequest(BaseModel):
    worker_id: str


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
