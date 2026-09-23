# Pilar 2 — Infraestructura de servicios distribuidos (VoxChain Reborn)

VoxChain Reborn no es una blockchain de dinero: es **gobierno por consenso de esfuerzo
computacional**: se promulgan y derogan **leyes**, y el consenso se mide en
hashes (Proof of Work), no en votos nominales. Propone quien responde por
cómputo en la red (el fundador de un equipo o el dueño de un minero standalone).
El NCT sólo coordina ventanas de votación, no arbitra contenido.

> La fuente de verdad del dominio es [`AGENT.md`](../AGENT.md) (raíz del repo).
> Las reglas de gobierno de su sección 3 son normativas.

El desafío que se hashea es el **desafío de gobierno serializado**:

```
partial_hash_base = law_id + text_hash + voting_window_id + action
nonce válido ⇔ md5(partial_hash_base + str(nonce)) empieza con n ceros
            (n para promulgación, n+1 para derogación)
```

---

## Arquitectura

```
   nodo/ciudadano                         ┌──────────────────────────────┐
   (author_pubkey)                        │            Redis             │
        │ scripts/propose_law.py          │  law:* window:* block:*      │
        │ (flujo 1: propuestas)           │  chain  active_window        │
        ▼                                 │  cooldown:* window_counter   │
 ┌───────────────┐   propuestas (cola)    │  leader lease (NCT y pool)   │
 │   RabbitMQ    │◄───────────────────────└───────────────▲──────────────┘
 │               │                                │       │ persiste estado
 │ exchanges     │   desafio_activo (topic)       │       │ y sella bloques
 │ + colas       │────────────┐        ┌──────────┴───────────────┐
 └───────┬───────┘            │        │       NCT primary        │
         │                    │        │  - cola round-robin autor│
         │ respuesta_nonce    │        │  - abre/cierra ventana   │
         │ (cola) red→NCT     │        │  - dificultad n/n+1      │
         │                    │        │  - verifica nonce, sella │
         │                    │        │  - publica heartbeats    │
         │                    │        └──────────────────────────┘
         │                    │                     ▲ nct.heartbeat
         │                    │        ┌────────────┴─────────────┐
         │                    │        │       NCT standby        │
         │                    │        │  - monitorea heartbeats  │
         │                    │        │  - toma el lease en Redis│
         │                    │        │    si el líder calla     │
         │                    ▼        └──────────────────────────┘
         │          ┌──────────────────┐
         │          │ Pool Coordinator │  worker en modo pool-coordinator
         │          │  - fragmenta el  │  (suscrito a desafio_activo)
         │          │    espacio nonce │
         │          │  - auto-mina     │
         │          └───────┬──────────┘
         │       HTTP       │   ▲ registro + heartbeat
         │  /work/next/<id> │   │
         │                  ▼   │
         │            ┌──────────────────┐   invoca minero Pilar 1
         └───────────►│  pool-workers    │──► GPU 05_brute_force_range
            nonce     │  - minan rango   │    └ fallback CPU brute_force.py
            ganador   └──────────────────┘
```

> El reparto de fragmentos va por **HTTP**, no por cola: el coordinator necesita
> saber qué minero tiene cada rango para reasignarlo si deja de reportar.

### Flujos de RabbitMQ

**Tres flujos canónicos hacia/desde el NCT** (AGENT.md 5 / P2; no se agrega un
cuarto que toque al NCT):

| # | Nombre            | Tipo            | Dirección   | Contenido |
|---|-------------------|-----------------|-------------|-----------|
| 1 | `propuestas`      | cola            | nodo → NCT  | `law_id, author_pubkey, text_hash, created_at, action, category` |
| 2 | `desafio_activo`  | exchange *topic*| NCT → red   | `voting_window_id, law_id, n_zeros_required, deadline, partial_hash_base, action, category, participants` |
| 3 | `respuesta_nonce` | cola            | red → NCT   | `voting_window_id, nonce, winning_node_or_pool, block_hash_candidato` |

**Deliberación** (AGENT.md 3.12): antes de publicar el desafío, el NCT anuncia
la ley en Redis (`nct:deliberation`, sólo la ley y su área) y espera
`DELIBERATION_SECONDS` a que los convocados respondan por el API
(`POST /api/deliberation/<law_id>/decision`, firmado). La dificultad y el plazo
se congelan en el anuncio sobre el convocado más grande del área. El desafío
sale después con `participants`: sólo minan los que aceptaron. No agrega un
flujo de RabbitMQ: el anuncio y las respuestas viajan por Redis y el API.

