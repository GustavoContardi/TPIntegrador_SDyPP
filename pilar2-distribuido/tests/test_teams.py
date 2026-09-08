"""Equipos de minado: almacenamiento y flujo extremo a extremo por la API.

Lo que estos tests protegen es la invariante del diseño: **el estado del equipo
y el modo real del worker se mueven juntos**. Cada alta o baja tiene que
despachar el ``switch_mode`` correspondiente, y no tiene que existir ningún
camino que cambie uno sin el otro — si lo hubiera, la lista de miembros
mostraría gente que ya no está minando para el equipo.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import fakeredis
import pytest

from voxchain_api.services.teams_store import TeamError, TeamsStore, slugify_team

# pubkey -> identidad, para que el cliente de tests sepa con qué clave firmar
# cada llamada a partir del `X-Owner-Id` que le pasaron.
_IDENTIDADES: dict[str, "_Identidad"] = {}


@pytest.fixture
def r():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def teams(r):
    return TeamsStore(r)


def _online(r, worker_id, mode="standalone", address=None):
    """Simula el estado que un worker publica en Redis cada 5 segundos."""
    r.set(f"worker:status:{worker_id}", json.dumps({
        "worker_id": worker_id,
        "mode": mode,
        "running": True,
        "pool_url": "",
        "address": address or f"http://{worker_id}:9001",
    }), ex=15)


# --------------------------------------------------------------------------
# TeamsStore
# --------------------------------------------------------------------------

class TestSlug:
    def test_normaliza_nombre_con_espacios_y_mayusculas(self):
        assert slugify_team("Los Pibes  del Barrio").startswith("los-pibes-del-barrio-")

    def test_nombres_iguales_dan_ids_distintos(self):
        # Dos equipos pueden llamarse igual: el nombre es de la persona, el id
        # es del sistema.
        assert slugify_team("Equipo") != slugify_team("Equipo")

    def test_nombre_sin_caracteres_utiles_no_produce_id_vacio(self):
        assert slugify_team("---").startswith("equipo-")


class TestCrear:
    def test_crear_deja_al_worker_como_coordinador(self, teams):
        team = teams.create_team(name="Los Pibes", owner=GUS.pubkey,
                                 coordinator_worker_id="w1",
                                 coordinator_url="http://w1:9001")
        assert teams.team_of_worker("w1") == team["team_id"]
        assert teams.role_of_worker("w1") == "coordinator"
        assert team["members"] == []

    def test_un_worker_no_puede_coordinar_dos_equipos(self, teams):
        teams.create_team(name="A", owner="pk", coordinator_worker_id="w1",
                          coordinator_url="u")
        with pytest.raises(TeamError) as exc:
            teams.create_team(name="B", owner="pk", coordinator_worker_id="w1",
                              coordinator_url="u")
        assert exc.value.status_code == 409

    def test_list_teams_descarta_ids_huerfanos(self, teams, r):
        teams.create_team(name="A", owner="pk", coordinator_worker_id="w1",
                          coordinator_url="u")
        r.sadd("teams", "fantasma-abc123")
        assert [t["name"] for t in teams.list_teams()] == ["A"]
        # y además limpia el set, para no repetir el trabajo en cada lectura
        assert not r.sismember("teams", "fantasma-abc123")


class TestUnirse:
    @pytest.fixture
    def team_id(self, teams):
        return teams.create_team(name="Los Pibes", owner=GUS.pubkey,
                                 coordinator_worker_id="coord",
                                 coordinator_url="http://coord:9001")["team_id"]

    def test_unirse_agrega_al_plantel(self, teams, team_id):
        team = teams.join_team(team_id, "w2")
        assert team["members"] == ["w2"]
        assert teams.role_of_worker("w2") == "member"

    def test_no_se_puede_unir_dos_veces(self, teams, team_id):
        teams.join_team(team_id, "w2")
        with pytest.raises(TeamError) as exc:
            teams.join_team(team_id, "w2")
        assert exc.value.status_code == 409

    def test_no_se_puede_estar_en_dos_equipos(self, teams, team_id):
        otro = teams.create_team(name="Otro", owner="pk2",
                                 coordinator_worker_id="coord2",
                                 coordinator_url="u")["team_id"]
        teams.join_team(team_id, "w2")
        with pytest.raises(TeamError) as exc:
            teams.join_team(otro, "w2")
        assert exc.value.status_code == 409

    def test_unirse_a_equipo_inexistente(self, teams):
        with pytest.raises(TeamError) as exc:
            teams.join_team("no-existe", "w2")
        assert exc.value.status_code == 404


class TestSalirYDisolver:
    @pytest.fixture
    def team_id(self, teams):
        tid = teams.create_team(name="Los Pibes", owner=GUS.pubkey,
                                coordinator_worker_id="coord",
                                coordinator_url="http://coord:9001")["team_id"]
        teams.join_team(tid, "w2")
        teams.join_team(tid, "w3")
        return tid

    def test_salir_libera_al_miembro(self, teams, team_id):
        teams.leave_team("w2")
        assert teams.team_of_worker("w2") is None
        assert teams.get_team(team_id)["members"] == ["w3"]

    def test_el_coordinador_no_puede_abandonar_su_equipo(self, teams, team_id):
        # Si pudiera, sus miembros quedarían pidiéndole fragmentos a un HTTP
        # que ya no reparte. Tiene que disolver.
        with pytest.raises(TeamError) as exc:
            teams.leave_team("coord")
        assert exc.value.status_code == 409

    def test_salir_sin_equipo(self, teams):
        with pytest.raises(TeamError) as exc:
            teams.leave_team("suelto")
        assert exc.value.status_code == 404

    def test_disolver_devuelve_a_todos_y_limpia_indices(self, teams, team_id, r):
        liberados = teams.dissolve_team(team_id)
        assert set(liberados) == {"coord", "w2", "w3"}
        for worker_id in liberados:
            assert teams.team_of_worker(worker_id) is None
        assert teams.get_team(team_id) is None
        assert not r.sismember("teams", team_id)
        assert not r.exists(f"team:members:{team_id}")

    def test_detach_worker_distingue_el_rol(self, teams, team_id):
        assert teams.detach_worker("w2") == (team_id, "member")
        assert teams.team_of_worker("w2") is None
        # El coordinador se informa pero NO se desvincula: desarmarlo requiere
        # disolver el equipo entero.
        assert teams.detach_worker("coord") == (team_id, "coordinator")
        assert teams.team_of_worker("coord") == team_id
        assert teams.detach_worker("suelto") is None


# --------------------------------------------------------------------------
# Flujo por la API
# --------------------------------------------------------------------------

class FakeMessaging:
    def __init__(self):
        self.commands: list[tuple[str, dict]] = []

    def publish_worker_command(self, worker_id, command):
        self.commands.append((worker_id, command))

    def close(self):
        pass


class FakePublisher:
    def __init__(self):
        self.messaging = FakeMessaging()
        self.closed = 0

    def close(self):
        self.closed += 1


# Qué recurso y qué acción firma cada endpoint de administración. Es el mismo
# mapa que el frontend tiene en su capa de API; acá vive para que los tests
# hablen de quién puede hacer qué y no de cómo se arma una firma.
_ACCIONES_FIRMADAS = [
    ("POST", re.compile(r"^/api/workers/(?P<rec>[^/]+)/switch-mode$"), "switch-mode", None),
    ("POST", re.compile(r"^/api/workers/pool/(?P<rec>[^/]+)/policy$"), "pool-policy", None),
    ("POST", re.compile(r"^/api/teams$"), "create-team", "worker_id"),
    ("POST", re.compile(r"^/api/teams/[^/]+/join$"), "join-team", "worker_id"),
    ("POST", re.compile(r"^/api/teams/[^/]+/leave$"), "leave-team", "worker_id"),
    ("PUT", re.compile(r"^/api/teams/(?P<rec>[^/]+)/categories$"), "set-categories", None),
    ("DELETE", re.compile(r"^/api/teams/(?P<rec>[^/]+)$"), "dissolve-team", None),
]


class _ClienteFirmante:
    """TestClient que firma las acciones de administración por el llamador.

    Desde que esos endpoints exigen probar la posesión de la clave y no sólo
    declararla, cada llamada necesita `X-Signature` y `X-Timestamp` sobre
    `recurso|acción|timestamp`. Armar eso a mano en cada test enterraría lo que
    el test prueba bajo cinco líneas de criptografía, así que se deduce del
    método, la URL y el cuerpo — igual que hace el frontend.

    Firma con la identidad que corresponda al `X-Owner-Id` que pasó el test. Si
    no reconoce esa identidad (una cuenta demo, o un `X-Owner-Id` inventado a
    propósito) no firma nada: es justo el caso que varios tests quieren ejercer.
    """

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        return getattr(self._client, name)

    def _firmar(self, metodo, url, headers, json_body):
        quien = _IDENTIDADES.get((headers or {}).get("X-Owner-Id", ""))
        if quien is None:
            return headers
        for met, patron, accion, campo in _ACCIONES_FIRMADAS:
            if met != metodo:
                continue
            match = patron.match(url)
            if not match:
                continue
            recurso = ((json_body or {}).get(campo, "") if campo
                       else match.group("rec"))
            ts = datetime.now(timezone.utc).isoformat()
            return {**headers, "X-Timestamp": ts,
                    "X-Signature": quien.firma(f"{recurso}|{accion}|{ts}")}
        return headers

    def post(self, url, json=None, headers=None, **kw):
        return self._client.post(url, json=json,
                                 headers=self._firmar("POST", url, headers, json), **kw)

    def put(self, url, json=None, headers=None, **kw):
        return self._client.put(url, json=json,
                                headers=self._firmar("PUT", url, headers, json), **kw)

    def delete(self, url, headers=None, **kw):
        return self._client.delete(url,
                                   headers=self._firmar("DELETE", url, headers, None), **kw)


@pytest.fixture
def api(r):
    """TestClient con Redis falso y RabbitMQ falso, sin tocar la red."""
    from fastapi.testclient import TestClient

    from voxchain_api.main import app
    from voxchain_api.routers import workers as workers_router

    publisher = FakePublisher()

    class FakeReader:
        def __init__(self, client):
            self.store = type("S", (), {"r": client})()

    app.dependency_overrides[workers_router.get_redis_reader] = lambda: FakeReader(r)
    app.dependency_overrides[workers_router.get_rabbitmq_publisher] = lambda: publisher
    client = TestClient(app)
    client.publisher = publisher
    yield _ClienteFirmante(client)
    app.dependency_overrides.clear()


def _own(r, worker_id, owner):
    """Marca a ``owner`` como dueño de ``worker_id`` (lo que hace el registro)."""
    r.sadd("registered_workers", worker_id)
    r.set(f"worker:owner:{worker_id}", owner)
    r.set(f"worker:pubkey:{worker_id}", owner)


class _Identidad:
    """Una identidad de ciudadano de mentira, para firmar altas y bajas.

    Guarda la privada porque varios tests necesitan firmar **dos veces con la
    misma identidad** (re-registrar un minero, o darlo de baja después de
    haberlo dado de alta); con una clave nueva por firma el backend contestaría
    409/401 y el test estaría probando otra cosa.
    """

    def __init__(self):
        import base64

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        self.key = ec.generate_private_key(ec.SECP256R1())
        self.pubkey = base64.b64encode(self.key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
        _IDENTIDADES[self.pubkey] = self

    def firma(self, mensaje: str) -> str:
        import base64

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils as asym

        der = self.key.sign(mensaje.encode(), ec.ECDSA(hashes.SHA256()))
        rr, ss = asym.decode_dss_signature(der)
        return base64.b64encode(rr.to_bytes(32, "big") + ss.to_bytes(32, "big")).decode()

    def registro(self, worker_id: str):
        from datetime import datetime, timezone

        from voxchain_api.models import RegisterWorkerRequest

        ts = datetime.now(timezone.utc).isoformat()
        return RegisterWorkerRequest(
            worker_id=worker_id, pubkey=self.pubkey, timestamp=ts,
            signature=self.firma(f"{worker_id}|register|{ts}"))


# Los cuatro personajes de los tests de equipos. Son claves de verdad porque el
# backend verifica firmas contra el dueño guardado: un `"pk-gus"` cualquiera ya
# no sirve, no se puede cargar como clave ni firmar con ella.
GUS = _Identidad()
VALEN = _Identidad()
OTRO = _Identidad()
INTRUSO = _Identidad()


def _registro_firmado(worker_id: str):
    """Alta válida de un minero, firmada con una identidad nueva."""
    return _Identidad().registro(worker_id)


def _commands_for(client, worker_id):
    return [cmd for wid, cmd in client.publisher.messaging.commands if wid == worker_id]


class TestFlujoApi:
    def test_crear_equipo_promueve_el_worker_a_coordinador(self, api, r):
        _own(r, "mi-minero", GUS.pubkey)
        _online(r, "mi-minero")

        resp = api.post("/api/teams", json={"name": "Los Pibes",
                                            "worker_id": "mi-minero"},
                        headers={"X-Owner-Id": GUS.pubkey})
        assert resp.status_code == 200, resp.text
        team = resp.json()
        assert team["name"] == "Los Pibes"
        assert team["coordinator_worker_id"] == "mi-minero"
        assert team["coordinator_online"] is True

        assert _commands_for(api, "mi-minero") == [
            {"type": "switch_mode", "mode": "pool-coordinator", "pool_url": ""}
        ]

    def test_crear_equipo_con_worker_ajeno_es_403(self, api, r):
        _own(r, "minero-de-otro", OTRO.pubkey)
        resp = api.post("/api/teams", json={"name": "Robo", "worker_id": "minero-de-otro"},
                        headers={"X-Owner-Id": GUS.pubkey})
        assert resp.status_code == 403

    def test_crear_equipo_sin_nombre_es_400(self, api, r):
        _own(r, "w", GUS.pubkey)
        resp = api.post("/api/teams", json={"name": "   ", "worker_id": "w"},
                        headers={"X-Owner-Id": GUS.pubkey})
        assert resp.status_code == 400

    def test_unirse_usa_la_direccion_que_publica_el_coordinador(self, api, r):
        _own(r, "coord", GUS.pubkey)
        _online(r, "coord", address="http://10.42.0.7:9001")
        team_id = api.post("/api/teams", json={"name": "Los Pibes", "worker_id": "coord"},
                           headers={"X-Owner-Id": GUS.pubkey}).json()["team_id"]
        # el coordinador ya arrancó y reporta su modo real
        _online(r, "coord", mode="pool-coordinator", address="http://10.42.0.7:9001")

        _own(r, "otro-minero", VALEN.pubkey)
        _online(r, "otro-minero")
        resp = api.post(f"/api/teams/{team_id}/join",
                        json={"worker_id": "otro-minero"},
                        headers={"X-Owner-Id": VALEN.pubkey})
        assert resp.status_code == 200, resp.text
        assert resp.json()["member_count"] == 2

        # La URL del pool NO la escribió nadie a mano: salió del `address` que
        # el coordinador publica de sí mismo.
        assert _commands_for(api, "otro-minero") == [
            {"type": "switch_mode", "mode": "pool-worker",
             "pool_url": "http://10.42.0.7:9001"}
        ]

    def test_se_puede_unir_aunque_el_coordinador_no_este_encendido(self, api, r):
        """Unirse no exige que las dos puntas estén prendidas al mismo tiempo.

        Antes acá había un 409 ("el coordinador no está en línea todavía") para
        no mandar a un minero a pedirle trabajo a un HTTP muerto. Salió peor el
        remedio: registrar un minero **no lo enciende**, así que un equipo recién
        fundado tenía a su coordinador apagado por definición y quedaba imposible
        de integrar para siempre — no era un caso de borde, era el caso normal.

        Ahora se puede entrar antes: la intención queda escrita, la URL se
        corrige sola cuando el coordinador aparece, y el pool se arma solo en
        cuanto ambos están encendidos.
        """
        _own(r, "coord", GUS.pubkey)
        _online(r, "coord")
        team_id = api.post("/api/teams", json={"name": "Los Pibes", "worker_id": "coord"},
                           headers={"X-Owner-Id": GUS.pubkey}).json()["team_id"]
        r.delete("worker:status:coord")  # el coordinador se apagó

        _own(r, "otro", VALEN.pubkey)
        resp = api.post(f"/api/teams/{team_id}/join", json={"worker_id": "otro"},
                        headers={"X-Owner-Id": VALEN.pubkey})
        assert resp.status_code == 200, resp.text
        assert resp.json()["member_count"] == 2

        # La intención quedó escrita, que es lo que lo hace seguro: el minero la
        # va a leer cuando arranque, aunque el comando de RabbitMQ se pierda.
        desired = json.loads(r.get("worker:desired_mode:otro"))
        assert desired["mode"] == "pool-worker"
        assert desired["pool_url"]

    def test_salir_del_equipo_vuelve_a_competitivo(self, api, r):
        _own(r, "coord", GUS.pubkey)
        _online(r, "coord", mode="pool-coordinator")
        team_id = api.post("/api/teams", json={"name": "T", "worker_id": "coord"},
                           headers={"X-Owner-Id": GUS.pubkey}).json()["team_id"]
        _own(r, "w2", VALEN.pubkey)
        _online(r, "w2")
        api.post(f"/api/teams/{team_id}/join", json={"worker_id": "w2"},
                 headers={"X-Owner-Id": VALEN.pubkey})

        resp = api.post(f"/api/teams/{team_id}/leave", json={"worker_id": "w2"},
                        headers={"X-Owner-Id": VALEN.pubkey})
        assert resp.status_code == 200
        assert _commands_for(api, "w2")[-1]["mode"] == "standalone"
        assert api.get(f"/api/teams/{team_id}").json()["member_count"] == 1

    def test_disolver_libera_a_todo_el_plantel(self, api, r):
        _own(r, "coord", GUS.pubkey)
        _online(r, "coord", mode="pool-coordinator")
        team_id = api.post("/api/teams", json={"name": "T", "worker_id": "coord"},
                           headers={"X-Owner-Id": GUS.pubkey}).json()["team_id"]
        _own(r, "w2", VALEN.pubkey)
        _online(r, "w2")
        api.post(f"/api/teams/{team_id}/join", json={"worker_id": "w2"},
                 headers={"X-Owner-Id": VALEN.pubkey})

        resp = api.delete(f"/api/teams/{team_id}", headers={"X-Owner-Id": GUS.pubkey})
        assert resp.status_code == 200
        assert set(resp.json()["released"]) == {"coord", "w2"}
        # Nadie queda apuntando a un coordinador que ya no reparte trabajo.
        assert _commands_for(api, "coord")[-1]["mode"] == "standalone"
        assert _commands_for(api, "w2")[-1]["mode"] == "standalone"
        assert api.get(f"/api/teams/{team_id}").status_code == 404

    def test_solo_el_creador_disuelve(self, api, r):
        _own(r, "coord", GUS.pubkey)
        _online(r, "coord")
        team_id = api.post("/api/teams", json={"name": "T", "worker_id": "coord"},
                           headers={"X-Owner-Id": GUS.pubkey}).json()["team_id"]
        resp = api.delete(f"/api/teams/{team_id}", headers={"X-Owner-Id": INTRUSO.pubkey})
        assert resp.status_code == 403


class TestSwitchModeEsSoloVueltaACompetitivo:
    """El modo cooperativo se entra por equipos y por ningún otro lado."""

    def test_pedir_pool_worker_a_mano_es_400(self, api, r):
        _own(r, "w1", GUS.pubkey)
        resp = api.post("/api/workers/w1/switch-mode",
                        json={"target": "pool-worker", "pool_url": "http://loquesea:9001"},
                        headers={"X-Owner-Id": GUS.pubkey})
        assert resp.status_code == 400
        assert "equipo" in resp.json()["detail"].lower()
        assert _commands_for(api, "w1") == []

    def test_pedir_pool_coordinator_a_mano_es_400(self, api, r):
        _own(r, "w1", GUS.pubkey)
        resp = api.post("/api/workers/w1/switch-mode", json={"target": "pool-coordinator"},
                        headers={"X-Owner-Id": GUS.pubkey})
        assert resp.status_code == 400

    def test_volver_a_competitivo_saca_del_equipo(self, api, r):
        _own(r, "coord", GUS.pubkey)
        _online(r, "coord", mode="pool-coordinator")
        team_id = api.post("/api/teams", json={"name": "T", "worker_id": "coord"},
                           headers={"X-Owner-Id": GUS.pubkey}).json()["team_id"]
        _own(r, "w2", VALEN.pubkey)
        _online(r, "w2")
        api.post(f"/api/teams/{team_id}/join", json={"worker_id": "w2"},
                 headers={"X-Owner-Id": VALEN.pubkey})

        resp = api.post("/api/workers/w2/switch-mode", json={"target": "standalone"},
                        headers={"X-Owner-Id": VALEN.pubkey})
        assert resp.status_code == 200
        assert resp.json()["left_team"] == team_id
        # el equipo se enteró: ya no lo cuenta como miembro
        assert api.get(f"/api/teams/{team_id}").json()["member_count"] == 1

    def test_el_coordinador_no_sale_por_switch_mode(self, api, r):
        _own(r, "coord", GUS.pubkey)
        _online(r, "coord", mode="pool-coordinator")
        api.post("/api/teams", json={"name": "T", "worker_id": "coord"},
                 headers={"X-Owner-Id": GUS.pubkey})
        resp = api.post("/api/workers/coord/switch-mode", json={"target": "standalone"},
                        headers={"X-Owner-Id": GUS.pubkey})
        assert resp.status_code == 409
        assert "disolv" in resp.json()["detail"].lower()


class TestListadoDeMineros:
    def test_el_listado_dice_a_que_equipo_pertenece_cada_minero(self, api, r):
        _own(r, "coord", GUS.pubkey)
        _online(r, "coord", mode="pool-coordinator")
        _own(r, "w2", VALEN.pubkey)
        _online(r, "w2")
        _own(r, "solo", OTRO.pubkey)
        _online(r, "solo")

        team_id = api.post("/api/teams", json={"name": "Los Pibes", "worker_id": "coord"},
                           headers={"X-Owner-Id": GUS.pubkey}).json()["team_id"]
        api.post(f"/api/teams/{team_id}/join", json={"worker_id": "w2"},
                 headers={"X-Owner-Id": VALEN.pubkey})

        by_id = {w["worker_id"]: w for w in api.get("/api/workers/status").json()}
        assert by_id["coord"]["team_role"] == "coordinator"
        assert by_id["coord"]["team_name"] == "Los Pibes"
        assert by_id["w2"]["team_role"] == "member"
        assert by_id["solo"]["team_id"] is None


class TestNoInventarWorkersVivos:
    """Un worker que nunca reportó no puede pasar por vivo.

    La escritura optimista de estado (para que la UI no muestre el modo viejo
    mientras el worker aplica el cambio) tiene que refinar lo reportado, no
    fabricarlo: si inventa `running: True` para un pod que todavía no arrancó, el
    equipo muestra a su coordinador en línea con una dirección adivinada y
    cualquiera puede unirse a un pool que no existe.
    """

    def test_equipo_con_coordinador_sin_reportar_no_figura_en_linea(self, api, r):
        # Alta sin _online(): es el caso del minero recién desplegado, cuyo pod
        # tarda decenas de segundos en levantar y reportar.
        _own(r, "recien-creado", GUS.pubkey)

        resp = api.post("/api/teams", json={"name": "Nuevo", "worker_id": "recien-creado"},
                        headers={"X-Owner-Id": GUS.pubkey})
        assert resp.status_code == 200, resp.text
        assert resp.json()["coordinator_online"] is False

    def test_la_orden_se_manda_igual(self, api, r):
        """No saber si arrancó no es razón para no mandarle la orden.

        Cuando el pod levante va a consumir su `worker.command` y arrancar ya en
        modo coordinador.
        """
        _own(r, "recien-creado", GUS.pubkey)
        api.post("/api/teams", json={"name": "Nuevo", "worker_id": "recien-creado"},
                 headers={"X-Owner-Id": GUS.pubkey})
        assert _commands_for(api, "recien-creado") == [
            {"type": "switch_mode", "mode": "pool-coordinator", "pool_url": ""}
        ]

    def test_el_estado_reportado_si_se_refina(self, api, r):
        """Con estado previo, la UI ve el modo nuevo sin esperar al worker."""
        _own(r, "vivo", GUS.pubkey)
        _online(r, "vivo", address="http://10.42.0.9:9001")
        api.post("/api/teams", json={"name": "T", "worker_id": "vivo"},
                 headers={"X-Owner-Id": GUS.pubkey})
        by_id = {w["worker_id"]: w for w in api.get("/api/workers/status").json()}
        assert by_id["vivo"]["mode"] == "pool-coordinator"
        # y la dirección que el worker había publicado sobrevive al refinamiento
        assert by_id["vivo"]["address"] == "http://10.42.0.9:9001"


class TestUnSoloEquipoPorIdentidad:
    """Fundar equipo es un derecho por identidad, no por minero.

    Un equipo concentra poder de cómputo (AGENT.md 3.9). Dejar que una misma
    clave funde varios le daría a un solo individuo tantos frentes como quisiera,
    agravando gratis la concentración de poder que el sistema documenta como su
    debilidad.

    Los tests usan ``_own`` para dar de alta mineros directamente en Redis, así
    que ejercitan esta regla sin pasar por el cupo de un minero por identidad
    (ver ``TestUnMineroPorIdentidad``), que se aplica en el alta. Son dos capas
    distintas y conviene poder probarlas por separado.
    """

    def test_la_segunda_fundacion_es_409(self, api, r):
        _own(r, "w1", GUS.pubkey)
        _online(r, "w1")
        _own(r, "w2", GUS.pubkey)
        _online(r, "w2")

        assert api.post("/api/teams", json={"name": "Primero", "worker_id": "w1"},
                        headers={"X-Owner-Id": GUS.pubkey}).status_code == 200

        resp = api.post("/api/teams", json={"name": "Segundo", "worker_id": "w2"},
                        headers={"X-Owner-Id": GUS.pubkey})
        assert resp.status_code == 409
        assert "Primero" in resp.json()["detail"]
        # y el minero libre no quedó promovido a coordinador de la nada
        assert _commands_for(api, "w2") == []

    def test_otra_identidad_si_puede_fundar(self, api, r):
        _own(r, "w1", GUS.pubkey)
        _online(r, "w1")
        _own(r, "w2", VALEN.pubkey)
        _online(r, "w2")

        assert api.post("/api/teams", json={"name": "De Gus", "worker_id": "w1"},
                        headers={"X-Owner-Id": GUS.pubkey}).status_code == 200
        assert api.post("/api/teams", json={"name": "De Valen", "worker_id": "w2"},
                        headers={"X-Owner-Id": VALEN.pubkey}).status_code == 200

    def test_disolver_libera_el_derecho_a_fundar(self, api, r):
        _own(r, "w1", GUS.pubkey)
        _online(r, "w1")
        team_id = api.post("/api/teams", json={"name": "Primero", "worker_id": "w1"},
                           headers={"X-Owner-Id": GUS.pubkey}).json()["team_id"]

        api.delete(f"/api/teams/{team_id}", headers={"X-Owner-Id": GUS.pubkey})

        assert api.post("/api/teams", json={"name": "Segundo", "worker_id": "w1"},
                        headers={"X-Owner-Id": GUS.pubkey}).status_code == 200

    def test_el_equipo_no_limita_cuantos_mineros_pone_cada_identidad(self, api, r):
        """La membresía no mira al dueño; el cupo se aplica al registrar.

        Por la API real una identidad llega con un solo minero, pero esa
        restricción vive en el alta y no acá: el equipo acepta a quien le manden.
        """
        _own(r, "coord", GUS.pubkey)
        _online(r, "coord", mode="pool-coordinator")
        team_id = api.post("/api/teams", json={"name": "Los Pibes", "worker_id": "coord"},
                           headers={"X-Owner-Id": GUS.pubkey}).json()["team_id"]

        for worker_id in ("v1", "v2"):
            _own(r, worker_id, VALEN.pubkey)
            _online(r, worker_id)
            assert api.post(f"/api/teams/{team_id}/join", json={"worker_id": worker_id},
                            headers={"X-Owner-Id": VALEN.pubkey}).status_code == 200

        assert api.get(f"/api/teams/{team_id}").json()["member_count"] == 3

    def test_el_indice_colgado_no_bloquea_para_siempre(self, teams, r):
        """Si el hash del equipo desapareció, la identidad vuelve a poder fundar."""
        teams.create_team(name="A", owner="pk", coordinator_worker_id="w1",
                          coordinator_url="u")
        r.delete(*[k for k in r.scan_iter("team:*") if not k.startswith("team:owner:")])
        assert teams.team_of_owner("pk") is None


class TestAgendaDelEquipo:
    """Las categorías que vota el equipo (AGENT.md 3.10).

    La invariante es la misma que la de los modos: **el estado del equipo y lo
    que hace el coordinador se mueven juntos**. Toda escritura de agenda tiene
    que bajar en el acto a `pool:policy:<coordinador>`, que es lo único que el
    coordinador lee. Una agenda guardada que no llegó allá es un equipo que dice
    votar economía y sigue minando todo.
    """

    def _policy(self, r, worker_id):
        raw = r.get(f"pool:policy:{worker_id}")
        return json.loads(raw) if raw else None

    def test_fundar_con_agenda_la_baja_al_coordinador(self, api, r):
        _own(r, "coord", GUS.pubkey)
        _online(r, "coord")

        resp = api.post("/api/teams",
                        json={"name": "Los Economistas", "worker_id": "coord",
                              "categories": ["economia", "salud"]},
                        headers={"X-Owner-Id": GUS.pubkey})
        assert resp.status_code == 200, resp.text
        assert resp.json()["categories"] == ["economia", "salud"]
        assert self._policy(r, "coord")["categories"] == ["economia", "salud"]

    def test_fundar_sin_agenda_vota_todas(self, api, r):
        _own(r, "coord", GUS.pubkey)
        _online(r, "coord")
        resp = api.post("/api/teams", json={"name": "Los Pibes", "worker_id": "coord"},
                        headers={"X-Owner-Id": GUS.pubkey})
        assert resp.json()["categories"] == []
        # y la política que baja es explícitamente "sin agenda", no la ausencia
        # de política: así el coordinador que venía de otro equipo se entera.
        assert self._policy(r, "coord")["categories"] == []

    def test_categoria_inventada_es_400(self, api, r):
        _own(r, "coord", GUS.pubkey)
        _online(r, "coord")
        resp = api.post("/api/teams",
                        json={"name": "Astrólogos", "worker_id": "coord",
                              "categories": ["astrologia"]},
                        headers={"X-Owner-Id": GUS.pubkey})
        assert resp.status_code == 400
        assert "astrologia" in resp.json()["detail"]
        # y no quedó un equipo a medio fundar
        assert api.get("/api/teams").json() == []

    def test_cambiar_la_agenda_la_baja_al_coordinador(self, api, r):
        _own(r, "coord", GUS.pubkey)
        _online(r, "coord", mode="pool-coordinator")
        team_id = api.post("/api/teams", json={"name": "T", "worker_id": "coord",
                                               "categories": ["economia"]},
                           headers={"X-Owner-Id": GUS.pubkey}).json()["team_id"]

        resp = api.put(f"/api/teams/{team_id}/categories",
                       json={"categories": ["ambiente", "salud"]},
                       headers={"X-Owner-Id": GUS.pubkey})
        assert resp.status_code == 200, resp.text
        assert resp.json()["categories"] == ["salud", "ambiente"]
        assert self._policy(r, "coord")["categories"] == ["salud", "ambiente"]

    def test_vaciar_la_agenda_vuelve_a_votar_todo(self, api, r):
        _own(r, "coord", GUS.pubkey)
        _online(r, "coord", mode="pool-coordinator")
        team_id = api.post("/api/teams", json={"name": "T", "worker_id": "coord",
                                               "categories": ["economia"]},
                           headers={"X-Owner-Id": GUS.pubkey}).json()["team_id"]

        resp = api.put(f"/api/teams/{team_id}/categories", json={"categories": []},
                       headers={"X-Owner-Id": GUS.pubkey})
        assert resp.json()["categories"] == []
        assert self._policy(r, "coord")["categories"] == []

    def test_solo_el_fundador_cambia_la_agenda(self, api, r):
        """La agenda es lo que el equipo es frente al resto de la red.

        Si cualquier miembro pudiera reescribirla, se entraría a un equipo sólo
        para desviarle el cómputo a otra área.
        """
        _own(r, "coord", GUS.pubkey)
        _online(r, "coord", mode="pool-coordinator")
        team_id = api.post("/api/teams", json={"name": "T", "worker_id": "coord",
                                               "categories": ["economia"]},
                           headers={"X-Owner-Id": GUS.pubkey}).json()["team_id"]
        _own(r, "w2", VALEN.pubkey)
        _online(r, "w2")
        api.post(f"/api/teams/{team_id}/join", json={"worker_id": "w2"},
                 headers={"X-Owner-Id": VALEN.pubkey})

        resp = api.put(f"/api/teams/{team_id}/categories",
                       json={"categories": ["salud"]},
                       headers={"X-Owner-Id": VALEN.pubkey})
        assert resp.status_code == 403
        assert self._policy(r, "coord")["categories"] == ["economia"]

    def test_disolver_borra_la_politica_del_coordinador(self, api, r):
        """Fundar otro equipo con el mismo minero no debe heredar la agenda vieja."""
        _own(r, "coord", GUS.pubkey)
        _online(r, "coord", mode="pool-coordinator")
        team_id = api.post("/api/teams", json={"name": "T", "worker_id": "coord",
                                               "categories": ["economia"]},
                           headers={"X-Owner-Id": GUS.pubkey}).json()["team_id"]
        assert self._policy(r, "coord") is not None

        api.delete(f"/api/teams/{team_id}", headers={"X-Owner-Id": GUS.pubkey})
        assert self._policy(r, "coord") is None

    def test_la_agenda_sobrevive_a_un_cambio_de_politica_de_acciones(self, api, r):
        """Las dos pantallas escriben la misma clave y no deben pisarse.

        La política por acción/ley se toca desde Mineros; la agenda desde
        Equipos. Un POST de política sin `categories` significa "no la toques".
        """
        _own(r, "coord", GUS.pubkey)
        _online(r, "coord", mode="pool-coordinator")
        api.post("/api/teams", json={"name": "T", "worker_id": "coord",
                                     "categories": ["economia"]},
                 headers={"X-Owner-Id": GUS.pubkey})

        resp = api.post("/api/workers/pool/coord/policy",
                        json={"decision": "reject", "action": "derogacion"},
                        headers={"X-Owner-Id": GUS.pubkey})
        assert resp.status_code == 200, resp.text
        policy = self._policy(r, "coord")
        assert policy["categories"] == ["economia"]
        assert policy["decision"] == "reject"


class TestIntencionPersistida:
    """El modo de un minero tiene que sobrevivir a que el minero esté apagado.

    El comando de cambio de modo viaja por RabbitMQ a una **cola exclusiva** del
    worker: si el minero no está conectado, no hay quien la consuma y la orden se
    pierde sin que nadie se entere. Como registrar un minero desde la UI no lo
    enciende, ese era el caso corriente — el dueño lo asignaba a un equipo, la UI
    respondía 200, y el minero jamás se enteraba.

    La intención se guarda en `worker:desired_mode:<id>` y el minero la lee al
    arrancar y la reconcilia mientras corre, así que el mensaje pasó a ser sólo
    una optimización de latencia.
    """

    def test_asignar_a_un_equipo_deja_la_intencion_escrita(self, api, r):
        _own(r, "coord", GUS.pubkey)
        _online(r, "coord", address="http://10.0.0.5:9001")
        team_id = api.post("/api/teams", json={"name": "T", "worker_id": "coord"},
                           headers={"X-Owner-Id": GUS.pubkey}).json()["team_id"]
        _own(r, "w2", VALEN.pubkey)  # nunca arrancó: no hay worker:status:w2

        api.post(f"/api/teams/{team_id}/join", json={"worker_id": "w2"},
                 headers={"X-Owner-Id": VALEN.pubkey})

        desired = json.loads(r.get("worker:desired_mode:w2"))
        assert desired == {"mode": "pool-worker", "pool_url": "http://10.0.0.5:9001"}

    def test_la_intencion_no_caduca(self, api, r):
        """A diferencia del estado (TTL 15 s), la intención no expira.

        Si expirara, un minero apagado más de 15 s perdería su equipo.
        """
        _own(r, "w1", GUS.pubkey)
        _online(r, "w1")
        api.post("/api/teams", json={"name": "T", "worker_id": "w1"},
                 headers={"X-Owner-Id": GUS.pubkey})
        assert r.ttl("worker:desired_mode:w1") == -1  # -1 = sin expiración

    def test_volver_a_competitivo_tambien_se_persiste(self, api, r):
        _own(r, "coord", GUS.pubkey)
        _online(r, "coord", mode="pool-coordinator")
        team_id = api.post("/api/teams", json={"name": "T", "worker_id": "coord"},
                           headers={"X-Owner-Id": GUS.pubkey}).json()["team_id"]
        _own(r, "w2", VALEN.pubkey)
        _online(r, "w2")
        api.post(f"/api/teams/{team_id}/join", json={"worker_id": "w2"},
                 headers={"X-Owner-Id": VALEN.pubkey})

        api.post("/api/workers/w2/switch-mode", json={"target": "standalone"},
                 headers={"X-Owner-Id": VALEN.pubkey})
        assert json.loads(r.get("worker:desired_mode:w2"))["mode"] == "standalone"

    def test_cuando_el_coordinador_aparece_se_corrige_la_url_de_los_miembros(self, api, r):
        """El caso real: se arma el equipo con todo apagado y después se enciende.

        Al fundar el equipo no se conoce la dirección del coordinador, así que se
        usa una derivada del id. Cuando el coordinador arranca y publica la suya
        —que en Kubernetes es la IP del pod, imposible de adivinar— hay que
        corregir la de los que ya estaban adentro, o se quedan pidiéndole
        fragmentos a un host que no existe.
        """
        _own(r, "coord", GUS.pubkey)
        team_id = api.post("/api/teams", json={"name": "T", "worker_id": "coord"},
                           headers={"X-Owner-Id": GUS.pubkey}).json()["team_id"]
        _own(r, "w2", VALEN.pubkey)
        api.post(f"/api/teams/{team_id}/join", json={"worker_id": "w2"},
                 headers={"X-Owner-Id": VALEN.pubkey})
        assert json.loads(r.get("worker:desired_mode:w2"))["pool_url"] == \
            "http://coord:9001"

        # el coordinador arranca y publica su dirección real
        _online(r, "coord", mode="pool-coordinator", address="http://10.42.0.7:9001")
        api.get("/api/teams")  # cualquier lectura reconcilia

        assert json.loads(r.get("worker:desired_mode:w2"))["pool_url"] == \
            "http://10.42.0.7:9001"

    def test_dar_de_baja_un_minero_olvida_su_intencion(self, api, r):
        """Sin esto, reusar el id lo haría arrancar en el equipo del que salió."""
        from voxchain_api.services.worker_control import clear_desired_mode
        r.set("worker:desired_mode:w9", json.dumps({"mode": "pool-worker",
                                                    "pool_url": "http://x:9001"}))
        clear_desired_mode(r, "w9")
        assert r.get("worker:desired_mode:w9") is None


class TestElAltaDiceLaVerdad:
    def test_sin_kubernetes_el_alta_no_despliega_nada(self, api, r, monkeypatch):
        """Registrar anota el minero; encenderlo es otra cosa.

        En el compose local no hay Kubernetes, así que el alta es sólo metadata.
        La UI usa este campo para no prometer un contenedor que nadie va a crear
        y decirle al usuario cómo levantarlo.
        """
        from voxchain_api.routers import workers as workers_router
        monkeypatch.setattr(workers_router, "K8S_ENABLED", False)

        resp = workers_router.persist_worker_registration(
            _registro_firmado("minero-nuevo"), r)
        assert resp["deployed"] is False
        assert r.sismember("registered_workers", "minero-nuevo")


class TestUnMineroPorIdentidad:
    """Registrar es un derecho por identidad, no por minero (misma regla que fundar).

    Un minero es poder de cómputo; dejar que una sola clave acumule varios agrava
    gratis la concentración de poder que el sistema documenta como su debilidad
    (AGENT.md 3.9/9). Con Sybil sigue siendo evadible —generar otra identidad no
    cuesta nada— pero acá no se pretende cerrar ese agujero, sólo no ensancharlo.
    """

    @pytest.fixture(autouse=True)
    def _alta_autenticada(self, monkeypatch):
        """La firma y la frescura del timestamp se verifican ANTES del cupo.

        Es el orden correcto —no se actúa sobre una identidad no autenticada— y
        significa que estos tests, que ejercitan la regla de negocio, tienen que
        pasar primero esa puerta.
        """
        monkeypatch.setattr("voxchain_api.routers.workers.verify", lambda *a: True)
        monkeypatch.setattr(
            "voxchain_api.routers.workers._verify_timestamp_freshness", lambda *a: None)

    def test_el_segundo_alta_es_409(self, api, r):
        _own(r, "mi-minero", GUS.pubkey)

        resp = api.post("/api/workers/register", json={
            "worker_id": "otro-minero", "pubkey": GUS.pubkey,
            "timestamp": "2026-01-01T00:00:00+00:00", "signature": "x",
        })
        assert resp.status_code == 409
        assert "mi-minero" in resp.json()["detail"]
        assert not r.sismember("registered_workers", "otro-minero")

    def test_otra_identidad_si_puede_registrar(self, api, r):
        _own(r, "de-gus", GUS.pubkey)

        resp = api.post("/api/workers/register", json={
            "worker_id": "de-valen", "pubkey": VALEN.pubkey,
            "timestamp": "2026-01-01T00:00:00+00:00", "signature": "x",
        })
        assert resp.status_code == 200, resp.text
        assert r.sismember("registered_workers", "de-valen")

    def test_reregistrar_el_mismo_id_sigue_valiendo(self, api, r):
        """Es el camino para volver a subir la clave privada o recrear el pod."""
        _own(r, "mi-minero", GUS.pubkey)

        resp = api.post("/api/workers/register", json={
            "worker_id": "mi-minero", "pubkey": GUS.pubkey,
            "timestamp": "2026-01-01T00:00:00+00:00", "signature": "x",
        })
        assert resp.status_code == 200, resp.text

    def test_dar_de_baja_libera_el_cupo(self, api, r):
        from voxchain_api.routers.workers import worker_of_owner

        _own(r, "mi-minero", GUS.pubkey)
        assert worker_of_owner(r, GUS.pubkey) == "mi-minero"

        # Lo que hace el endpoint de baja.
        r.srem("registered_workers", "mi-minero")
        r.delete("worker:owner:mi-minero")
        assert worker_of_owner(r, GUS.pubkey) is None

    def test_un_registro_a_medias_no_bloquea_para_siempre(self, r):
        """Un id en el set sin su `worker:owner:*` es estado roto, no un minero."""
        from voxchain_api.routers.workers import worker_of_owner

        r.sadd("registered_workers", "huerfano")
        assert worker_of_owner(r, GUS.pubkey) is None

    def test_fundar_equipo_con_minero_nuevo_respeta_el_cupo(self, api, r):
        """El alta que hace `create_team` pasa por la misma regla."""
        _own(r, "mi-minero", GUS.pubkey)
        _online(r, "mi-minero")

        resp = api.post("/api/teams", json={
            "name": "Segundo intento", "worker_id": "otro-minero",
            "new_worker": {"worker_id": "otro-minero", "pubkey": GUS.pubkey,
                           "timestamp": "2026-01-01T00:00:00+00:00", "signature": "x"},
        }, headers={"X-Owner-Id": GUS.pubkey})
        assert resp.status_code == 409
        assert api.get("/api/teams").json() == []


# --- Identidad de nodo separada de la del ciudadano (AGENT.md 3.1) -----------


def _pubkey_valida() -> str:
    """Una pubkey EC P-256 en SPKI DER base64, como la que genera un minero."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    return base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)).decode()


