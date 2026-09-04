# Worker minero

El worker ejecuta el Proof of Work de gobierno **invocando el minero de Pilar 1**
(no se reimplementa el hashing).

> Referencia detallada de los modos competitivo y cooperativo (protocolo HTTP del
> pool, elección de coordinator, threads, y diferencias entre lo documentado y lo
> implementado): [`docs/workers.md`](../../docs/workers.md).

## Modos de operación

El worker soporta **cuatro modos** intercambiables en caliente vía hot-switch:

| Modo | `WORKER_MODE` | Cómo recibe trabajo | Rango de nonces | A quién reporta |
|------|---------------|---------------------|-----------------|-----------------|
| Standalone | `standalone` (default) | Topic `desafio_activo` del NCT | Completo `[0, NONCE_SPACE)` | NCT (`respuesta_nonce`) |
| Pool-coordinator | `pool-coordinator` | Topic `desafio_activo` del NCT (fragmenta internamente) | Fragmenta y reparte + auto-mina | NCT (`respuesta_nonce`) |
| Pool-worker | `pool-worker` | HTTP al Pool Coordinator | Fragmento del coordinator | Pool Coordinator |
| Pool-auto | `pool-auto` | Elección bully por mini-PoW (RabbitMQ); el ganador coordina y el resto mina | Según el rol que le toque | NCT o coordinator |

> **Desde la UI los modos de pool no se eligen sueltos: se eligen creando o
> uniéndose a un equipo.** `POST /api/workers/{id}/switch-mode` sólo acepta
> `standalone`; entrar al modo cooperativo va por `/api/teams`, que además
> resuelve la dirección del coordinador sola. El detalle está en
> [`docs/workers.md`](../../docs/workers.md) §5.bis.

### Standalone mode
Se suscribe directo al exchange `desafio_activo` del NCT. Mina el **espacio
completo de nonces** y publica el resultado directamente al NCT. Filtra leyes
según `STANDALONE_REJECTED_ACTIONS` (por acción) y `STANDALONE_CATEGORIES` (por
área de gobierno) — el usuario decide qué leyes votar.

### Pool-coordinator mode
El worker actúa como líder de un pool: se suscribe al `desafio_activo`, aplica
la `voting_policy` (agenda temática + veto por acción/ley), **fragmenta el
espacio de nonces**, distribuye fragmentos a workers conectados vía HTTP, y
también **auto-mina** sus propios fragmentos. Usa Redis para liderazgo (HA del
pool coordinator) y para releer su política, que el backend deja en
`pool:policy:<pool_id>` cuando el dueño del equipo cambia la agenda.

### Pool-auto mode
N workers intercambiables del mismo `POOL_ID` compiten por un mini-PoW vía
RabbitMQ; el ganador arranca un Pool Coordinator y el resto se le une por HTTP.
**No necesita Redis** —ésa es su ventaja para nodos federados— pero si lo hay, su
coordinador toma el lease `pool:leader:<POOL_ID>`, el mismo que usa el modo
`pool-coordinator`. Así las dos elecciones, que eligen por transportes distintos,
comparten un único árbitro final: un pool mixto no puede quedar con dos
coordinadores activos, porque el que no consigue el lease se retira a candidato.
Ver `docs/workers.md` §5 y §7/B.

### Pool-worker mode
Se conecta vía HTTP a un Pool Coordinator. Delega la **decisión de voto** al
dueño del pool: el coordinator aplica su `voting_policy` y solo asigna trabajo
si la ley es aceptada. El minero no elige qué minar. No usa RabbitMQ.

## Votación y autonomía