**Failover del NCT** (AGENT.md 4):

| # | Nombre          | Tipo             | Dirección            | Contenido |
|---|-----------------|------------------|----------------------|-----------|
| 4 | `nct.heartbeat` | exchange *topic* | NCT activo → backups | `nct_id, ts, active_window_id, last_block_hash` |

No hay cola de elección: el arbitraje entre standbys lo hace **Redis
atómicamente** con un lease. Los NCT son homogéneos dentro del mismo clúster, sin
ventaja de cómputo entre ellos, así que una prueba de trabajo entre pares no
aportaría nada. Ver `nct/monitor.py`.

**Coordinación del pool de minado:**

| # | Nombre           | Tipo             | Dirección          | Contenido |
|---|------------------|------------------|--------------------|-----------|
| 5 | `pool.election`  | exchange *topic* | workers ↔ workers  | mini-PoW para elegir coordinator (`POOL_ELECTION_N_ZEROS`) |
| 6 | `worker.command` | exchange *topic* | backend → worker   | `switch_mode`, `stop` |

**Colas declaradas pero sin uso activo:** `tareas_trp` y `keepalive_trp` son
restos del diseño original con un servicio TrP separado. `tareas_trp` no tiene
publicadores ni consumidores; `keepalive_trp` sólo tiene publicador. Se declaran
en la topología por compatibilidad, pero el reparto de trabajo real va por HTTP.

---

## Componentes

| Servicio | Rol |
|---|---|
| [`nct-coordinator/`](nct-coordinator/) | NCT **primario**: cola round-robin, cooldown, apertura/cierre de ventana, verificación de nonce, sellado del bloque y publicación de heartbeats |
| [`nct-coordinator/`](nct-coordinator/) (standby) | NCT **standby**: monitorea heartbeats del líder; si deja de recibirlos, toma el lease de liderazgo en Redis |
| [`worker/`](worker/) modo `pool-coordinator` | Fragmenta el espacio de nonces, reparte tareas por HTTP, trackea capacidad por keep-alives y además auto-mina |
| [`worker/`](worker/) modo `pool-worker` | Pide rangos al coordinator y los mina invocando el minero de Pilar 1 (GPU/CPU) |
| [`worker/`](worker/) modo `standalone` | Mina el espacio completo por su cuenta y publica el nonce directo al NCT (modo competitivo) |
| [`voxchain_api/routers/teams.py`](voxchain_api/routers/teams.py) | **Equipos**: capa de nombres sobre el modo cooperativo. Crear un equipo promueve un minero propio a `pool-coordinator`; unirse pone un minero en `pool-worker` apuntando a él |
| [`worker/`](worker/) modo `pool-auto` | Pool de infraestructura anónimo: los pares eligen coordinator por mini-PoW (`pool.election`) |
| [`voxchain_api/`](voxchain_api/) | API REST (FastAPI): propuestas, cadena, estado de workers, equipos, deliberación y disponibilidad |
| [`voxchain-frontend/`](voxchain-frontend/) | SPA en Angular; firma en el navegador las propuestas y las decisiones de deliberación |
| `common/` | Paquete compartido: `blockchain`, `storage` (Redis), `messaging` (RabbitMQ), health, logging, métricas, config |

> El diseño original tenía un servicio `transaction-pool/` separado (TrP). Su rol
> quedó absorbido por el worker en modo `pool-coordinator`, que hace lo mismo sin
> un despliegue adicional.

---

## Ejecución local

```bash
cd pilar2-distribuido
# RabbitMQ + Redis + NCT primary + NCT standby + 2 workers standalone
# + 1 pool-coordinator + API + frontend
docker compose up --build

# en otra terminal: proponer una ley (flujo 1)
docker compose run --rm coordinator \
  python /app/scripts/propose_law.py \
  --text "Presupuesto participativo 2026" --category economia --author pk-ciudadano-1
```

El sistema, sin más intervención, abre la ventana, los workers resuelven el PoW
y el NCT sella el bloque en Redis con encadenamiento válido.

Para el **experimento de escalado** (N transacciones con M vs 2xM mineros) hay
un compose aparte, con los mineros en modo pool y un servicio escalable:

```bash
../pilar3-despliegue/load-tests/scenarios/run_scaling.sh --miners 1,2,4 --laws 10
```

Ver [`docker-compose.scale.yml`](docker-compose.scale.yml) y la sección 4 del
[informe](../docs/informe/INFORME.md).