class TestLaPrivadaDelCiudadanoNoViaja:
    """El alta de un minero no transporta ninguna clave privada de individuo.

    Antes sí lo hacía: el frontend armaba el PEM del ciudadano y el backend lo
    escribía en un Secret de Kubernetes, con lo cual cualquiera con acceso al
    clúster podía proponer y votar como esa persona indefinidamente. Ahora el
    minero genera su identidad adentro de su propio proceso y sólo reclama el
    vínculo con un token de un solo uso.
    """

    def test_el_alta_no_acepta_una_clave_privada(self):
        from voxchain_api.models import RegisterWorkerRequest

        assert "private_key" not in RegisterWorkerRequest.model_fields

    def test_el_alta_sin_despliegue_entrega_el_token_de_enrolamiento(self, r, monkeypatch):
        from voxchain_api.routers import workers as workers_router
        monkeypatch.setattr(workers_router, "K8S_ENABLED", False)

        resp = workers_router.persist_worker_registration(
            _registro_firmado("minero-token"), r)
        assert resp["deployed"] is False
        assert resp["enrollment_token"]
        # En Redis queda el hash, no el token: leer la base no da nada usable.
        assert r.get("worker:enroll:minero-token") != resp["enrollment_token"]


class TestEnrolamientoDeNodo:
    @pytest.fixture
    def alta(self, r, monkeypatch):
        from voxchain_api.routers import workers as workers_router
        monkeypatch.setattr(workers_router, "K8S_ENABLED", False)
        yo = _Identidad()
        resp = workers_router.persist_worker_registration(yo.registro("minero-enrol"), r)
        return {"worker_id": "minero-enrol", "owner": yo.pubkey, "yo": yo,
                "token": resp["enrollment_token"]}

    def test_vincula_el_nodo_con_su_dueno(self, api, r, alta):
        node = _pubkey_valida()
        resp = api.post("/api/workers/enroll", json={
            "worker_id": alta["worker_id"], "node_pubkey": node,
            "enrollment_token": alta["token"]})
        assert resp.status_code == 200
        assert r.get(f"node:owner:{node}") == alta["owner"]
        assert r.get(f"worker:node_pubkey:{alta['worker_id']}") == node

    def test_el_token_sirve_una_sola_vez(self, api, r, alta):
        primera = api.post("/api/workers/enroll", json={
            "worker_id": alta["worker_id"], "node_pubkey": _pubkey_valida(),
            "enrollment_token": alta["token"]})
        assert primera.status_code == 200

        impostor = _pubkey_valida()
        segunda = api.post("/api/workers/enroll", json={
            "worker_id": alta["worker_id"], "node_pubkey": impostor,
            "enrollment_token": alta["token"]})
        assert segunda.status_code == 401
        assert r.get(f"node:owner:{impostor}") is None

    def test_sin_token_valido_no_se_puede_reclamar_un_minero_ajeno(self, api, r, alta):
        impostor = _pubkey_valida()
        resp = api.post("/api/workers/enroll", json={
            "worker_id": alta["worker_id"], "node_pubkey": impostor,
            "enrollment_token": "token-inventado"})
        assert resp.status_code == 401
        assert r.get(f"node:owner:{impostor}") is None

    def test_una_pubkey_que_no_es_p256_se_rechaza(self, api, alta):
        """Vincular basura dejaría un nodo cuya firma el NCT nunca podría verificar."""
        resp = api.post("/api/workers/enroll", json={
            "worker_id": alta["worker_id"], "node_pubkey": "no-es-una-clave",
            "enrollment_token": alta["token"]})
        assert resp.status_code == 400

    def test_una_pubkey_invalida_no_quema_el_token(self, api, r, alta):
        """Un pedido que no puede prosperar no debe gastar el token.

        Al revés —consumir y después validar— un request malformado dejaba al
        minero sin poder enrolarse hasta que su dueño lo re-registrara. Lo
        encontró la prueba end-to-end, no los tests unitarios: acá cada caso
        estrenaba token y nunca reusaba el mismo dos veces.
        """
        api.post("/api/workers/enroll", json={
            "worker_id": alta["worker_id"], "node_pubkey": "no-es-una-clave",
            "enrollment_token": alta["token"]})

        node = _pubkey_valida()
        segunda = api.post("/api/workers/enroll", json={
            "worker_id": alta["worker_id"], "node_pubkey": node,
            "enrollment_token": alta["token"]})
        assert segunda.status_code == 200
        assert r.get(f"node:owner:{node}") == alta["owner"]

    def test_no_se_enrola_un_minero_que_no_existe(self, api, alta):
        resp = api.post("/api/workers/enroll", json={
            "worker_id": "minero-fantasma", "node_pubkey": _pubkey_valida(),
            "enrollment_token": alta["token"]})
        assert resp.status_code == 404

    def test_re_registrar_desvincula_el_nodo_anterior(self, api, r, alta, monkeypatch):
        """El pod viejo se reemplaza y su clave se va con él.

        Dejar el vínculo colgado le imputaría al dueño un nodo que ya no controla.
        """
        from voxchain_api.routers import workers as workers_router
        monkeypatch.setattr(workers_router, "K8S_ENABLED", False)

        viejo = _pubkey_valida()
        api.post("/api/workers/enroll", json={
            "worker_id": alta["worker_id"], "node_pubkey": viejo,
            "enrollment_token": alta["token"]})
        assert r.get(f"node:owner:{viejo}")

        workers_router.persist_worker_registration(
            alta["yo"].registro(alta["worker_id"]), r)
        assert r.get(f"node:owner:{viejo}") is None
        assert r.get(f"worker:node_pubkey:{alta['worker_id']}") is None

    def test_la_baja_borra_el_vinculo_y_el_token(self, api, r, alta):
        from datetime import datetime, timezone

        node = _pubkey_valida()
        api.post("/api/workers/enroll", json={
            "worker_id": alta["worker_id"], "node_pubkey": node,
            "enrollment_token": alta["token"]})

        wid = alta["worker_id"]
        ts = datetime.now(timezone.utc).isoformat()
        resp = api.delete(f"/api/workers/{wid}", headers={
            "X-Timestamp": ts, "X-Signature": alta["yo"].firma(f"{wid}|delete|{ts}")})
        assert resp.status_code == 200
        assert r.get(f"node:owner:{node}") is None
        assert r.get(f"worker:enroll:{wid}") is None


