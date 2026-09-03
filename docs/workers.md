# Los workers de VoxChain Reborn: modo competitivo y modo cooperativo

> Documento de referencia técnica. Describe **cómo funcionan realmente los
> workers hoy en el código**, no cómo deberían funcionar. Donde el código y la
> documentación existente (README de Pilar 2, README del worker, INFORME) no
> coinciden, se marca explícitamente con ⚠️ y se explica la diferencia.
>
> Fuente: `pilar2-distribuido/worker/`, `pilar2-distribuido/common/messaging/`,
> `pilar3-despliegue/kubernetes/gpu-cluster/`.

---

## 0. Respuesta corta

**Sí, es el mismo código.** Hay **un solo binario** (`worker/main.py`), una sola
imagen Docker, un solo `Deployment`. Lo que cambia es una variable de entorno:
`WORKER_MODE`. Adentro del proceso, un `WorkerManager` arranca una clase
distinta según el modo y la corre en un thread.

Hay **cuatro** modos, no tres (el README del worker documenta tres):

| `WORKER_MODE` | Clase que corre | Cómo recibe trabajo | Modo del TP |
|---|---|---|---|
| `standalone` | `StandaloneWorker` | RabbitMQ (topic `desafio_activo`) | **Competitivo** |
| `pool-coordinator` | `PoolCoordinator` | RabbitMQ (topic `desafio_activo`) | **Cooperativo** (lado servidor) |
| `pool-worker` | `PoolWorker` | HTTP al coordinator | **Cooperativo** (lado cliente) |
| `pool-auto` | `PoolBully` → se convierte en uno de los dos anteriores | ambos, según quién gane la elección | **Cooperativo** (auto-organizado) |

Y el reparto de trabajo dentro del pool va por **HTTP**, no por cola, por una
razón de diseño concreta que se explica en §5.

Sobre esos modos hay una capa de nombres: los **equipos** (§5.bis). Crear un
equipo promueve un minero propio a `pool-coordinator`; unirse a uno pone un
minero propio en `pool-worker` apuntando a él. La dirección del coordinador no
se escribe nunca a mano — la anuncia el propio worker.

---

## 1. Un solo proceso, varios threads

`worker/main.py` no es "el worker": es un **supervisor de modos**. Esto es lo que
levanta al arrancar, en cualquier modo:

```
                       proceso worker (1 contenedor)
 ┌──────────────────────────────────────────────────────────────────────┐
 │                                                                      │
 │  main()  main.py:327                                                 │
 │   ├─ gpu_usable()          → self-test del binario CUDA (§8)         │
 │   ├─ lee WORKER_MODE       (ConfigMap worker-modes → env var)        │
 │   ├─ WorkerSigner.from_env()  → clave EC P-256 opcional              │
 │   └─ WorkerManager.start(mode)                                       │
 │                                                                      │
 │  THREADS que quedan corriendo:                                       │
 │                                                                      │
 │  [main]          signal.pause()  — no hace nada, sólo mantiene vivo  │
 │  [admin  :9090]  POST /switch-mode, GET /status      admin_server.py │
 │  [health :8080]  GET /health, GET /metrics           common/health   │
 │  [report]        cada 5 s escribe worker:status:<id> en Redis        │
 │  [modo]          el thread del modo activo (ver abajo)               │
 │  [pool   :9001]  SÓLO en pool-coordinator: HTTP server para mineros  │
 │  [automine]      SÓLO en pool-coordinator: auto-minado               │
 └──────────────────────────────────────────────────────────────────────┘
```

Puntos que importan:

- **La conexión a RabbitMQ se abre en los cuatro modos**, incluido `pool-worker`
  (`_start_pool_worker`, `main.py:231`), porque todos se suscriben al exchange
  `worker.command` para poder recibir `switch_mode` remoto.
  ⚠️ El README del worker dice que `pool-worker` "no usa RabbitMQ". Es falso: no
  usa RabbitMQ **para el trabajo de minado**, pero mantiene la conexión abierta.
- **El minado siempre es un subproceso.** `run_miner` (`worker_pkg/miner.py`)
  invoca el binario CUDA o el script CPU con `subprocess.run` y parsea `Nonce = N`
  de stdout. Ningún modo reimplementa el hashing. Es la frontera con Pilar 1.
- **`WORKER_ID`** cae al hostname del contenedor si no se fija. En el compose de
  escalado eso es deliberado: permite `--scale pool-worker=N` sin colisión.

### Cambio de modo en caliente (hot-switch)

`switch_mode` (`main.py:74`) está serializado con un lock y hace siempre lo mismo:
para el modo actual → cierra la mensajería → joinea el thread → arranca el nuevo.
Se puede disparar por dos caminos:

1. **HTTP directo** al worker: `POST :9090/switch-mode {"target":"standalone"}`
2. **RabbitMQ**, exchange `worker.command` con routing key = `worker_id`. Este es
   el camino que usa la UI (`voxchain_api/routers/workers.py:486`).