- Los mineros dentro de un **pool** delegan el sentido de voto al dueño del pool.
  Si el dueño rechaza una ley, el pool coordinator no distribuye trabajo para
  esa ley y los mineros nunca la procesan — no hace falta avisarle a cada
  minero, porque sólo pueden trabajar sobre los fragmentos que el coordinador
  reparte. Hay dos filtros independientes en `_check_voting_policy`:
  - **Agenda temática** (`categories`, AGENT.md 3.10): las áreas de ley a cuyas
    ventanas el equipo aporta cómputo. Vacía = todas. La elige el fundador del
    equipo desde la UI (Mineros → Equipos → Cambiar) y el backend la baja a
    `pool:policy:<pool_id>`, que el coordinador relee en cada tick.
  - **Veto puntual** (`decision: reject` por `action` y/o `law_id`), que se fija
    desde Workers → Configure Policy o con `POST /pool/policy`.
- Los mineros en modo **standalone** deciden por sí mismos qué leyes minar
  mediante `STANDALONE_REJECTED_ACTIONS` (por acción) y `STANDALONE_CATEGORIES`
  (por área; vacía = mina todo).
- En cualquier momento un minero puede cambiar de modo mediante hot-switch
  sin reiniciar el contenedor.

## Hot-switch (cambio de modo en caliente)

El worker expone un servidor HTTP de administración en el puerto `9090`
(variable `ADMIN_PORT`). Este puerto es una **herramienta de operación**, no la
vía de uso normal: acepta cualquier modo, sin pasar por la capa de equipos ni
por la verificación de dueño. En un despliegue no está expuesto fuera del
clúster; los usuarios administran sus mineros por la API.

```bash
# De pool-worker a standalone
curl -X POST http://worker:9090/switch-mode \
  -d '{"target":"standalone"}'

# De standalone a pool-worker
curl -X POST http://worker:9090/switch-mode \
  -d '{"target":"pool-worker","pool_url":"http://pool-coordinator:9001"}'

# De standalone a pool-coordinator
curl -X POST http://worker:9090/switch-mode \
  -d '{"target":"pool-coordinator"}'

# Ver modo actual
curl http://worker:9090/status
```

Al cambiar de modo:
1. El worker actual se detiene limpiamente (señal `stop()`)
2. Se inicia el nuevo modo en un thread separado
3. El health endpoint refleja el modo activo

## Estructura

| Archivo | Contenido |
|---------|-----------|
| `main.py` | Punto de entrada, `WorkerManager` con hot-switch, health y admin server |
| `worker_pkg/standalone_worker.py` | Worker standalone: consume `desafio_activo`, mina espacio completo |
| `worker_pkg/pool_worker.py` | Worker de pool: HTTP al Pool Coordinator |
| `worker_pkg/pool_coordinator/` | Pool Coordinator embebido (fragmentación, auto-miner, HTTP server) |
| `worker_pkg/pool_coordinator/coordinator.py` | Lógica del pool: fragmentación, política de voto, auto-miner |
| `worker_pkg/pool_coordinator/server.py` | Servidor HTTP para workers del pool |
| `worker_pkg/miner.py` | Puente al minero de Pilar 1 (GPU/CPU) y parseo de salida |
| `worker_pkg/admin_server.py` | Servidor HTTP para hot-switch (`POST /switch-mode`, `GET /status`) |

## Ejecución

```bash
docker compose up --build worker   # levanta 2 réplicas en modo standalone

# Modo pool-coordinator (requiere Redis)
docker compose up --build worker-pool-coordinator

# Modo pool-worker (conectarse a un pool coordinator)
WORKER_MODE=pool-worker POOL_COORDINATOR_URL=http://pool:9001 docker compose up --build worker
```

Health: `GET :8080/health` → `{"worker_id":"...", "mode":"standalone", "status":"ok"}`.

## Configuración