class TestElVinculoLoLeeElNct:
    """El índice que escribe la API es el mismo que lee el NCT.

    Los dos lados viven en módulos distintos (`routers/workers.py` escribe,
    `VoxChainStore.owner_of_node` lee) y sólo se encuentran por el nombre de la
    clave de Redis. Un desacuerdo ahí no rompe ningún test de cada lado: la regla
    3.4 simplemente dejaría de aplicarse, en silencio. Este test los enfrenta
    contra el mismo Redis.
    """

    def test_el_nct_resuelve_nodo_a_dueno_con_lo_que_escribio_la_api(self, api, r, monkeypatch):
        from common.storage import VoxChainStore
        from voxchain_api.routers import workers as workers_router

        monkeypatch.setattr(workers_router, "K8S_ENABLED", False)
        yo = _Identidad()
        alta = workers_router.persist_worker_registration(yo.registro("mi-minero"), r)

        node = _pubkey_valida()
        assert api.post("/api/workers/enroll", json={
            "worker_id": "mi-minero", "node_pubkey": node,
            "enrollment_token": alta["enrollment_token"]}).status_code == 200

        store = VoxChainStore(r)
        assert store.owner_of_node(node) == yo.pubkey

    def test_un_nodo_desconocido_no_se_le_imputa_a_nadie(self, r):
        from common.storage import VoxChainStore

        assert VoxChainStore(r).owner_of_node(_pubkey_valida()) is None