El comando remoto se aplica **fuera del hilo de consumo** (`_apply_off_thread`,
`main.py:174`) porque el callback de pika corre en el mismo hilo que
`switch_mode` tiene que joinear — hacerlo inline reventaba con
`RuntimeError: cannot join current thread`.

---

## 2. Modo competitivo — `standalone`

### Qué hace

Cada worker standalone es un **participante independiente de la red**. Se suscribe
al broadcast del NCT, mina **el espacio completo de nonces por su cuenta**, y
publica el nonce directo al NCT. No conoce a ningún otro worker.

```
   NCT                    RabbitMQ                    workers standalone
    │                        │                          │      │      │
    │  publish_challenge     │                        w-1    w-2    w-3
    │───────────────────────►│ exchange topic
    │                        │ "desafio_activo"
    │                        │  (fan-out: cola exclusiva por worker)
    │                        │──────┬──────┬──────┐
    │                               ▼      ▼      ▼
    │                          handle_challenge(challenge)
    │                               │      │      │
    │                          mina [0, NONCE_SPACE)  ← LOS TRES EL MISMO RANGO
    │                               │      │      │
    │                          encuentran EL MISMO nonce
    │                               │      │      │
    │        cola "respuesta_nonce" │      │      │
    │◄──────────────────────────────┴──────┴──────┘
    │
    │  gana el PRIMERO EN LLEGAR (SETNX en Redis), los otros dos se descartan
```

### El detalle clave: es minado redundante, no paralelo

Los tres workers arrancan en el nonce **0**, con la misma `partial_hash_base`, y
avanzan en el mismo orden (`standalone_worker.py:66` → `self.mine(base, prefix, 0, self.nonce_space)`).
Encuentran **exactamente el mismo nonce**. Agregar workers standalone **no da
speedup**: da redundancia y tolerancia a fallos, igual que Bitcoin.

Esto no es un defecto — es la definición del modo competitivo — pero explica por
qué toda la batería de escalado del informe corre en modo pool. Medir escalado
sobre standalone daría una línea plana por diseño.

**Quién gana**: el primero cuyo mensaje llega a la cola `respuesta_nonce`. O sea:
la carrera se decide por latencia de red y por microdiferencias de CPU, no por el
valor del nonce.

### Condiciones de validación del ganador (checklist §1)

El NCT aplica, en este orden (`nct/coordinator.py`, `handle_nonce_response`):

1. ¿Soy el líder? Si no, ignora.
2. ¿Hay ventana activa y el `voting_window_id` coincide? Si no, descarta.
3. ¿Llegó antes del `deadline`? Si no, descarta.
4. ¿La firma valida contra `winning_node_or_pool`? (si hay firma o si
   `REQUIRE_SIGNATURES=true`).
5. ¿El ganador **no** es el autor de la ley? (regla 3.4 de AGENT.md).
6. `verify_nonce(base, nonce, n_zeros)` — recalcula el MD5 y verifica el prefijo.
7. `try_seal_window` → `SET window_sealed:<wid> <winner> NX` en Redis. **El primero
   que llega acá gana**; los demás ven el guard puesto y se descartan como tardíos.

### Autonomía política del worker standalone

`STANDALONE_REJECTED_ACTIONS` (`standalone_worker.py:36`) permite que un worker se
niegue a minar cierto tipo de ley:

```bash
STANDALONE_REJECTED_ACTIONS=derogacion   # este nodo no ayuda a derogar nada
```

Es la contraparte individual de la `voting_policy` del pool (§4.4): en standalone
**cada minero decide qué vota**; en pool, decide el dueño del pool.

### Deduplicación

`self._solved` (`standalone_worker.py:33`) es un `set` **en memoria del proceso**
con los `voting_window_id` ya resueltos, para no volver a minar una ventana ya
ganada por uno mismo.

---

## 3. Modo cooperativo — visión general

El modo cooperativo son **dos roles distintos** que hay que levantar juntos:

- **`pool-coordinator`** — recibe el desafío del NCT, lo **fragmenta**, y reparte
  los fragmentos. Además **auto-mina**.
- **`pool-worker`** — no habla con el NCT. Pide fragmentos al coordinator por HTTP,
  los mina, y le devuelve el resultado.

```
                            ┌──────────────┐
   NCT ──desafio_activo────►│              │
                            │     Pool     │  publica el nonce ganador
                            │ Coordinator  │──respuesta_nonce──► NCT
                            │              │
                            │ [0, 50M) →   │
                            │ 50 fragmentos│
                            └──┬────┬───┬──┘
              HTTP :9001       │    │   │        ▲
       ┌───────────────────────┘    │   └────────┼───────────┐
       │                            │            │           │
       ▼                            ▼        (auto-miner     │
  ┌─────────┐                 ┌─────────┐     interno,       │
  │ pool-   │                 │ pool-   │     mismo pool)    │
  │ worker 1│                 │ worker 2│                    │
  └────┬────┘                 └────┬────┘                    │
       │  GET /work/next/<id>      │                         │
       │  → [0, 1M)                │  → [1M, 2M)             │
       │  mina                     │  mina                   │
       │  POST /work/result ───────┴─────────────────────────┘
```

