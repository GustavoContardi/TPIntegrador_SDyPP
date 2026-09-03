"""Equipos de minado: almacenamiento y flujo extremo a extremo por la API.

Lo que estos tests protegen es la invariante del diseño: **el estado del equipo
y el modo real del worker se mueven juntos**. Cada alta o baja tiene que
despachar el ``switch_mode`` correspondiente, y no tiene que existir ningún
camino que cambie uno sin el otro — si lo hubiera, la lista de miembros
mostraría gente que ya no está minando para el equipo.
"""

from __future__ import annotations

import json

import fakeredis
import pytest

from voxchain_api.services.teams_store import TeamError, TeamsStore, slugify_team


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
        team = teams.create_team(name="Los Pibes", owner="pk-gus",
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
        return teams.create_team(name="Los Pibes", owner="pk-gus",
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
        tid = teams.create_team(name="Los Pibes", owner="pk-gus",
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
    yield client
    app.dependency_overrides.clear()


def _own(r, worker_id, owner):
    """Marca a ``owner`` como dueño de ``worker_id`` (lo que hace el registro)."""
    r.sadd("registered_workers", worker_id)
    r.set(f"worker:owner:{worker_id}", owner)
    r.set(f"worker:pubkey:{worker_id}", owner)


def _commands_for(client, worker_id):
    return [cmd for wid, cmd in client.publisher.messaging.commands if wid == worker_id]


class TestFlujoApi:
    def test_crear_equipo_promueve_el_worker_a_coordinador(self, api, r):
        _own(r, "mi-minero", "pk-gus")
        _online(r, "mi-minero")

        resp = api.post("/api/teams", json={"name": "Los Pibes",
                                            "worker_id": "mi-minero"},
                        headers={"X-Owner-Id": "pk-gus"})
        assert resp.status_code == 200, resp.text
        team = resp.json()
        assert team["name"] == "Los Pibes"
        assert team["coordinator_worker_id"] == "mi-minero"
        assert team["coordinator_online"] is True

        assert _commands_for(api, "mi-minero") == [
            {"type": "switch_mode", "mode": "pool-coordinator", "pool_url": ""}
        ]

    def test_crear_equipo_con_worker_ajeno_es_403(self, api, r):
        _own(r, "minero-de-otro", "pk-otro")
        resp = api.post("/api/teams", json={"name": "Robo", "worker_id": "minero-de-otro"},
                        headers={"X-Owner-Id": "pk-gus"})
        assert resp.status_code == 403

    def test_crear_equipo_sin_nombre_es_400(self, api, r):
        _own(r, "w", "pk-gus")
        resp = api.post("/api/teams", json={"name": "   ", "worker_id": "w"},
                        headers={"X-Owner-Id": "pk-gus"})
        assert resp.status_code == 400

    def test_unirse_usa_la_direccion_que_publica_el_coordinador(self, api, r):
        _own(r, "coord", "pk-gus")
        _online(r, "coord", address="http://10.42.0.7:9001")
        team_id = api.post("/api/teams", json={"name": "Los Pibes", "worker_id": "coord"},
                           headers={"X-Owner-Id": "pk-gus"}).json()["team_id"]
        # el coordinador ya arrancó y reporta su modo real
        _online(r, "coord", mode="pool-coordinator", address="http://10.42.0.7:9001")

        _own(r, "otro-minero", "pk-valen")
        _online(r, "otro-minero")
        resp = api.post(f"/api/teams/{team_id}/join",
                        json={"worker_id": "otro-minero"},
                        headers={"X-Owner-Id": "pk-valen"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["member_count"] == 2

        # La URL del pool NO la escribió nadie a mano: salió del `address` que
        # el coordinador publica de sí mismo.
        assert _commands_for(api, "otro-minero") == [
            {"type": "switch_mode", "mode": "pool-worker",
             "pool_url": "http://10.42.0.7:9001"}
        ]

    def test_no_se_puede_unir_a_un_coordinador_caido(self, api, r):
        _own(r, "coord", "pk-gus")
        _online(r, "coord")
        team_id = api.post("/api/teams", json={"name": "Los Pibes", "worker_id": "coord"},
                           headers={"X-Owner-Id": "pk-gus"}).json()["team_id"]
        r.delete("worker:status:coord")  # el coordinador se cayó

        _own(r, "otro", "pk-valen")
        resp = api.post(f"/api/teams/{team_id}/join", json={"worker_id": "otro"},
                        headers={"X-Owner-Id": "pk-valen"})
        assert resp.status_code == 409
        # y sobre todo: no se lo mandó a minar contra un HTTP muerto
        assert _commands_for(api, "otro") == []

    def test_salir_del_equipo_vuelve_a_competitivo(self, api, r):
        _own(r, "coord", "pk-gus")
        _online(r, "coord", mode="pool-coordinator")
        team_id = api.post("/api/teams", json={"name": "T", "worker_id": "coord"},
                           headers={"X-Owner-Id": "pk-gus"}).json()["team_id"]
        _own(r, "w2", "pk-valen")
        _online(r, "w2")
        api.post(f"/api/teams/{team_id}/join", json={"worker_id": "w2"},
                 headers={"X-Owner-Id": "pk-valen"})

        resp = api.post(f"/api/teams/{team_id}/leave", json={"worker_id": "w2"},
                        headers={"X-Owner-Id": "pk-valen"})
        assert resp.status_code == 200
        assert _commands_for(api, "w2")[-1]["mode"] == "standalone"
        assert api.get(f"/api/teams/{team_id}").json()["member_count"] == 1

    def test_disolver_libera_a_todo_el_plantel(self, api, r):
        _own(r, "coord", "pk-gus")
        _online(r, "coord", mode="pool-coordinator")
        team_id = api.post("/api/teams", json={"name": "T", "worker_id": "coord"},
                           headers={"X-Owner-Id": "pk-gus"}).json()["team_id"]
        _own(r, "w2", "pk-valen")
        _online(r, "w2")
        api.post(f"/api/teams/{team_id}/join", json={"worker_id": "w2"},
                 headers={"X-Owner-Id": "pk-valen"})

        resp = api.delete(f"/api/teams/{team_id}", headers={"X-Owner-Id": "pk-gus"})
        assert resp.status_code == 200
        assert set(resp.json()["released"]) == {"coord", "w2"}
        # Nadie queda apuntando a un coordinador que ya no reparte trabajo.
        assert _commands_for(api, "coord")[-1]["mode"] == "standalone"
        assert _commands_for(api, "w2")[-1]["mode"] == "standalone"
        assert api.get(f"/api/teams/{team_id}").status_code == 404

    def test_solo_el_creador_disuelve(self, api, r):
        _own(r, "coord", "pk-gus")
        _online(r, "coord")
        team_id = api.post("/api/teams", json={"name": "T", "worker_id": "coord"},
                           headers={"X-Owner-Id": "pk-gus"}).json()["team_id"]
        resp = api.delete(f"/api/teams/{team_id}", headers={"X-Owner-Id": "pk-intruso"})
        assert resp.status_code == 403


class TestSwitchModeEsSoloVueltaACompetitivo:
    """El modo cooperativo se entra por equipos y por ningún otro lado."""

    def test_pedir_pool_worker_a_mano_es_400(self, api, r):
        _own(r, "w1", "pk-gus")
        resp = api.post("/api/workers/w1/switch-mode",
                        json={"target": "pool-worker", "pool_url": "http://loquesea:9001"},
                        headers={"X-Owner-Id": "pk-gus"})
        assert resp.status_code == 400
        assert "equipo" in resp.json()["detail"].lower()
        assert _commands_for(api, "w1") == []

    def test_pedir_pool_coordinator_a_mano_es_400(self, api, r):
        _own(r, "w1", "pk-gus")
        resp = api.post("/api/workers/w1/switch-mode", json={"target": "pool-coordinator"},
                        headers={"X-Owner-Id": "pk-gus"})
        assert resp.status_code == 400

    def test_volver_a_competitivo_saca_del_equipo(self, api, r):
        _own(r, "coord", "pk-gus")
        _online(r, "coord", mode="pool-coordinator")
        team_id = api.post("/api/teams", json={"name": "T", "worker_id": "coord"},
                           headers={"X-Owner-Id": "pk-gus"}).json()["team_id"]
        _own(r, "w2", "pk-valen")
        _online(r, "w2")
        api.post(f"/api/teams/{team_id}/join", json={"worker_id": "w2"},
                 headers={"X-Owner-Id": "pk-valen"})

        resp = api.post("/api/workers/w2/switch-mode", json={"target": "standalone"},
                        headers={"X-Owner-Id": "pk-valen"})
        assert resp.status_code == 200
        assert resp.json()["left_team"] == team_id
        # el equipo se enteró: ya no lo cuenta como miembro
        assert api.get(f"/api/teams/{team_id}").json()["member_count"] == 1

    def test_el_coordinador_no_sale_por_switch_mode(self, api, r):
        _own(r, "coord", "pk-gus")
        _online(r, "coord", mode="pool-coordinator")
        api.post("/api/teams", json={"name": "T", "worker_id": "coord"},
                 headers={"X-Owner-Id": "pk-gus"})
        resp = api.post("/api/workers/coord/switch-mode", json={"target": "standalone"},
                        headers={"X-Owner-Id": "pk-gus"})
        assert resp.status_code == 409
        assert "disolv" in resp.json()["detail"].lower()


class TestListadoDeMineros:
    def test_el_listado_dice_a_que_equipo_pertenece_cada_minero(self, api, r):
        _own(r, "coord", "pk-gus")
        _online(r, "coord", mode="pool-coordinator")
        _own(r, "w2", "pk-valen")
        _online(r, "w2")
        _own(r, "solo", "pk-otro")
        _online(r, "solo")

        team_id = api.post("/api/teams", json={"name": "Los Pibes", "worker_id": "coord"},
                           headers={"X-Owner-Id": "pk-gus"}).json()["team_id"]
        api.post(f"/api/teams/{team_id}/join", json={"worker_id": "w2"},
                 headers={"X-Owner-Id": "pk-valen"})

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
        _own(r, "recien-creado", "pk-gus")

        resp = api.post("/api/teams", json={"name": "Nuevo", "worker_id": "recien-creado"},
                        headers={"X-Owner-Id": "pk-gus"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["coordinator_online"] is False

        # y por lo tanto nadie puede unirse todavía
        _own(r, "otro", "pk-valen")
        team_id = resp.json()["team_id"]
        assert api.post(f"/api/teams/{team_id}/join", json={"worker_id": "otro"},
                        headers={"X-Owner-Id": "pk-valen"}).status_code == 409

    def test_la_orden_se_manda_igual(self, api, r):
        """No saber si arrancó no es razón para no mandarle la orden.

        Cuando el pod levante va a consumir su `worker.command` y arrancar ya en
        modo coordinador.
        """
        _own(r, "recien-creado", "pk-gus")
        api.post("/api/teams", json={"name": "Nuevo", "worker_id": "recien-creado"},
                 headers={"X-Owner-Id": "pk-gus"})
        assert _commands_for(api, "recien-creado") == [
            {"type": "switch_mode", "mode": "pool-coordinator", "pool_url": ""}
        ]

    def test_el_estado_reportado_si_se_refina(self, api, r):
        """Con estado previo, la UI ve el modo nuevo sin esperar al worker."""
        _own(r, "vivo", "pk-gus")
        _online(r, "vivo", address="http://10.42.0.9:9001")
        api.post("/api/teams", json={"name": "T", "worker_id": "vivo"},
                 headers={"X-Owner-Id": "pk-gus"})
        by_id = {w["worker_id"]: w for w in api.get("/api/workers/status").json()}
        assert by_id["vivo"]["mode"] == "pool-coordinator"
        # y la dirección que el worker había publicado sobrevive al refinamiento
        assert by_id["vivo"]["address"] == "http://10.42.0.9:9001"


class TestUnSoloEquipoPorIdentidad:
    """Fundar equipo es un derecho por identidad, no por minero.

    Un equipo concentra poder de cómputo (AGENT.md 3.9). Dejar que una misma
    clave funde varios le daría a un solo individuo tantos frentes como quisiera,
    agravando gratis la concentración de poder que el sistema documenta como su
    debilidad. Participar en el equipo de otro con varios mineros sí se puede:
    lo que se limita es fundar.
    """

    def test_la_segunda_fundacion_es_409(self, api, r):
        _own(r, "w1", "pk-gus")
        _online(r, "w1")
        _own(r, "w2", "pk-gus")
        _online(r, "w2")

        assert api.post("/api/teams", json={"name": "Primero", "worker_id": "w1"},
                        headers={"X-Owner-Id": "pk-gus"}).status_code == 200

        resp = api.post("/api/teams", json={"name": "Segundo", "worker_id": "w2"},
                        headers={"X-Owner-Id": "pk-gus"})
        assert resp.status_code == 409
        assert "Primero" in resp.json()["detail"]
        # y el minero libre no quedó promovido a coordinador de la nada
        assert _commands_for(api, "w2") == []

    def test_otra_identidad_si_puede_fundar(self, api, r):
        _own(r, "w1", "pk-gus")
        _online(r, "w1")
        _own(r, "w2", "pk-valen")
        _online(r, "w2")

        assert api.post("/api/teams", json={"name": "De Gus", "worker_id": "w1"},
                        headers={"X-Owner-Id": "pk-gus"}).status_code == 200
        assert api.post("/api/teams", json={"name": "De Valen", "worker_id": "w2"},
                        headers={"X-Owner-Id": "pk-valen"}).status_code == 200

    def test_disolver_libera_el_derecho_a_fundar(self, api, r):
        _own(r, "w1", "pk-gus")
        _online(r, "w1")
        team_id = api.post("/api/teams", json={"name": "Primero", "worker_id": "w1"},
                           headers={"X-Owner-Id": "pk-gus"}).json()["team_id"]

        api.delete(f"/api/teams/{team_id}", headers={"X-Owner-Id": "pk-gus"})

        assert api.post("/api/teams", json={"name": "Segundo", "worker_id": "w1"},
                        headers={"X-Owner-Id": "pk-gus"}).status_code == 200

    def test_sumar_varios_mineros_al_equipo_de_otro_sigue_permitido(self, api, r):
        _own(r, "coord", "pk-gus")
        _online(r, "coord", mode="pool-coordinator")
        team_id = api.post("/api/teams", json={"name": "Los Pibes", "worker_id": "coord"},
                           headers={"X-Owner-Id": "pk-gus"}).json()["team_id"]

        for worker_id in ("v1", "v2"):
            _own(r, worker_id, "pk-valen")
            _online(r, worker_id)
            assert api.post(f"/api/teams/{team_id}/join", json={"worker_id": worker_id},
                            headers={"X-Owner-Id": "pk-valen"}).status_code == 200

        assert api.get(f"/api/teams/{team_id}").json()["member_count"] == 3

    def test_el_indice_colgado_no_bloquea_para_siempre(self, teams, r):
        """Si el hash del equipo desapareció, la identidad vuelve a poder fundar."""
        teams.create_team(name="A", owner="pk", coordinator_worker_id="w1",
                          coordinator_url="u")
        r.delete(*[k for k in r.scan_iter("team:*") if not k.startswith("team:owner:")])
        assert teams.team_of_owner("pk") is None