class TestAdministrarExigeFirma:
    """Las acciones de administración se prueban, no se declaran.

    Se autorizaban comparando la cabecera ``X-Owner-Id`` contra el dueño
    guardado. La cabecera la elige quien llama y la pubkey es pública —está en
    cada bloque de la cadena—, así que alcanzaba con saber a quién imitar:
    cualquiera podía sacarle un minero de su equipo a otro, o reescribirle la
    agenda a su pool, con un curl.

    Estos tests usan el TestClient crudo, sin el firmante automático, porque lo
    que ejercen es justamente la ausencia de firma.
    """

    @pytest.fixture
    def crudo(self, api):
        return api._client

    @pytest.fixture
    def equipo(self, api, r):
        _own(r, "coord", GUS.pubkey)
        _online(r, "coord")
        _own(r, "w2", VALEN.pubkey)
        _online(r, "w2")
        team_id = api.post("/api/teams", json={"name": "Los Pibes", "worker_id": "coord"},
                           headers={"X-Owner-Id": GUS.pubkey}).json()["team_id"]
        api.post(f"/api/teams/{team_id}/join", json={"worker_id": "w2"},
                 headers={"X-Owner-Id": VALEN.pubkey})
        return team_id

    def test_sin_firma_no_se_saca_a_nadie_de_su_equipo(self, crudo, equipo):
        """El ataque original: conocer la pubkey de alguien alcanzaba."""
        resp = crudo.post("/api/teams/%s/leave" % equipo, json={"worker_id": "w2"},
                          headers={"X-Owner-Id": VALEN.pubkey})
        assert resp.status_code == 401

    def test_sin_firma_no_se_disuelve_un_equipo(self, crudo, equipo):
        resp = crudo.delete(f"/api/teams/{equipo}", headers={"X-Owner-Id": GUS.pubkey})
        assert resp.status_code == 401

    def test_sin_firma_no_se_cambia_la_agenda(self, crudo, equipo):
        resp = crudo.put(f"/api/teams/{equipo}/categories", json={"categories": ["salud"]},
                         headers={"X-Owner-Id": GUS.pubkey})
        assert resp.status_code == 401

    def test_sin_firma_no_se_cambia_el_modo_de_un_minero(self, crudo, r):
        _own(r, "mio", GUS.pubkey)
        resp = crudo.post("/api/workers/mio/switch-mode", json={"target": "standalone"},
                          headers={"X-Owner-Id": GUS.pubkey})
        assert resp.status_code == 401

    def test_sin_firma_no_se_reescribe_la_politica_de_un_pool(self, crudo, r):
        _own(r, "coord", GUS.pubkey)
        resp = crudo.post("/api/workers/pool/coord/policy",
                          json={"decision": "reject", "action": "derogacion"},
                          headers={"X-Owner-Id": GUS.pubkey})
        assert resp.status_code == 401

    def test_la_firma_de_otra_accion_no_sirve(self, crudo, r):
        """Firmar `leave-team` no autoriza un `switch-mode`.

        La acción va adentro del mensaje firmado justo para esto: si no
        estuviera, una firma capturada de la acción más inocua autorizaría la
        más destructiva.
        """
        _own(r, "mio", GUS.pubkey)
        ts = datetime.now(timezone.utc).isoformat()
        resp = crudo.post("/api/workers/mio/switch-mode", json={"target": "standalone"},
                          headers={"X-Owner-Id": GUS.pubkey, "X-Timestamp": ts,
                                   "X-Signature": GUS.firma(f"mio|leave-team|{ts}")})
        assert resp.status_code == 401

    def test_la_firma_de_otro_minero_no_sirve(self, crudo, r):
        _own(r, "mio", GUS.pubkey)
        _own(r, "otro-mio", GUS.pubkey)
        ts = datetime.now(timezone.utc).isoformat()
        resp = crudo.post("/api/workers/mio/switch-mode", json={"target": "standalone"},
                          headers={"X-Owner-Id": GUS.pubkey, "X-Timestamp": ts,
                                   "X-Signature": GUS.firma(f"otro-mio|switch-mode|{ts}")})
        assert resp.status_code == 401

    def test_una_firma_vieja_no_sirve(self, crudo, r):
        from datetime import timedelta

        _own(r, "mio", GUS.pubkey)
        viejo = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        resp = crudo.post("/api/workers/mio/switch-mode", json={"target": "standalone"},
                          headers={"X-Owner-Id": GUS.pubkey, "X-Timestamp": viejo,
                                   "X-Signature": GUS.firma(f"mio|switch-mode|{viejo}")})
        assert resp.status_code == 400

    def test_una_firma_no_se_usa_dos_veces(self, api, crudo, r, equipo):
        """Dentro de la ventana de frescura la firma seguiría siendo válida.

        Quien la vio pasar podría repetir la acción — volver a sacar del equipo
        al minero que su dueño acaba de meter. Por eso se consume.
        """
        ts = datetime.now(timezone.utc).isoformat()
        headers = {"X-Owner-Id": VALEN.pubkey, "X-Timestamp": ts,
                   "X-Signature": VALEN.firma(f"w2|leave-team|{ts}")}

        primera = crudo.post(f"/api/teams/{equipo}/leave", json={"worker_id": "w2"},
                             headers=headers)
        assert primera.status_code == 200

        api.post(f"/api/teams/{equipo}/join", json={"worker_id": "w2"},
                 headers={"X-Owner-Id": VALEN.pubkey})
        repetida = crudo.post(f"/api/teams/{equipo}/leave", json={"worker_id": "w2"},
                              headers=headers)
        assert repetida.status_code == 401
        assert "utilizada" in repetida.json()["detail"]