**El coordinator cuenta como un minero.** Su `_auto_mine_loop` consume de la misma
cola de fragmentos que reparte (`coordinator.py:294`). Por eso en el experimento
de escalado M=2 significa "coordinator + 1 pool-worker".

### Fragmentación

Al llegar un desafío, `handle_challenge` (`coordinator.py:248`) parte
`[0, NONCE_SPACE)` en trozos de `FRAGMENT_SIZE` y los mete en un `deque`:

```python
chunks = fragment_range(0, self.nonce_space, self.fragment_size)
```

Con los defaults (`NONCE_SPACE=50_000_000`, `FRAGMENT_SIZE=1_000_000`) son **50
fragmentos**. En el compose de escalado se usa `200M / 500K` = **400 fragmentos**,
a propósito: más fragmentos reparten más parejo entre mineros.

Dos parámetros con efectos opuestos:

- `NONCE_SPACE` debe ser holgadamente mayor que el trabajo esperado (`16^n`) o la
  ventana se agota sin solución. Con `n=6` el promedio es ~16,7 M, de ahí los 200 M.
- `FRAGMENT_SIZE` es el grano: chico reparte mejor pero paga más ida y vuelta HTTP;
  grande es eficiente pero desbalancea.

### Limpieza de fragmentos obsoletos

Hay **dos** puntos donde se descartan fragmentos, y ambos existen por un bug real
que costaba 47 segundos por ley (INFORME §4.7):

1. En `submit_result` (`coordinator.py:214`): al ganar la ventana, los fragmentos
   sin repartir de esa ventana ya no sirven.
2. En `handle_challenge` (`coordinator.py:280`): al llegar un desafío nuevo se
   tiran los de **otras** ventanas. Cubre el caso de una ventana que venció sin
   ganador, que nunca pasa por `submit_result`.

---

## 4. El protocolo HTTP del pool

### Por qué HTTP y no una cola de RabbitMQ

La razón declarada en el README de Pilar 2 es:

> El coordinator necesita saber **qué minero tiene cada rango** para reasignarlo
> si deja de reportar.

El razonamiento es correcto y es la diferencia real entre los dos transportes:

| | Cola RabbitMQ | HTTP pull |
|---|---|---|
| Quién inicia | broker empuja | minero pide |
| Identidad del consumidor | anónima (round-robin) | explícita (`miner_id` en la URL) |
| Alta/baja de mineros | transparente | registro explícito + keep-alive |
| Reasignación al morir uno | requeue por nack/TTL | el coordinator decide |

⚠️ **Pero la reasignación no está implementada.** Ver §7, hallazgo A. Hoy el
argumento justifica la elección de HTTP, pero el beneficio que justifica no se
está cobrando.

Hay una segunda razón, no declarada pero igual de válida: los mineros del clúster
k3s **ya pagan una conexión AMQPS por internet** contra el RabbitMQ de GKE. Meter
además el reparto de fragmentos por esa conexión multiplicaría el tráfico
transatlántico por el número de fragmentos. Con HTTP, el reparto queda **dentro
del clúster del pool** y sólo el nonce ganador cruza a GKE.

### Endpoints

Servidor: `worker_pkg/pool_coordinator/server.py`, puerto `POOL_HTTP_PORT` (9001).
Cliente: `worker_pkg/pool_worker.py`.

| Método | Ruta | Request | Response | Quién llama |
|---|---|---|---|---|
| `POST` | `/register` | `{"capacity":1,"has_gpu":false}` | `{"miner_id":"<pool>-miner-N"}` | pool-worker al arrancar |
| `POST` | `/heartbeat` | `{"miner_id":"..."}` | `{"ok":true|false}` | pool-worker cada 10 s |
| `GET` | `/work/next/<miner_id>` | — | `200` + fragmento, o **`204`** si no hay | pool-worker en loop |
| `POST` | `/work/result` | `{"miner_id":"...","result":{...}}` | `{"ok":true|false}` | pool-worker al encontrar nonce |
| `POST` | `/pool/policy` | `{"decision":"reject","action":"derogacion"}` | `{"ok":true}` | UI / dueño del pool |
| `GET` | `/health` | — | `{"pool","rabbitmq","miners","voting_policy"}` | probes, UI, run_scaling.sh |

**Payload de un fragmento** (`get_next_task`, `coordinator.py:163`):

```json
{
  "voting_window_id": "W12-ley-abc",
  "law_id": "ley-abc",
  "action": "promulgacion",
  "partial_hash_base": "ley-abc<text_hash>W12-ley-abcpromulgacion",
  "n_zeros_required": 6,
  "range_min": 3000000,
  "range_max": 3500000
}
```

El minero traduce `n_zeros_required` a un prefijo de N caracteres `'0'`
(`prefix_for_zeros`) y llama al minero de Pilar 1 con
`(base, prefix, range_min, range_max)` — exactamente la CLI que exponen tanto el
binario CUDA como el script CPU.

### El loop del pool-worker