### Health endpoints (JSON, sin GUI)

- API: <http://localhost:8000/api/health> → `{"api","nct","redis","rabbitmq","frontend","workers","clock"}`
- Disponibilidad para proponer:
  <http://localhost:8000/api/system/availability?category=salud&action=derogacion>
  → `{"available","live_workers","eligible_workers","queued_laws","message",…}`.
  Responde si hay mineros dispuestos a minar **esa ventana**; con
  `available:false` la ley se acepta igual pero **queda encolada** hasta que los
  haya (AGENT.md 3.11). `action` importa: un equipo puede votar el área y vetar
  toda derogación, así que la misma categoría puede estar disponible para
  promulgar e indisponible para derogar.
- NCT: <http://localhost:8081/health> → `{"nct":"ok","redis":"ok","rabbitmq":"ok"}`
- Pool coordinator: <http://localhost:9001/health> → incluye `miners` registrados
- Frontend: <http://localhost:4200>
- RabbitMQ management: <http://localhost:15672> (guest/guest, sólo dev local).

### Tests

Desde la **raíz del repo**:

```bash
./run.sh test          # arma el venv con todo lo necesario y corre la suite
```

```bash
./run.sh test -m integration   # sólo el flujo extremo a extremo
```

`run.sh` instala también las dependencias del API Gateway (`fastapi`,
`pydantic`, `httpx`). Sin ellas la suite pierde 63 tests, y no de forma
evidente: aparecen como errores de import, no como "falta una dependencia".

**Sobre los pines del API.** `pydantic==2.9.2` compila `pydantic-core` contra
PyO3, que no soporta Python posterior a 3.13. En un intérprete más nuevo el pin
no instala y `run.sh` cae a versiones sueltas, avisando: la suite corre igual,
pero no contra las mismas versiones que el contenedor. Para correrla contra las
exactas, usando la imagen del API (también desde la raíz del repo, con el
sistema ya levantado por lo menos una vez para que la imagen exista):

```bash
docker run --rm -v "$PWD:/repo" -w /repo/pilar2-distribuido --entrypoint sh voxchain-pilar2-voxchain-api:latest -c 'pip install -q pytest "fakeredis[lua]" && python -m pytest -q'
```

Se monta la raíz del repo y no `pilar2-distribuido`: los tests e2e usan el
minero CPU de Pilar 1, que vive fuera de este directorio.

---

## Decisiones de diseño

- **Núcleo agnóstico del transporte.** NCT, pool coordinator y worker reciben un `Messaging` y
  un `VoxChainStore`. En producción se inyecta RabbitMQ + Redis; en tests, un bus
  en memoria + `fakeredis`. El mismo código de dominio corre en ambos.
- **Una sola ventana activa** (AGENT.md 3.3): estado único `active_window` en
  Redis; el NCT no abre una nueva hasta cerrar la anterior.
- **No se abre ventana sin mineros que puedan minarla** (AGENT.md 3.11). Antes la
  ventana se abría con la red vacía, vencía, y la ley quedaba `discarded` con su
  texto anotado — así que reproponerla costaba el cooldown largo de reproposición
  idéntica, por una falla de infraestructura ajena al autor. Ahora la ley espera
  en la cola, el NCT publica el porqué en `nct:availability` y el cliente muestra
  *"sistema no disponible, tu ley será pospuesta"*. Una ley postergada **no
  bloquea la cola**: se saltea y el turno pasa a la siguiente con red.
- **Cierre atómico al primer nonce válido** (AGENT.md 5 / P2): el primer nonce
  válido **recibido** para una ventana cierra el sello mediante un guard atómico
  en Redis (`SET window_sealed:<voting_window_id> <winner> NX`). Toda solución
  válida posterior para la misma ventana ve el guard puesto y se descarta como
  tardía (no sobrescribe `winning_nonce`/`winning_node_or_pool`). El guard vive en
  Redis —no sólo en memoria del proceso— para ser autoritativo ante un failover
  (un NCT distinto retomando). El desempate es por orden de llegada, no por el
  valor del nonce.
- **Suscripción a colas de trabajo gateada por liderazgo** (AGENT.md 4): sólo el
  NCT líder consume `propuestas` y `respuesta_nonce`. Un standby es follower:
  escucha `nct.heartbeat`, pero **no** se suscribe a las colas de
  trabajo, porque RabbitMQ las reparte round-robin entre consumidores y un
  follower suscrito se quedaría con (y descartaría) la mitad de los mensajes. La
  promoción follower→líder (al ganar la elección) abre esos consumidores; la
  pérdida de liderazgo (`step_down`) los cierra.