| Variable | Default | Descripción |
|----------|---------|-------------|
| `MINER_GPU_BIN` | (vacío) | Ruta al binario CUDA; si no existe, se usa CPU. |
| `MINER_CPU_SCRIPT` | `/app/pilar1-minero/cpu/src/brute_force.py` | Minero CPU (fallback). |
| `WORKER_ID` | `worker-<hostname>` | Identidad del worker (gana el bloque). |
| `WORKER_CAPACITY` | 1 | Capacidad reportada en el keep-alive. |
| `WORKER_MODE` | `standalone` | Modo inicial: `standalone`, `pool-coordinator`, o `pool-worker`. |
| `POOL_COORDINATOR_URL` | `http://pool-coordinator:9001` | URL del Pool Coordinator (modo pool-worker). |
| `POOL_HTTP_PORT` | `9001` | Puerto HTTP del pool coordinator embebido (modo pool-coordinator). |
| `WORKER_ADDRESS` | `http://<MY_POD_IP o hostname>:<POOL_HTTP_PORT>` | Dirección con la que otros mineros lo alcanzan si coordina un equipo. Se publica en el estado y el backend se la entrega a quien se una. Conviene fijarla explícitamente: el hostname del contenedor no tiene por qué coincidir con el `WORKER_ID`. |
| `MY_POD_IP` | (vacío) | IP del pod, inyectada por `fieldRef` en Kubernetes. Respaldo de `WORKER_ADDRESS`. |
| `NONCE_SPACE` | `50000000` | Tamaño total del espacio de nonces a fragmentar (pool-coordinator/standalone). |
| `FRAGMENT_SIZE` | `1000000` | Tamaño de cada fragmento (modo pool-coordinator). |
| `STANDALONE_NONCE_SPACE` | `50000000` | Tamaño del espacio de nonces (modo standalone). |
| `STANDALONE_REJECTED_ACTIONS` | (vacío) | Acciones a rechazar en modo standalone, separadas por coma. Ej: `derogacion` |
| `ADMIN_PORT` | `9090` | Puerto del servidor de administración (hot-switch). |
| `RABBITMQ_URL` | `amqp://guest:guest@rabbitmq:5672/` | Conexión RabbitMQ. |
| `HEALTH_PORT` | `8080` | Puerto del health endpoint. |
| `REDIS_URL` | `redis://redis:6379/0` | Conexión Redis (modo pool-coordinator). |

## Decisiones de diseño

- **El minero es un subproceso, no una librería**: respeta la frontera con Pilar 1
  y permite usar el binario CUDA tal cual. La salida (`Nonce = N`) es común a GPU y
  CPU, así que el parseo es uno solo.
- **Sin Redis en el worker standalone**: el worker standalone es stateless respecto
  del estado de la cadena; la deduplicación de soluciones tardías la hace el NCT.
- **Hot-switch vía threads**: cada modo corre en un thread para poder detenerlo
  limpiamente al cambiar sin reiniciar el proceso.
- **Pool-coordinator embebido**: ya no hay un servicio `pool-coordinator` separado
  ni un `transaction-pool`. Cada nodo que quiere ser pool leader ejecuta el
  coordinator dentro del worker. La fragmentación del espacio de nonces es interna.
- **Pool-worker delega voto**: el minero dentro de un pool no elige qué leyes minar,
  esa decisión la centraliza el pool coordinator según la política del dueño.
- **El worker anuncia su dirección, no la adivina el backend**: `WORKER_ADDRESS`
  (o `MY_POD_IP`, o el hostname) se publica en `worker:status:<id>`. Es lo que
  permite que un usuario se una al equipo de otro sin averiguar ninguna URL, y
  lo único que funciona en Kubernetes, donde los pods de un Deployment no tienen
  DNS estable.
- **Un hilo para consumir, otro para minar** (modo pool-worker): el bucle de
  pedir y minar fragmentos bloquea, así que no puede compartir hilo con el
  consumo de RabbitMQ. Si lo comparte, el worker deja de escuchar
  `worker.command` y ya no hay forma de sacarlo del equipo.
- **Standalone mina todo el espacio**: sin pool, el worker se suscribe al desafío
  del NCT y barre `[0, STANDALONE_NONCE_SPACE)` compitiendo directamente contra
  el resto de la red.
- **En contenedor sin GPU**, el fallback CPU es automático (el binario CUDA no existe).