`PoolWorker.run` (`pool_worker.py:109`) es un bucle anidado:

```
while running:
    while not registered:            ← registro con reintento cada 5 s
        register()
    while registered:
        if pasaron 10 s: heartbeat()
            ├─ ok:True   → seguimos
            ├─ ok:False  → el coordinator NO nos conoce → re-registrar
            └─ None      → el coordinator no responde (HTTP error) → reintentar
        task = GET /work/next/<id>
        if not task: sleep(2); continue    ← 204, no hay trabajo
        nonce = mine(base, prefix, rmin, rmax)   ← BLOQUEANTE
        if nonce: POST /work/result
```

La distinción de tres estados en el heartbeat (`pool_worker.py:80`) es
deliberada y está cubierta por tests: **"no me conocés" ≠ "estás caído"**. Si el
coordinator reinició y perdió el registro, el minero se re-registra; si es un
corte de red, reintenta sin tirar su `miner_id`.

### Keep-alive y capacidad disponible (checklist §1)

Dos niveles:

1. **Minero → coordinator**: `POST /heartbeat` cada 10 s. El coordinator guarda
   `last_seen` y purga a los que pasan `KEEPALIVE_TTL = 15 s`
   (`_purge_stale_miners`, `coordinator.py:153`). Expone
   `voxchain_pool_miners_registered` a Prometheus.
2. **Coordinator → resto del sistema**: `emit_keepalive` (`coordinator.py:317`)
   publica capacidad agregada (`sum(capacity)`, `any(has_gpu)`) a la cola
   `keepalive_trp`, **y** escribe `pool:health:<pool_id>` en Redis con TTL 15 s.

⚠️ La cola `keepalive_trp` **no tiene consumidor** (resto del diseño con TrP
separado). Lo que efectivamente hace visible la capacidad es la clave de Redis y
la métrica de Prometheus, no la cola. Ver §7, hallazgo K.

### Política de voto del pool

`_check_voting_policy` (`coordinator.py:236`) se evalúa **antes de fragmentar**.
Si el dueño del pool rechaza una ley, el coordinator ni siquiera crea los
fragmentos, así que los mineros nunca la ven. Es la diferencia conceptual con
standalone: **en el pool el minero delega su voto en el dueño del pool**, y eso es
justamente la "facción política" que describe AGENT.md §5/P5.

Combinaciones aceptadas: por `action`, por `law_id`, ambas, o rechazo total.

---

## 5. Modo `pool-auto` — el pool que se auto-organiza

Este es el modo que corre en el **despliegue real de k3s**
(`worker-deployment.yaml` → `WORKER_MODE: pool-auto`), y es el menos documentado
del repo.

La idea: N workers idénticos arrancan sin saber quién es el coordinator. Compiten
por un **mini-PoW**; el ganador se convierte en `PoolCoordinator` y los demás en
`PoolWorker` apuntando a él. Es el algoritmo Bully del enunciado, con "esfuerzo"
en lugar de "mayor ID".

### Máquina de estados (`worker_pkg/bully.py`)

```
                    ┌──────────────┐
      arranque ────►│  CANDIDATE   │
                    └──┬────────┬──┘
                       │        │
   12 s sin heartbeat  │        │  llega heartbeat o claim válido de otro
   del coordinator     │        │
                       ▼        ▼
              resuelve mini-PoW   ┌─────────┐
              (2 ceros, seed =    │  MINER  │  arranca un PoolWorker
               "<pool_id>:<epoch>")└────┬────┘  apuntando a leader_address
                       │                │
              publica "claim"           │ 24 s sin heartbeat
              a pool.election           │ del coordinator
                       │                ▼
                       │           vuelve a CANDIDATE
                       ▼
              ┌───────────────┐
              │  COORDINATOR  │  arranca PoolCoordinator + HTTP :9001
              └───────────────┘  emite "heartbeat" cada 5 s
```

Detalles:

- **Transporte**: exchange topic `pool.election`, routing key `<pool_id>.<tipo>`.
  Cada worker se bindea con `<pool_id>.#`, así que los mensajes son fan-out
  dentro del pool.
- **Epoch**: `floor(time()/30)`. Sirve para que todos calculen el mismo seed sin
  coordinarse. Depende de que los relojes estén sincronizados por NTP — de ahí la
  sección de NTP del README de Pilar 3.
- **Verificación del claim** (`_verify_claim`, `bully.py:160`): cualquiera puede
  recomputar `md5(seed + nonce)` y confirmar que empieza con 2 ceros. Nadie
  confía en la palabra del candidato.
- **El claim se publica desde `tick()`, no desde el thread del PoW**
  (`_pending_claim`, `bully.py:65`). Es el mismo cuidado de thread-safety de pika
  que el resto del sistema.
- **`WORKER_ADDRESS`** se arma con la IP del pod (`status.podIP` vía `fieldRef`),
  porque los pods no tienen DNS estable — un `Deployment`, no un `StatefulSet`.

### ⚠️ Hay DOS elecciones de coordinator distintas, y no interoperan