class TestLasCuentasDemoSiguenAndando:
    """El camino custodial no se rompe al exigir firmas.

    Una cuenta demo no puede firmar desde el navegador —su clave la tiene el
    backend— así que se autoriza como antes. Que siga funcionando es parte del
    contrato: si al cerrar el agujero la demo dejaba de andar, el arreglo no
    servía.
    """

    def test_una_cuenta_demo_administra_su_minero_sin_firmar(self, api, r):
        crudo = api._client
        _online(r, "worker-standalone")
        resp = crudo.post("/api/workers/worker-standalone/switch-mode",
                          json={"target": "standalone"},
                          headers={"X-Owner-Id": "valentin"})
        assert resp.status_code == 200, resp.text

    def test_una_cuenta_demo_no_administra_el_minero_de_otra(self, api, r):
        crudo = api._client
        resp = crudo.post("/api/workers/worker-pool-miner-1/switch-mode",
                          json={"target": "standalone"},
                          headers={"X-Owner-Id": "valentin"})
        assert resp.status_code == 403

    def test_un_id_demo_no_se_puede_registrar(self, r):
        """Lo que mantiene disjuntos los dos caminos de autorización.

        Si un ciudadano real pudiera registrar `worker-standalone`, su minero
        caería en el camino custodial y quedaría administrable sin firma.
        """
        from fastapi import HTTPException
        from voxchain_api.routers import workers as workers_router

        with pytest.raises(HTTPException) as exc:
            workers_router.persist_worker_registration(
                _Identidad().registro("worker-standalone"), r)
        assert exc.value.status_code == 409
