# VoxChain API

API Gateway para VoxChain, implementada con FastAPI. Expone endpoints REST para consultar la blockchain, leyes, ventanas de votación y proponer nuevas leyes. Incluye Server-Sent Events (SSE) para actualizaciones en tiempo real y métricas Prometheus.

## Responsabilidades

- Expone endpoints REST para consultar cadena, leyes, ventanas y health del sistema.
- Permite proponer nuevas leyes vía POST a `/api/laws` (publica a cola `propuestas`).
- Lee estado de Redis (cadena, leyes, ventanas) sin escribir directamente.
- Publica propuestas de leyes a RabbitMQ para consumo del NCT.
- Implementa SSE (`/api/events`) para broadcasting de cambios en tiempo real (bloques, ventanas, leyes).
- Expone métricas Prometheus en `/metrics` para monitoreo.
- Health check agregado que verifica NCT, Redis y estado interno.

## Estructura

| Archivo                | Contenido |
|------------------------|-----------|
| `main.py` | Aplicación FastAPI, configuración CORS, middleware de métricas, tarea de fondo SSE. |
| `config.py` | Configuración desde variables de entorno (Redis, RabbitMQ, URLs de health, puerto). |
| `models.py` | Modelos Pydantic para requests/responses (Law, Window, Block, HealthResponse, etc.). |
| `routers/chain.py` | Endpoints para consultar la blockchain (`GET /api/chain`, `GET /api/chain/{block_hash}`). |
| `routers/laws.py` | Endpoints para leyes (`GET /api/laws`, `POST /api/laws`, `GET /api/laws/{law_id}`, cola, texto y catálogo de categorías). |
| `routers/windows.py` | Endpoints para ventanas de votación (`GET /api/windows/active`, `GET /api/windows/{voting_window_id}`; no hay listado de todas). |
| `routers/workers.py` | Gestión de workers: status, registro/baja dinámica, switch de modo, y política de voto (`accept`/`reject` por `action` o `law_id`) de pool coordinators. |
| `routers/accounts.py` | Cuentas demo: listado, reserva y liberación de sesión (`GET/POST /api/accounts/...`). |
| `routers/health.py` | Health check agregado (`GET /api/health`). |
| `services/redis_reader.py` | Cliente de lectura de Redis (cadena, leyes, ventanas). |
| `services/rabbitmq_publisher.py` | Publicador de RabbitMQ para propuestas de leyes. |

## Ejecución

```bash
# Vía docker-compose (recomendado), desde pilar2-distribuido/
docker compose up --build voxchain-api

# Directo (requiere Redis y RabbitMQ accesibles)
REDIS_URL=redis://localhost:6379/0 RABBITMQ_URL=amqp://guest:guest@localhost:5672/ \
NCT_HEALTH_URL=http://localhost:8080/health \
PORT=8000 python -m voxchain_api.main
```

Root: `GET /` → `{"service":"voxchain-api","version":"1.0.0","status":"running"}`.
Health: `GET /api/health` → `{"api":"ok","nct":"ok","redis":"ok"}`.
Metrics: `GET /metrics` → Métricas Prometheus (Prometheus text format).

## API REST

| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/api/chain` | GET | Obtiene toda la blockchain |
| `/api/chain/{block_hash}` | GET | Obtiene un bloque por su hash (no por índice) |
| `/api/laws` | GET | Obtiene todas las leyes (filtros opcionales `?status=` y `?category=`) |
| `/api/laws/categories` | GET | Áreas de gobierno disponibles (`value`/`label`); es la lista autoritativa que valida el NCT |
| `/api/laws/next` | GET | Próxima ley que entrará en ventana de votación (orden round-robin) |
| `/api/laws/queue` | GET | Cola completa de leyes pendientes, en orden |
| `/api/laws/{law_id}` | GET | Obtiene una ley por ID |
| `/api/laws/{law_id}/text` | GET | Texto descomprimido de una ley |
| `/api/laws` | POST | Propone una nueva ley (publica a RabbitMQ). Acepta `category`; una categoría desconocida es 400, y en una derogación se ignora y manda la de la ley original |
| `/api/windows/active` | GET | Obtiene la ventana activa actual |
| `/api/windows/{voting_window_id}` | GET | Obtiene una ventana por ID (no existe un listado de todas) |
| `/api/workers/status` | GET | Estado de todos los workers registrados |
| `/api/workers/{worker_id}/status` | GET | Estado de un worker puntual |
| `/api/workers/{worker_id}/switch-mode` | POST | Cambia el modo de un worker (standalone/pool-coordinator/pool-worker) |
| `/api/workers/pool/{pool_id}/health` | GET | Health de un pool coordinator (miners, rabbitmq, política de voto) |
| `/api/workers/pool/{pool_id}/policy` | POST | Fija la política de voto del pool: `accept` o `reject` (por `action` y/o `law_id`). Si no manda `categories`, conserva la agenda temática que ya tuviera el pool |
| `/api/teams` | GET/POST | Lista los equipos / funda uno (acepta `categories`: la agenda temática) |
| `/api/teams/{team_id}` | GET/DELETE | Detalle de un equipo / lo disuelve (sólo el fundador) |
| `/api/teams/{team_id}/categories` | PUT | Cambia las áreas de ley que vota el equipo (sólo el fundador). Lista vacía = vota todas |
| `/api/teams/{team_id}/join` | POST | Suma un minero propio al equipo, en modo `pool-worker` |
| `/api/teams/{team_id}/leave` | POST | Saca un minero propio del equipo y lo devuelve a competitivo |
| `/api/workers/register` | POST | Registra un worker dinámico nuevo |
| `/api/workers/{worker_id}` | DELETE | Da de baja un worker dinámico |
| `/api/accounts` | GET | Lista las cuentas demo disponibles/ocupadas |
| `/api/accounts/{username}` | GET | Detalle de una cuenta demo |
| `/api/accounts/reserve` | POST | Reserva una cuenta demo para una sesión |
| `/api/accounts/release` | POST | Libera una cuenta demo reservada |
| `/api/health` | GET | Health check agregado del sistema |
| `/api/events` | GET | SSE stream para eventos en tiempo real |

## Eventos SSE

El endpoint `/api/events` emite eventos en tiempo real cuando ocurren cambios en el sistema:

| Evento | Data | Descripción |
|--------|------|-------------|
| `block_added` | `{"block": {...}}` | Nuevo bloque añadido a la cadena |
| `window_opened` | `{"window": {...}}` | Nueva ventana de votación abierta |
| `window_closed` | `{}` | Ventana de votación cerrada |
| `law_updated` | `{"law_id": "...", "status": "..."}` | Estado de ley actualizado |

## Configuración (variables de entorno)

| Variable | Default | Descripción |
|----------|---------|-------------|
| `REDIS_URL` | `redis://redis:6379/0` | URL de conexión a Redis |
| `RABBITMQ_URL` | `amqp://guest:guest@rabbitmq:5672/` | URL de conexión a RabbitMQ |
| `NCT_HEALTH_URL` | `http://coordinator:8080/health` | URL de health del NCT |
| `TRP_HEALTH_URL` | (eliminado) | El TrP fue eliminado; la fragmentación la hace cada Pool Coordinator internamente |
| `PORT` | `8000` | Puerto del servidor HTTP |

## Decisiones de diseño

- **Solo lectura de Redis**: la API no escribe estado; lee de Redis y publica propuestas a RabbitMQ. El NCT es la única fuente de verdad para escrituras.
- **SSE polling**: implementación simple con polling cada 2 segundos a Redis. En producción podría reemplazarse por Redis Pub/Sub o notificaciones del NCT.
- **CORS configurado para localhost**: permite desarrollo local con frontend en puerto 4200. En producción debe ajustarse.
- **Métricas Prometheus**: middleware automático que captura duración y conteo de requests por ruta y método.
- **Modelos Pydantic**: validación automática de requests/responses y documentación OpenAPI generada automáticamente (`/docs`).
- **Lifespan manager**: maneja conexión a Redis y tarea de fondo SSE en startup/shutdown.