Este es el punto más confuso del código y conviene tenerlo claro antes de tocar
nada:

| | `pool_coordinator/election.py` | `bully.py` |
|---|---|---|
| Usada por el modo | `pool-coordinator` | `pool-auto` |
| Transporte | **Redis** (`SET NX pool:election:<epoch>`) | **RabbitMQ** (exchange `pool.election`) |
| Seed del PoW | `<last_block_hash>:<epoch>` | `<pool_id>:<epoch>` |
| Árbitro final | lease `pool:leader` en Redis | primer `claim` que ve cada worker |
| Detección de caída | TTL del lease (10 s) | timeout de heartbeat (12 s) |
| Ceros | `POOL_ELECTION_N_ZEROS` (2) | `ELECTION_N_ZEROS` hardcodeado (2) |

Un worker en `pool-auto` y otro en `pool-coordinator`, en el mismo "pool", **no se
ven entre sí**: uno arbitra por Redis y el otro por mensajes. Ambos pueden creerse
coordinator simultáneamente. Ver §7, hallazgo B.

Nota: cuando `PoolBully` arranca su coordinator lo hace con `redis=None`
(`bully.py:201`), lo que en `PoolCoordinator.__init__` fuerza `is_leader = True`
incondicionalmente — o sea, la elección por Redis queda **desactivada** dentro de
`pool-auto`, a propósito, porque el arbitraje ya lo hizo el bully.

---

## 5.bis Equipos: la capa de nombres sobre el modo cooperativo

Los modos de §3 y §4 son el mecanismo; **los equipos son cómo lo usa una
persona**. Un equipo no agrega ningún servicio: es un `pool-coordinator` con
nombre más la lista de quién se le unió. Lo que aporta es que nadie escribe
`http://algo:9001` a mano.

### El problema que resuelve

Para poner un minero en modo `pool-worker` hay que decirle la URL del
coordinador. Averiguarla a mano es, en la práctica, imposible para un usuario:
en Compose el hostname del contenedor no tiene por qué coincidir con el
`WORKER_ID`, y en Kubernetes los pods de un `Deployment` no tienen DNS estable —
la dirección buena es la IP del pod, que cambia en cada reinicio.

La solución es invertir quién sabe la dirección: **el worker la anuncia**.
`WorkerManager._resolve_address` (`worker/main.py`) la resuelve una vez al
arrancar, en este orden:

1. `WORKER_ADDRESS` explícita — se fija en todos los despliegues que
   controlamos (Compose, y el spawner de Kubernetes con `http://$(MY_POD_IP):9001`).
2. `MY_POD_IP` (inyectada por `fieldRef: status.podIP`).
3. El hostname del contenedor, como último recurso.

Esa dirección va en `get_status()` y de ahí al estado que el worker publica en
Redis cada 5 s (`worker:status:<id>`). El backend la lee y se la entrega a quien
se una al equipo.

### Modelo de datos

| Clave de Redis | Contenido |
|---|---|
| `teams` | set de `team_id` |
| `team:<team_id>` | hash: `name`, `owner`, `coordinator_worker_id`, `coordinator_url`, `created_at` |
| `team:members:<team_id>` | set de `worker_id` (sin el coordinador) |
| `worker:team:<worker_id>` | índice inverso: en qué equipo está cada worker |

El índice inverso existe porque la pregunta que más hace la UI es "¿este worker
está en algún equipo?", y sin él habría que recorrer todos los equipos por cada
fila de la tabla de mineros.

Implementación: `voxchain_api/services/teams_store.py`.

### Endpoints

| Método | Ruta | Qué hace | `switch_mode` que despacha |
|---|---|---|---|
| `GET` | `/api/teams` | Lista con plantel y estado del coordinador | — |
| `GET` | `/api/teams/{id}` | Detalle de un equipo | — |
| `POST` | `/api/teams` | Crea el equipo y promueve un minero propio | `pool-coordinator` al coordinador |
| `POST` | `/api/teams/{id}/join` | Suma un minero propio | `pool-worker` con la URL del coordinador |
| `POST` | `/api/teams/{id}/leave` | Saca un minero propio | `standalone` |
| `DELETE` | `/api/teams/{id}` | Disuelve | `standalone` a **todo** el plantel |

`POST /api/teams` acepta un `new_worker` opcional con el alta firmada del minero
(`worker_id`, `pubkey`, `timestamp`, `signature`, `private_key`), para el usuario
que se registró recién y todavía no tiene ninguno: se da de alta, se despliega y
se promueve en un solo paso. Sin eso el formulario sería un callejón sin salida.

### Una identidad funda un solo equipo

`create_team` rechaza con 409 si el `owner` ya fundó uno (índice
`team:owner:<owner>`; se libera al disolver). No es una limitación técnica sino
una regla del dominio: un equipo es una **facción política** que concentra poder
de cómputo (AGENT.md 3.9), y dejar que una misma clave funde varios le daría a un
solo individuo tantos frentes como quisiera armar, agravando gratis la
concentración de poder que el sistema ya documenta como su debilidad.