- **Texto comprimido inline en la propuesta**: la propuesta viaja con
  `text_compressed` (gzip+base64) y `text_original_len` además de `text_hash`. El
  esquema 7.1 de AGENT prevé MinIO (`text_ref`) para el texto completo; acá el
  texto comprimido viaja **inline** en el mensaje para evitar la dependencia de
  MinIO en el camino crítico (propuesta→ventana→sellado). `text_hash` sigue siendo
  la **identidad canónica** de la ley (reproposición y encadenamiento se deciden
  por hash, no por el blob); MinIO queda como opción para textos grandes.
- **Orden round-robin por autor**, no FIFO (3.3): un autor no encadena turnos
  consecutivos si hay leyes de otros.
- **Cuota de turnos por identidad** (3.3): una identidad que se llevó más de la
  mitad de las últimas 10 ventanas cede el turno. Acota el monopolio sostenido,
  que el round-robin solo no frena (alcanza con alternar con un cómplice). **No
  cierra Sybil** —identidades gratis, AGENT.md 9— y la cola nunca se bloquea: si
  nadie cumple las reglas, entra igual la ley más antigua.
- **Dificultad n / n+1** (3.6, 11.3): la relación promulgar/derogar es
  invariante. `n` en cambio es **dinámico** con `DYNAMIC_DIFFICULTY=true`: el NCT
  lo recalcula al abrir cada ventana según el cómputo **vivo**, para que
  promulgar cueste siempre ~`DIFFICULTY_TARGET_SECONDS`. Mide cómputo declarado,
  no comportamiento (por eso no es el mecanismo gameable que descarta 11.2), sube
  en el acto y baja con histéresis (3 ventanas), con el estado del trinquete
  persistido en Redis para que sobreviva al failover del NCT. `NONCE_SPACE` se deriva de `n` y **viaja en
  el desafío**, porque mover uno sin el otro hace vencer las derogaciones en
  silencio. Con `false` vuelve al `n` fijo del enunciado original.
- **Registrar mineros no abarata leyes**: los standalone son redundantes entre sí
  (todos barren desde 0 el mismo rango), así que la red vale lo que su buscador
  independiente más rápido. Un equipo grande sí agrega, y ahí `n` sube.
- **Ante ausencia de GPUs, baja el prefijo.** La medición de la red pondera cada
  minero por su hashrate reportado, o por `HPS_GPU`/`HPS_CPU` según `has_gpu` si
  todavía no midió. Si se van las GPU, la red medida cae y `n` baja, de a un
  cero y con la histéresis de siempre. Sin `DYNAMIC_DIFFICULTY` no hay
  reducción: `n` queda fijo y las ventanas pueden vencer.
- **Ley pendiente → `discarded`** (3.2): si la ventana vence sin nonce, la ley se
  descarta y **no** se reencola; su `text_hash` queda marcado para detectar
  reproposición. **Excepción:** si al vencer la red está por debajo del quórum,
  la ley vuelve a la cola (`expired_no_quorum`) sin marcar el texto.
- **Deliberación antes de cada ventana** (3.12): el NCT anuncia la ley y espera
  `DELIBERATION_SECONDS` a que los equipos y standalone convocados decidan, con
  una respuesta firmada, si aportan cómputo. Sólo minan los que aceptaron.
  Dificultad y plazo se congelan al anunciar sobre el convocado más grande, para
  que su "no" encarezca la ley en vez de abaratarla. Si nadie acepta y alguien
  veta, la ley se descarta; si nadie responde, vuelve a la cola.
- **Quién propone** (3.2): sólo el fundador de un equipo o el dueño de un minero
  standalone (`RESTRICT_PROPOSERS`). Se verifica en el API (403 con el motivo) y
  otra vez en el NCT, porque a la cola `propuestas` se puede llegar sin pasar
  por el API. Regla en `common/blockchain/proposers.py`.
- **Categorías de ley y agenda de equipos** (3.10): toda ley declara un área de
  gobierno (`economia`, `salud`, …, `general` por defecto) que su autor **firma**
  junto con el resto de la propuesta, y que una derogación **hereda** de la ley
  original. Cada equipo declara la agenda de áreas que vota: si entra una ley de
  otra área, su coordinador no fragmenta el espacio de nonces y el equipo entero
  no aporta un solo hash. La categoría **no** entra en el `partial_hash_base`: el
  desafío que resuelve el minero de Pilar 1 no cambia. Una ley que no le
  interesa a ningún equipo **espera en la cola** por el quórum por área
  (`QUORUM_BY_CATEGORY=true`); con `false` vuelve a expirar como veto por
  abstención.
