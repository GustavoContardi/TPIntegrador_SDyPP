# TODO: "Register Worker" no crea un pod real en el cluster GPU del profesor

> Escrito el 2026-07-13 al cierre de una sesión de Claude Code, para que una sesión nueva (sin memoria de
> esta conversación) pueda retomar el problema sin tener que re-investigar todo desde cero. Leer completo
> antes de tocar código — ya está diagnosticada la causa raíz con file:line, falta decidir e implementar
> la solución.

## Contexto rápido del proyecto

VoxChain (`pilar2-distribuido/`) es una blockchain de gobernanza simulada (TP de Sistemas Distribuidos).
Los "workers" son procesos que minan Proof-of-Work para promulgar/derogar leyes. Pueden correr en 3 modos:
`standalone`, `pool-coordinator`, `pool-worker`. El frontend Angular (`voxchain-frontend/`) tiene una
página "Workers Management" (`src/app/features/workers/workers.component.ts`) con:

- **Register Worker**: registra un worker nuevo y (en teoría) lo despliega en el cluster.
- **Switch Mode**: cambia el modo de un worker ya corriendo.
- **Configure Policy**: política de voto de un pool coordinator (accept/reject por action y/o law_id —
  esto se agregó en la sesión anterior y ya funciona, no es parte de este TODO).

## Topología real de despliegue (`pilar3-despliegue/`)

Son **dos clusters de Kubernetes separados**, conectados solo por red (ver `pilar3-despliegue/README.md`
líneas 6-8):

```
GCP — GKE (Cluster 1)                    GPU Cluster — k3s (Cluster 2, del profesor)
Namespace: voxchain                       Namespace: g-git-push-cv
- voxchain-api, NCT, Redis, RabbitMQ,     - Workers GPU (Deployments fijos, ver
  frontend                                  demo-deployments.yaml / worker-deployment.yaml)
```

- `voxchain-api` corre en GKE, namespace `voxchain` (`pilar3-despliegue/kubernetes/applications/voxchain-api-deployment.yaml`).
- Los workers GPU corren en el cluster k3s del profesor, namespace `g-git-push-cv`, y se conectan a
  RabbitMQ/Redis de GKE vía IP de LoadBalancer externo (`pilar3-despliegue/kubernetes/gpu-cluster/worker-config.yaml`:
  `rabbitmq-host: "35.198.33.80"  # IP del LoadBalancer de RabbitMQ en GCP`).
- El comentario en `pilar3-despliegue/kubernetes/gpu-cluster/demo-deployments.yaml` (líneas 1-13) lo dice
  explícito: los workers reportan su estado a Redis y "el backend voxchain-api lee esas claves para servir
  el estado al frontend **sin necesidad de acceso directo al cluster k3s**". Es decir: por diseño,
  `voxchain-api` no tiene acceso a la API de Kubernetes del cluster del profesor.

## El problema

Dos operaciones distintas de la UI de Workers se comportan muy distinto:

### 1. Switch Mode (worker ya corriendo) → SÍ funciona, en local y en el cluster real

`POST /api/workers/{worker_id}/switch-mode` (`pilar2-distribuido/voxchain_api/routers/workers.py:409-469`)
**no toca Kubernetes en absoluto**: solo publica un mensaje `switch_mode` a RabbitMQ
(`publisher.messaging.publish_worker_command`, línea 427), que el propio worker consume
(`pilar2-distribuido/worker/main.py`, método `_handle_command` / `switch_mode`, líneas 145-152) y cambia de
modo en caliente sin reiniciar el proceso. Como RabbitMQ es compartido entre los dos clusters (vía el
LoadBalancer de la config de arriba), esto debería andar igual en el cluster real que en local, siempre que:
- el worker exista como pod real y esté corriendo (`running: true`),
- la ownership pase (`verify_worker_ownership`, `workers.py:240-258` — pubkey del caller matchea
  `worker:owner:<id>` en Redis, o está en el mapeo hardcodeado `OWNER_WORKERS_MAPPING`).

Los 5 workers demo (`demo-deployments.yaml`: valentin→worker-standalone, gustavo→worker-pool-coordinator,
matt/profesor1/profesor2→worker-pool-miner-{1,2,3}) y los fijos de `worker-deployment.yaml` entran en este
caso: **switch-mode debería funcionar sobre ellos en el cluster real sin cambios de código.**

### 2. Register Worker (worker nuevo) → NO va a crear un pod real, ni en local ni en el cluster del profesor