Lo que se limita es **fundar**, no participar: sumar varios mineros propios al
equipo de otro sigue permitido. Y la restricción es por identidad, no por
minero — con Sybil sigue siendo evadible, igual que todo lo demás en el sistema
(AGENT.md 9); acá no se pretende cerrar ese agujero, sólo no ensancharlo.

### La invariante que sostiene todo

> **El estado del equipo y el modo real del worker se mueven siempre juntos.**

Por eso `POST /api/workers/{id}/switch-mode` **sólo acepta `standalone`**. Antes
aceptaba `pool-coordinator` y `pool-worker` con una `pool_url` escrita a mano, y
eso permitía dos cosas malas: apuntar a un coordinador que no existe, y sacar un
minero de un equipo sin que el equipo se entere — la lista de miembros quedaba
mintiendo. Hoy:

- Entrar a cooperativo: sólo por `/api/teams`.
- Volver a competitivo: por `switch-mode`, que **además** desarma la membresía.
- El coordinador no puede volver a competitivo por ese camino (409): sus
  miembros quedarían pidiéndole fragmentos a un HTTP que ya no reparte. Tiene
  que disolver el equipo, y la disolución devuelve a todos a `standalone`.

Cobertura: `tests/test_teams.py`, incluidos los cuatro casos de arriba y la
regla de un equipo por identidad.

### Dónde vive en la UI

Equipos y mineros son **una sola pantalla** (`/workers`, "Minería"): con quién
minás y con qué minás son la misma decisión vista de dos lados, y separarlas
obligaba a saltar de pantalla para entender el estado de un solo minero. La ruta
`/teams` redirige ahí. `TeamsComponent` quedó como sección embebida: recibe
`teams` y `workers` por input y avisa con `changed`, así la página hace un solo
sondeo para las dos secciones.

### Secuencia de "unirse a un equipo"

```
UI                    API                     Redis            RabbitMQ        worker
 │  POST join          │                        │                 │              │
 ├────────────────────►│                        │                 │              │
 │                     │ ¿es dueño del minero?  │                 │              │
 │                     ├───────────────────────►│ worker:owner:*  │              │
 │                     │ estado del coordinador │                 │              │
 │                     ├───────────────────────►│ worker:status:* │              │
 │                     │◄─── address ───────────┤                 │              │
 │                     │                        │                 │              │
 │              ¿coordinador en línea?  no → 409 y NO se manda ninguna orden      │
 │                     │                        │                 │              │
 │                     │ join_team              │                 │              │
 │                     ├───────────────────────►│ team:members:*  │              │
 │                     │ switch_mode(pool-worker, address)        │              │
 │                     ├─────────────────────────────────────────►│─────────────►│
 │◄─── Team ───────────┤                        │                 │      GET /work/next/…
```

El chequeo de "coordinador en línea" antes de mandar la orden es deliberado: si
el coordinador está caído, mandar igual al minero lo dejaría girando en vacío
contra un HTTP muerto, y el equipo mostraría un miembro que no está minando nada.

---

## 6. Dónde corre cada modo

| Entorno | Archivo | Qué levanta |
|---|---|---|
| **Demo local** | `pilar2-distribuido/docker-compose.yml` | `worker-1` y `worker-2` en **standalone** + `worker-pool-coordinator` en **pool-coordinator** |
| **Experimento de escalado** | `pilar2-distribuido/docker-compose.scale.yml` | 1 `pool-coordinator` + N `pool-worker` (`--scale`), cada uno con `cpus: 1.0` |
| **k3s (real)** | `kubernetes/gpu-cluster/worker-deployment.yaml` | 2 réplicas en **pool-auto** |
| **k3s (demo UI)** | `pool-coordinator-deployment.yaml` + `pool-miner-deployment.yaml` | coordinator explícito + mineros en **pool-worker** |

⚠️ En `./run.sh demo` el `worker-pool-coordinator` **no tiene ningún pool-worker
conectado**: no hay servicio `pool-worker` en el compose principal. O sea, el
camino cooperativo se ejercita sólo con el auto-miner del coordinator. Para ver el
pool real hace falta `run_scaling.sh` o el clúster.

### Cómo se elige el modo al arrancar

`main.py:334`, en orden de precedencia:

1. ConfigMap `worker-modes` de Kubernetes, clave = `worker_id`
   (`_read_mode_from_configmap`, `main.py:142`) — persiste el modo entre reinicios
   del pod tras un hot-switch.
2. Variable de entorno `WORKER_MODE`.
3. Default: `standalone`.

---

## 7. Hallazgos: lo que hay que decidir si se toca o se deja

Ordenados por impacto. Los primeros tres son diferencias entre lo que el código
hace y lo que la documentación entregable afirma.

### A. No hay reasignación de fragmentos al caer un minero 🔴

**Qué dice la doc.** INFORME §5.1: *"El coordinator trackea qué minero tiene cada
fragmento y purga a los que dejan de mandar keep-alive. **El fragmento vuelve a la
cola de pendientes.**"* El README de Pilar 2 usa el mismo argumento para justificar
HTTP sobre colas. La checklist §1 lo pregunta explícitamente: *"Manejo de fallas en
workers: ¿Qué pasa si uno cae? **¿Se reasignan tareas?**"*.