- **Reproposición por hash exacto del texto** (3.5): idéntica a una descartada →
  cooldown mayor (`reproposed_identical`); distinta → propuesta nueva. Misma `n`.
- **Sellado y encadenamiento**: `block_hash = sha256(contenido)`, cada bloque
  referencia el `block_hash` anterior; la cadena se valida de punta a punta
  (links + que el nonce satisface la dificultad declarada).
- **El minero no se reimplementa** (Pilar 1): el worker lo invoca como subproceso
  y cae a CPU si no hay GPU. El "puente" es: `n` ceros ⇒ prefijo de `n` caracteres
  `'0'`.
- **Seguridad**: cero secretos en el repo; URLs y credenciales por
  variables de entorno; las **claves privadas nunca** se persisten ni viajan por
  RabbitMQ (sólo `author_pubkey`). En el navegador, la clave del ciudadano es un
  `CryptoKey` no extraíble en IndexedDB (AGENT.md 3.1).
- **Tolerancia a fallos del NCT** (4): cada NCT que no es líder corre un
  `NCTHeartbeatMonitor`; si el líder deja de emitir durante `HEARTBEAT_TIMEOUT`,
  intenta tomar el lease de liderazgo en Redis. **El arbitraje es atómico en
  Redis, no una elección distribuida por PoW**: los NCT son homogéneos y sin
  ventaja de cómputo entre sí, así que gana quien detectó la caída antes. La
  ventana en curso se pierde por diseño (se prefiere descartarla antes que
  arriesgar un sellado doble). Cubierto por `nct-coordinator/tests/test_bully.py`
  y `nct-coordinator/tests/test_failover_y_cierre.py`.
- **El modo cooperativo se administra por equipos, no por URL**: para poner un
  minero en `pool-worker` hay que decirle la URL del coordinador, y esa URL un
  usuario no la puede averiguar (en Kubernetes los pods de un Deployment no
  tienen DNS estable; la buena es la IP del pod). Se invirtió quién la sabe: el
  **worker anuncia su dirección** (`WORKER_ADDRESS`/`MY_POD_IP`) en el estado que
  publica en Redis, y el backend se la entrega a quien se une al equipo. En
  consecuencia `POST /api/workers/{id}/switch-mode` **sólo acepta `standalone`**:
  si se pudiera cambiar el modo por un lado y la membresía por otro, la lista de
  miembros del equipo mentiría. Ver [`docs/workers.md`](../docs/workers.md).
- **Elección del coordinator del pool** (AGENT.md 4.2): en un **equipo** el
  coordinador lo designa su fundador, y la elección por lease en Redis sólo
  garantiza que haya uno vivo si el pod se reinicia. En el **pool de
  infraestructura** (`pool-auto`), donde no hay a quién designar, hay mini-PoW
  (`POOL_ELECTION_N_ZEROS`, 2 ceros por defecto) por el exchange
  `pool.election`: los candidatos son mineros y el criterio de esfuerzo es
  coherente con el resto del sistema. Con Redis, los dos se disputan el mismo
  lease `pool:leader:<pool_id>`.

## Limitaciones conocidas

- **Una ventana activa por vez**: el NCT sella de a una ley, así que hay un techo
  duro de throughput que no se corrige agregando mineros. Es la limitación más
  importante del diseño.
- **Colas clásicas durables, no *quorum queues***: RabbitMQ corre con 3 réplicas,
  pero cada cola vive en un solo nodo. Los mensajes sobreviven a un reinicio, pero
  si cae el nodo que hospeda la cola, esa cola queda indisponible.
- El pool coordinator es un punto único mientras vive (tiene failover por lease,
  pero reparte todo el trabajo desde un solo proceso).
- Sybil y concentración de poder en pools son vulnerabilidades **por diseño**
  documentado (AGENT.md 9), no bugs.
- **Una ley que ningún equipo quiere minar espera indefinidamente** con el
  default `QUORUM_BY_CATEGORY=true`: no muere, pero tampoco sale, y nada la
  caduca. Aplica igual al área y al veto de acción — una derogación que todos los
  equipos del área rechazan queda en la cola sin fecha. Con `false` vuelve a
  expirar como veto por abstención (AGENT.md 3.10/3.11).