`POST /api/workers/register` (`workers.py:546-594`) solo guarda metadata en Redis (`registered_workers`,
`worker:owner:<id>`, `worker:pubkey:<id>`) y, si viene una private key, llama a
`_spawn_k8s_worker(worker_id, private_key)` (`workers.py:70-174`), que intenta crear un `V1Secret` +
`V1Deployment` vía la librería `kubernetes` de Python.

**En local (docker-compose)**: confirmado en los logs reales del contenedor `voxchain-api` que no hay
config de Kubernetes disponible → `K8S_ENABLED=False` → `_spawn_k8s_worker` es un no-op
(`workers.py:71-73`, log: "Kubernetes is not enabled, skipping dynamic spawner."). El worker registrado
queda para siempre con `mode: "unknown", running: false` (fallback #3 en `GET /api/workers/status`,
`workers.py:333-344`), así que en la tabla el botón "Switch Mode" queda deshabilitado
(`workers.component.ts`, `[disabled]="!worker.running || !isWorkerOwned(worker)"`) — **esto es esperado y
está bien así en local, no hace falta arreglarlo para el entorno de desarrollo.**

**En el cluster real (GKE + k3s del profesor)**, aunque `voxchain-api` sí correría dentro de un cluster
real (K8S_ENABLED probablemente pasaría a `True` automáticamente, porque cualquier pod tiene montado un
token de service account), esto **tampoco va a funcionar**, por dos motivos de fondo:

1. **Cluster/namespace equivocado**: `NAMESPACE` se lee de
   `/var/run/secrets/kubernetes.io/serviceaccount/namespace` in-cluster (`workers.py:27-32`), que en GKE
   sería `"voxchain"` (donde vive el propio pod de la API), no `"g-git-push-cv"` (el namespace del cluster
   k3s del profesor). Como el kubeconfig in-cluster apunta al API server de **GKE**, el
   `create_namespaced_deployment` terminaría intentando crear el Deployment en el cluster GKE, no en el
   k3s del profesor.
2. **GKE no tiene nodos GPU**: aunque lograra crearse ahí, el container pide
   `resources.limits: {"nvidia.com/gpu": "1"}` (`workers.py:124-126`). Según `pilar3-despliegue/README.md`
   línea 177, GKE es "regional (1 nodo/AZ) para HA del plano de control" — sin nodepool GPU (las GPUs están
   solo en el k3s separado, línea 181). El pod quedaría en `Pending` para siempre por falta de ese recurso.

### Pista de un diseño no terminado: `backend-proxy` / ConfigMap `worker-modes`

Existe un RBAC ya escrito para un componente llamado `backend-proxy`
(`pilar3-despliegue/kubernetes/gpu-cluster/backend-proxy-rbac.yaml`) con permisos acotados:
`deployments/scale` (get, patch), `pods` (list, get, delete), `configmaps` con `resourceNames:
["worker-modes"]` (get, update, patch) — todo en namespace `g-git-push-cv` (el del cluster k3s correcto).

También existe `worker-modes-configmap.yaml` y el propio worker la lee al arrancar
(`pilar2-distribuido/worker/main.py`, método `_read_mode_from_configmap`, líneas 124-138): si hay una
entrada `<worker_id>: <mode>` en el ConfigMap, la usa como modo inicial en vez de la env var `WORKER_MODE`.

**Pero nada en el código actual escribe en ese ConfigMap.** `switch_worker_mode` (`workers.py:409-469`)
nunca lo toca — solo RabbitMQ + Redis. Y no existe ningún servicio/deployment llamado `backend-proxy` en
todo el repo (`grep -rn "backend-proxy" --include="*.yaml" --include="*.py"` solo encuentra el RBAC).
O sea: **el RBAC de `backend-proxy` es código muerto de un diseño que quedó a medio hacer** —
probablemente la idea original era que un componente corriendo *dentro* del cluster k3s (con permisos
acotados, sin necesitar acceso cross-cluster) hiciera: patchear el ConfigMap con el nuevo modo + borrar el
pod para forzar su reinicio y que relea el modo — dándole persistencia al switch de modo entre reinicios
del pod (cosa que el mecanismo actual de RabbitMQ no tiene: si el pod se reinicia por cualquier motivo,
vuelve al `WORKER_MODE` fijo de su Deployment, perdiendo el modo que se había seteado a mano).

## Qué hay que decidir mañana

Dos preguntas separadas, no asumir la respuesta — preguntarle al usuario (Gustavo) antes de escribir código:

### Pregunta 1: ¿Hace falta que "Register Worker" spawee un pod GPU real en el cluster del profesor?

Si la respuesta es que no (el profesor ya pre-provisionó los workers fijos vía `demo-deployments.yaml` y
alcanza con eso para la demo/entrega), **no hay nada que arreglar** — dejar "Register Worker" como está
(solo bookkeeping en Redis) o directamente ocultar/deshabilitar ese botón en el cluster real con un mensaje
claro tipo "el spawn dinámico no está soportado en este despliegue, contactá al admin del cluster para
agregar un worker nuevo".

Si la respuesta es sí, hace falta:
1. Darle a `voxchain-api` credenciales específicas para el cluster k3s (el código ya prioriza
   `KUBECONFIG` env var sobre in-cluster config, `workers.py:38-43` — habría que montar un kubeconfig del
   k3s como Secret y setear esa env var en `voxchain-api-deployment.yaml`).
2. Corregir `NAMESPACE` para que use `"g-git-push-cv"` cuando se conecta al cluster k3s (hoy se pisa con
   el namespace del propio pod si existe el archivo in-cluster — hay que revisar la lógica de
   `workers.py:27-32` para que no confunda "namespace del pod de la API" con "namespace destino del
   Deployment a crear").
3. Ampliar el RBAC del service account que use `voxchain-api` (o el de `backend-proxy` si se decide usar
   ese patrón) para que incluya `create` sobre `secrets` y `deployments` en `g-git-push-cv` — hoy
   `backend-proxy-rbac.yaml` NO lo tiene, solo `scale`/`get`/`patch`/`delete`.
4. Confirmar que el nodepool GPU del k3s tenga capacidad disponible antes de intentar spawnear.

### Pregunta 2: ¿Vale la pena persistir el modo del worker entre reinicios (usar el ConfigMap `worker-modes`)?

Si sí: conectar `switch_worker_mode` (`workers.py:409`) para que, además de publicar el comando RabbitMQ,
patchee `worker-modes[worker_id] = target_mode` vía la API de k8s — mismo problema de cross-cluster que
arriba, así que probablemente conviene resolverlo junto con la Pregunta 1 (mismo mecanismo de acceso al
cluster k3s).

Si no vale la pena (el switch en caliente vía RabbitMQ alcanza y los pods no se reinician seguido durante
la demo), **borrar el RBAC/ConfigMap muerto** (`backend-proxy-rbac.yaml`, `worker-modes-configmap.yaml`,
y la lectura en `worker/main.py:_read_mode_from_configmap`) para no dejar código/infra fantasma en el repo.

## Cómo verificar el estado actual sin tocar nada

```bash
# 1. Confirmar que no hay confusión de cluster: ver en qué namespace vive voxchain-api en el cluster real
kubectl config get-contexts   # confirmar contexto GKE vs k3s
kubectl get pods -n voxchain -l app=voxchain-api   # (contexto GKE)
kubectl get pods -n g-git-push-cv                   # (contexto k3s del profesor)

# 2. Ver si K8S_ENABLED terminó siendo True en el pod real de voxchain-api
kubectl logs -n voxchain deploy/voxchain-api | grep -i kubernetes

# 3. Confirmar que backend-proxy nunca se deployó (debería no encontrar nada)
kubectl get deploy,svc -n g-git-push-cv | grep -i backend-proxy
```

## Archivos relevantes (para no tener que re-buscarlos)

- `pilar2-distribuido/voxchain_api/routers/workers.py` — endpoints register/switch-mode/`_spawn_k8s_worker`.
- `pilar2-distribuido/worker/main.py` — `_read_mode_from_configmap`, `_handle_command`/`switch_mode`.
- `pilar2-distribuido/voxchain-frontend/src/app/features/workers/workers.component.ts` — UI.
- `pilar3-despliegue/README.md` — diagrama de los dos clusters (líneas 6-8, 177, 181).
- `pilar3-despliegue/kubernetes/applications/voxchain-api-deployment.yaml` — deployment de la API en GKE.
- `pilar3-despliegue/kubernetes/gpu-cluster/` — todo el overlay del cluster k3s (worker-deployment.yaml,
  demo-deployments.yaml, worker-modes-configmap.yaml, worker-rbac.yaml, backend-proxy-rbac.yaml,
  worker-config.yaml, kustomization.yaml).

## Una vez resuelto

Borrar este archivo (o mover su contenido a `pilar3-despliegue/README.md` como sección permanente si el
fix queda incompleto y hay que retomarlo en el futuro).