**Qué hace el código.** `get_next_task` (`coordinator.py:163`) hace
`self._pending_fragments.popleft()` y devuelve el fragmento **sin registrar a quién
se lo dio**. `_purge_stale_miners` (`coordinator.py:153`) borra la entrada del
minero de `self._miners` y nada más. El campo `busy` se inicializa en `False` al
registrar (`coordinator.py:135`) y **nunca se vuelve a escribir**. No existe ningún
`assigned`, `in_flight` ni requeue en todo el archivo.

**Consecuencia real.** Si un minero muere con un fragmento en la mano, ese rango de
nonces **no lo barre nadie**. Si el nonce ganador estaba ahí, la ventana vence sin
solución y la ley se descarta, aunque la red tuviera capacidad de sobra. Con 400
fragmentos y 4 mineros la probabilidad es baja; con fragmentos grandes y pocos
mineros, no tanto.

**Qué habría que hacer.** Es acotado: un dict `{miner_id: fragmento}` poblado en
`get_next_task`, limpiado en `submit_result`, y en `_purge_stale_miners` hacer
`appendleft` del fragmento del minero purgado. ~15 líneas más un test. Alternativa
honesta si no se quiere tocar código: corregir el INFORME y el README para que
describan lo que hay (el trabajo perdido es como mucho un fragmento, y la ventana
sigue abierta para el resto).

### B. Dos elecciones de coordinator incompatibles 🟠

Descrito en §5. `election.py` (Redis) y `bully.py` (RabbitMQ) resuelven el mismo
problema con seeds, árbitros y timeouts distintos. Funciona porque cada modo usa
sólo la suya, pero:

- Es difícil de explicar en una defensa ("¿cuál de las dos es *la* elección?").
- Un pool mixto (algunos pods en `pool-auto`, otros en `pool-coordinator`) tiene
  dos coordinators activos sin que ninguno lo detecte.
- `ELECTION_N_ZEROS` está hardcodeado en `bully.py:22` mientras el otro camino lo
  lee de `POOL_ELECTION_N_ZEROS`.

**Decisión a tomar**: unificar en una (la de `bully.py` es la que corre en el
despliegue real y no necesita Redis, que es una ventaja para workers federados), o
documentar explícitamente que son dos mecanismos para dos topologías distintas.

### C. El minado es bloqueante y no mira el deadline 🟠

`self.mine(...)` barre el rango **entero** antes de devolver. Ni el standalone
(`standalone_worker.py:71`), ni el auto-miner (`coordinator.py:305`), ni el
pool-worker (`pool_worker.py:146`) chequean el deadline **durante** el barrido, y
`run_miner` se llama siempre con `timeout=None`.

Efecto: si una ventana vence a mitad de un barrido, el worker sigue quemando CPU en
trabajo inútil y recién atiende la ventana siguiente cuando termina. El deadline sí
se chequea **al recibir** el desafío (`standalone_worker.py:60`), lo que evita
arrancar tarde, pero no cortar a mitad.

En el pool esto está mitigado por el tamaño del fragmento (un fragmento de 500 K
nonces son fracciones de segundo). En standalone con `NONCE_SPACE=50M` y `n=6`, no.

### C-bis. En modo `pool-worker` nadie consumía RabbitMQ ✅ corregido

`_start_pool_worker` usaba el único hilo del modo para el bucle de minado, así
que **nunca se llamaba a `start_consuming`**: el consumidor de `worker.command`
quedaba registrado en `self._handlers` pero jamás activo. El worker publicaba su
estado y aceptaba órdenes por el admin HTTP, pero la orden por RabbitMQ —el
único camino que llega al clúster k3s— no la consumía nadie.

Con el flujo de equipos esto pasó de latente a bloqueante: un minero que se une
a un equipo no podría salir nunca de él, porque la orden de volver a competitivo
se publicaría y se perdería. Corregido con dos hilos (consumo y minado) y test
de regresión en `worker/tests/test_switch_mode_remoto.py`.

En la misma pasada: salir de `pool-auto` no detenía el bully, así que el
coordinator o el pool-worker que había arrancado por dentro sobrevivían al
cambio de modo — el worker terminaba minando en dos modos a la vez. Y las
esperas del `PoolWorker` pasaron de `time.sleep` a un `threading.Event`, para
que sacar un minero de su equipo se aplique en el acto en vez de tardar hasta
5 segundos.

### D. El health endpoint del worker no dice nada 🟡

`main.py:350`: `start_health_server(config.HEALTH_PORT, lambda: {"status": "ok"})`.
Devuelve una constante. El README del worker afirma que devuelve
`{"worker_id","mode","status"}`, y la checklist §2 pide *"endpoint público de estado
por servicio"*.

El estado real (modo, pool_url, bully_state, running, pubkey) existe —
`get_status()`, `main.py:60` — y se publica por dos caminos: `GET :9090/status` y la
clave `worker:status:<id>` en Redis. Sería un cambio de una línea hacer que
`/health` devuelva `get_status()`.

Relacionado: `worker-deployment.yaml` fija `REDIS_URL: ""`, así que los workers de
k3s en modo `pool-auto` **no reportan estado a Redis**; la UI los ve sólo si
están en el set `registered_workers`, y no pueden participar de un equipo con
nombre (§5.bis) porque nunca publican su dirección. Los mineros que la UI
despliega sí reciben `REDIS_URL`.

### E. `rejected_actions` está cableado a medias 🟡

`admin_server.py:36` devuelve `ctx.get("rejected_actions","")` pero `get_status()`
nunca pone esa clave → siempre `""`. `admin_server.py:53` lee `rejected_actions` del
body de `/switch-mode` y **lo descarta**: nunca se pasa a `switch_mode`. La única
forma de configurarlo es la variable de entorno al arrancar, o sea que no se puede
cambiar en caliente aunque la API sugiera que sí.

### F. `_solved` crece sin límite y se pierde en el switch 🟡

Tanto `StandaloneWorker._solved` como `PoolCoordinator._solved` son sets en memoria
que acumulan un `voting_window_id` por ventana y nunca se podan. En un proceso de
larga vida es una fuga lenta (irrelevante para el TP, real en producción). Y tras
un hot-switch se resetean, así que el worker puede volver a minar una ventana que
ya resolvió.

### G. Colas declaradas sin consumidor 🟡

`_declare_topology` (`common/messaging/rabbitmq.py`) declara `tareas_trp` y
`keepalive_trp`. `tareas_trp` no tiene publicador ni consumidor. `keepalive_trp`
tiene publicador (`emit_keepalive`) y **ningún consumidor**. Está declarado como
limitación conocida en el README de Pilar 2, pero conviene saber que el keep-alive
agregado del pool efectivamente **se pierde**; lo que funciona es
`pool:health:<id>` en Redis + la métrica de Prometheus.

### H. `PoolHTTPHandler.coordinator` es atributo de clase 🟢

`server.py:82`: `PoolHTTPHandler.coordinator = coordinator`. Si dos coordinators
convivieran en un proceso se pisarían. Hoy no pasa (siempre hay uno), pero el
hot-switch reasigna el atributo global y es una trampa si alguien agrega
concurrencia.

---

## 8. Anexo: GPU vs CPU y el self-test

`gpu_usable` (`miner.py:80`) no se limita a mirar si el binario existe. Corre un
**self-test**: mina el prefijo `"0"` en `[0, 512)`, donde la probabilidad de no
encontrar nada con una GPU sana es `(15/16)^512 ≈ 4e-15`.

La razón está comentada en el código y es un hallazgo real: en un nodo sin GPU el
binario CUDA **falla en silencio** — las llamadas CUDA fallan pero el proceso sale
con código 0 y sin nonce. El fallback por excepción nunca se disparaba y el worker
minaba al vacío indefinidamente. El self-test convierte "no encontró" en "la GPU no
sirve" y cachea el resultado.

El fallback a CPU es automático en los cuatro modos, porque vive dentro de
`run_miner` y todos los modos lo invocan igual.

---

## 9. Tabla de configuración por modo

| Variable | `standalone` | `pool-coordinator` | `pool-worker` | `pool-auto` |
|---|---|---|---|---|
| `RABBITMQ_URL` | ✅ desafío + comandos | ✅ desafío + comandos | ⚪ sólo comandos | ✅ elección + desafío |
| `REDIS_URL` | ⚪ sólo reporte de estado | ✅ lease + política | ⚪ sólo reporte | ❌ no se usa |
| `NONCE_SPACE` | — | ✅ espacio a fragmentar | — | ✅ (si gana) |
| `STANDALONE_NONCE_SPACE` | ✅ espacio a barrer | — | — | — |
| `FRAGMENT_SIZE` | — | ✅ grano del reparto | — | ✅ (si gana) |
| `POOL_HTTP_PORT` | — | ✅ escucha | — | ✅ escucha/conecta |
| `WORKER_ADDRESS` | ⚪ se publica igual | ✅ es la URL del equipo | ⚪ se publica igual | ✅ dónde encontrarlo |
| `MY_POD_IP` | ⚪ respaldo de address | ⚪ respaldo | ⚪ respaldo | ⚪ respaldo |
| `POOL_COORDINATOR_URL` | — | — | ✅ a quién pedirle | ⚪ default |
| `POOL_ID` | — | — | — | ✅ ámbito de la elección |
| `WORKER_CAPACITY` | — | ✅ propia + agregada | ✅ se reporta | ✅ |
| `STANDALONE_REJECTED_ACTIONS` | ✅ voto propio | — | — | — |
| `WORKER_PRIVKEY_PEM` | ✅ firma nonces | ✅ firma nonces | — (firma el coord.) | ✅ |
| `MINER_GPU_BIN` / `MINER_CPU_SCRIPT` | ✅ | ✅ | ✅ | ✅ |
