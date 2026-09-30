# Pilar 3 — Despliegue, prueba y escalabilidad

## Arquitectura

```
GCP — GKE zonal, southamerica-east1-a      Clúster GPU — k3s (externo)
┌──────────────────────────────┐           ┌──────────────────────────────┐
│ Namespace: voxchain          │           │ Namespace: g-git-push-cv     │
│                              │   AMQPS   │                              │
│ RabbitMQ (STS ×3) ─ LB:5671 ─┼──────────►┼ mineros de los ciudadanos    │
│                              │(CA propia)│ (los levanta el alta desde   │
│ Redis + Sentinel (STS ×3+3)  │           │ la UI)                       │
│ HAProxy ×2 ── LB:6379 ───────┼──────────►┼ (estado worker:status:*)     │
│                              │  (sin TLS)│ - CPU por defecto            │
│ NCT primary + standby        │           │ - GPU opt-in por pod         │
│ voxchain-api ×2              │           │ - CA de RabbitMQ montada     │
│ voxchain-frontend ×2         │           │                              │
│ Ingress nginx + cert-manager │           │                              │
├──────────────────────────────┤           │                              │
│ Namespace: monitoring        │           │                              │
│ Prometheus+Grafana+Alertmgr  │           │                              │
└──────────────────────────────┘           └──────────────────────────────┘
```

Diagrama completo: [`docs/diagrams/ArquitecturaVoxchain.png`](../docs/diagrams/ArquitecturaVoxchain.png).

- **GKE** corre la infraestructura (Redis, RabbitMQ) en el node pool `infra`
  y las aplicaciones (NCT, API, frontend) en el node pool `apps`.
- **k3s** es un clúster ajeno. Los workers salen de ahí hacia GKE por dos
  LoadBalancer: **RabbitMQ por AMQPS** (5671, con CA propia) para desafíos y
  nonces, y **Redis** (6379, con contraseña y **sin TLS**) para reportar su
  estado.
- El pipeline `04` prepara el namespace, los secretos y los ConfigMaps, pero no
  despliega mineros: los levanta el alta desde la UI. `worker-deployment.yaml` +
  `worker-hpa.yaml` (2→10 por CPU) y `pool-miner-*` son alternativas que se
  aplican a mano.
- Cada alta crea en el k3s un **Secret** (tokens de enrolamiento, montado en el
  pod como volumen para que el API pueda reponer uno a un pod que reemplaza a
  otro), un
  **Deployment** y un **Service** (`worker-svc-<id>`, puerto 9001). El Service
  es la dirección estable del minero si coordina un equipo: sus miembros le
  piden trabajo por ese nombre y no por la IP del pod, que cambia cada vez que
  el pod se reemplaza. Si la cuenta del kubeconfig no puede crear Services, el
  alta sigue y el minero anuncia la IP de su pod, como antes.
- Al **fundar un equipo**, el API escala el Deployment de su coordinador a
  `TEAM_COORDINATOR_REPLICAS` (env del API, default 2) y le deja en el Secret un
  segundo token de enrolamiento para la réplica; al **disolverlo** lo vuelve a
  1. Las dos réplicas se reparten por el lease del pool: una manda y la otra
  espera. La readinessProbe va en `/ready`, que da 503 en la que espera, así
  que el Deployment muestra **1/2 disponible** de forma permanente: es a
  propósito, y el Service sólo enruta a la que manda. Necesita permiso para
  `deployments/scale` y para parchear Secrets en el namespace; sin él, el
  equipo corre con un solo pod como antes, y un pod que reemplaza a otro mina
  sin dueño porque el API no puede reponerle el token.
- **GPU opt-in**: los manifests piden `nvidia.com/gpu` sólo si se descomenta el
  recurso junto con las variables `NVIDIA_*` (ver el comentario en
  `gpu-cluster/worker-deployment.yaml`). Sin eso, el minero detecta que no hay
  GPU utilizable y mina con CPU. `gpu-cluster/gpu-miner-deployment.yaml` es un
  minero standalone que ya pide la GTX 1060: aplicarlo y borrarlo es la prueba
  de ingreso/egreso de un nodo GPU.

## Guía de setup paso a paso

> Los pasos 1, 2 y 4 son manuales, una vez por entorno. El 3 lo hace `01-infra`
> desde el segundo despliegue (el primero es local), y los pasos 5 a 7 los hacen
> los pipelines `02`–`04` de `.github/workflows/`. La guía manual sirve para
> entenderlos o para un entorno sin CI. La bitácora de un despliegue real, con
> los problemas que aparecieron, está en
> [`docs/informe/despliegue-gcp.md`](../docs/informe/despliegue-gcp.md).

### Paso 1: Certs TLS autofirmados

```bash
cd pilar3-despliegue
./kubernetes/scripts/generate-certs.sh ./certs
```

Esto genera:
- `certs/ca.crt` → para los workers del k3s
- `certs/rabbitmq-cert.pem` + `certs/rabbitmq-key.pem` → para RabbitMQ

Detalle completo (SANs, qué se versiona y qué no, cómo validan los workers
contra la IP del LoadBalancer, limitaciones): **[`certs/README.md`](certs/README.md)**.

### Paso 2: Bootstrap del entorno (secretos, bucket e IP)

```bash
./kubernetes/scripts/bootstrap-secrets.sh            # reusa las contraseñas que ya existan
./kubernetes/scripts/bootstrap-secrets.sh --rotate   # regenera las contraseñas
```

Deja listo todo lo que tiene que existir **antes** del primer `tofu init` y
sobrevivir a un `tofu destroy`:

- **11 secretos en Secret Manager.** `rabbitmq-user`, `rabbitmq-pass`,
  `rabbitmq-erlang-cookie`, `rabbitmq-tls-crt`, `rabbitmq-tls-key`,
  `rabbitmq-ca-crt` y `redis-pass` son los que leen los `ExternalSecret` de
  `kubernetes/infrastructure/`. `loki-push-password` y `loki-push-htpasswd`
  son el basic auth del push de logs de los mineros del k3s (ver *Plataforma de
  logging*), y los leen los `ExternalSecret` de `kubernetes/monitoring/`.
  `alertmanager-discord-webhook` es el receptor de las alertas: no se genera,
  se pasa con `DISCORD_WEBHOOK_URL` (o el script lo pide) y, si falta, todo
  anda pero las alertas no notifican. `grafana-admin-password` lo consume
  OpenTofu (paso 3) y el secret `GRAFANA_ADMIN_PASSWORD` de `01-infra`.
- **Bucket del estado de OpenTofu** (`gs://voxchain-unlu-tfstate`, con
  versionado).
- **IP estática del Ingress** (`voxchain-ingress-ip`, hoy `35.199.68.144`). De
  ella sale el host `voxchain.<IP>.sslip.io`, y con él el certificado, la URL de
  Grafana y el `rpId` de las passkeys. Si la IP cambiara, las passkeys
  registradas dejarían de servir. Terraform sólo la lee. Mientras el clúster
  está apagado cuesta unos US$7 por mes.

Si en `certs/` no están los `*.pem`, genera una CA nueva con el script del paso
1. La clave privada de la CA no se versiona, así que en otra máquina eso
significa una CA nueva: hay que actualizar el secret `RABBITMQ_CA_CERT` (paso 4)
y volver a correr `04`.

Se corre **una vez por entorno**, a mano: para que lo hiciera el CI, la clave
privada de la CA y las contraseñas tendrían que vivir en el CI, y eso contradice
*zero static keys*. El pipeline `02` verifica que los secretos estén y falla con
un mensaje claro si falta alguno.

Después de `--rotate`, la versión anterior de cada secreto sigue siendo legible.
Para revocarla (es reversible, a diferencia de `destroy`):

```bash
gcloud secrets versions disable <versión> --secret <nombre> --project voxchain-unlu
```

### Paso 3: OpenTofu — crear el clúster GKE

```bash
cd terraform/gke
cp terraform.tfvars.example terraform.tfvars   # revisar project_id y github_repository
# sin default, a propósito: se lee de Secret Manager y no queda en el historial
export TF_VAR_grafana_admin_password="$(gcloud secrets versions access latest --secret grafana-admin-password --project voxchain-unlu)"
tofu init
tofu plan -out=plan.out
tofu apply plan.out   # ~15 min
rm plan.out           # guarda la contraseña de Grafana en texto plano
```

Crea:

- La VPC y el clúster GKE **zonal** (`southamerica-east1-a`) con Dataplane V2.
- Los node pools `infra` (1→2, con taint) y `apps` (2→3).
- Artifact Registry y Workload Identity Federation para GitHub Actions.
- Las service accounts. La de los nodos lleva
  `roles/container.defaultNodeServiceAccount`: sin ese rol, Cloud Logging
  rechaza los logs.
- Por Helm: External Secrets Operator, kube-prometheus-stack, ingress-nginx
  (con la IP del paso 2) y cert-manager.
- El namespace `voxchain`, más un RoleBinding que le da a la SA de CI el
  ClusterRole `admin` **sólo en ese namespace**. `roles/container.developer` no
  incluye RBAC, y sin esto `02` no puede crear el Role de peer discovery de
  RabbitMQ (ver [Decisiones de diseño](#decisiones-de-diseño)).

> **Migrar un clúster donde `voxchain` ya existe:** si el namespace se creó
> antes con `kubectl` (por ejemplo, lo creó el pipeline `02`), hay que
> importarlo antes del `apply`, o fallará con "already exists":
> `tofu import kubernetes_namespace_v1.voxchain voxchain`. En un entorno nuevo
> no hace falta.

Grafana:
- La contraseña del admin es la de `TF_VAR_grafana_admin_password`; no hay
  ninguna en el repo.
- Se publica por el Ingress de la app en `grafana.voxchain.<IP>.sslip.io`, a
  través del `ExternalName` de `monitoring/grafana-bridge-service.yaml`.
- Tiene un PVC de 10 Gi para que los dashboards persistan.
- Tiene a Loki como segundo datasource (uid `loki`); ver *Plataforma de logging*.

`tofu output` devuelve lo que se necesita para el paso 4
(`workload_identity_provider` e `infra_service_account`), además de
`artifact_registry`, `app_url`, `grafana_url`, `ingress_ip` y `get_credentials`
(el comando de kubectl).

> El estado de OpenTofu vive en `gs://voxchain-unlu-tfstate` (backend de
> `versions.tf`), que crea el script del paso 2. Este primer `apply` tiene que
> ser local: la SA `voxchain-infra` y el pool de WIF que usa `01-infra` los crea
> él mismo. Los siguientes pueden correr desde CI con `01-infra`.

### Paso 4: Configurar los secrets de GitHub Actions

| Secret | Valor |
|--------|-------|
| `GCP_WIF_PROVIDER` | `workload_identity_provider` de `tofu output` |
| `GCP_SERVICE_ACCOUNT` | `voxchain-cicd@voxchain-unlu.iam.gserviceaccount.com` (pipelines 02-04) |
| `GCP_INFRA_SERVICE_ACCOUNT` | `infra_service_account` de `tofu output` (sólo `01-infra`) |
| `GRAFANA_ADMIN_PASSWORD` | `gcloud secrets versions access latest --secret grafana-admin-password --project voxchain-unlu` (la inyecta `01-infra`) |
| `K3S_KUBECONFIG` | kubeconfig del clúster k3s, en base64 |
| `RABBITMQ_USER` | `voxchain-worker` |
| `RABBITMQ_PASS` | `gcloud secrets versions access latest --secret rabbitmq-pass --project voxchain-unlu` |
| `RABBITMQ_CA_CERT` | contenido de `certs/ca.crt` |

Ninguno es una llave de GCP: los workflows se autentican por **Workload
Identity Federation** (OIDC). La única credencial estática es el kubeconfig del
k3s, porque es un clúster ajeno.

Los valores sensibles se pasan con un pipe, para que no queden en pantalla ni
en el historial de la shell:

```bash
gcloud secrets versions access latest --secret rabbitmq-pass --project voxchain-unlu | gh secret set RABBITMQ_PASS
```

Además, dos **variables** del repo (Settings → Secrets and variables → Actions →
Variables):

| Variable | Valor |
|----------|-------|
| `CLOUD_ENABLED` | `true` mientras la infraestructura esté desplegada. Sin ella, un push no dispara `02`–`04` (se saltean en vez de fallar contra un WIF inexistente); a mano corren siempre |
| `K3S_EGRESS_CIDRS` | IP de salida del k3s en CIDR (`x.x.x.x/32`, varias separadas por coma). `02` acota con ella los LoadBalancer de Redis y RabbitMQ; sin ella quedan abiertos a internet y el job lo advierte |

### Paso 5: Imágenes

Los manifests ya apuntan a
`southamerica-east1-docker.pkg.dev/voxchain-unlu/voxchain-images/`. El pipeline
`03` construye las 5 imágenes (`nct`, `worker`, `worker-gpu`, `voxchain-api`,
`voxchain-frontend`), las sube con el tag del commit y con `latest`, y fija los
Deployments al SHA del commit. En otro proyecto de GCP hay que reemplazar ese
prefijo en los `*.yaml` de `kubernetes/`.

### Paso 6: Aplicar los manifests en GKE

Lo hacen `02-services` (infraestructura) y `03-apps` (aplicaciones). A mano,
desde la raíz del repo:

```bash
gcloud container clusters get-credentials voxchain \
  --zone southamerica-east1-a --project voxchain-unlu

kubectl apply -f pilar3-despliegue/kubernetes/namespace.yaml
kubectl apply -f pilar3-despliegue/kubernetes/infrastructure/
kubectl apply -f pilar3-despliegue/kubernetes/cert-manager/
kubectl apply -f pilar3-despliegue/kubernetes/applications/
kubectl apply -f pilar3-despliegue/kubernetes/hpa/
kubectl apply -f pilar3-despliegue/kubernetes/monitoring/
kubectl get pods -n voxchain
```

`02-services` además crea el secreto `k3s-kubeconfig`, que usa la API para dar de
alta mineros en el k3s desde la web, y el usuario `voxchain-worker` en RabbitMQ.

Los hosts del Ingress (`voxchain-ingress.yaml`), `WEBAUTHN_RP_IDS` y
`WEBAUTHN_ORIGINS` (`voxchain-config.yaml`) y los ClusterIssuers llevan la IP
estática del paso 2 en formato `sslip.io`. **No cambia al redesplegar**: sólo
hay que tocarlos en un proyecto nuevo, o si se libera `voxchain-ingress-ip`. La
URL de Grafana la arma Terraform a partir de la misma IP.

Para verificar que RabbitMQ formó **un solo** clúster: si falla el peer
discovery, los tres pods quedan `Ready` pero como tres brokers separados (pasó
en el redespliegue de septiembre, ver
[`despliegue-gcp.md` §8.7](../docs/informe/despliegue-gcp.md)):

```bash
kubectl exec -n voxchain rabbitmq-0 -- rabbitmqctl cluster_status   # 3 running nodes
```

Todo el despliegue se verifica desde afuera con:

```bash
curl https://voxchain.35.199.68.144.sslip.io/api/health
```

### Paso 7: Desplegar los workers en el k3s

Lo hace `04-gpu-workers`. A mano, desde la raíz del repo; las tres primeras
líneas van contra GKE:

```bash
RMQ_IP=$(kubectl get svc rabbitmq-external -n voxchain -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
REDIS_IP=$(kubectl get svc redis-external -n voxchain -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
REDIS_PASS=$(kubectl get secret redis-credentials -n voxchain -o jsonpath='{.data.password}' | base64 -d)

# a partir de acá, contra el k3s
export KUBECONFIG=~/.kube/k3s.yaml
kubectl create configmap worker-config -n g-git-push-cv \
  --from-literal=rabbitmq-host="$RMQ_IP" --from-literal=redis-host="$REDIS_IP"
kubectl create secret generic rabbitmq-ca -n g-git-push-cv \
  --from-file=ca.crt=pilar3-despliegue/certs/ca.crt
kubectl create secret generic rabbitmq-credentials -n g-git-push-cv \
  --from-literal=username=voxchain-worker --from-literal=password="<RABBITMQ_PASS>"
kubectl create secret generic redis-credentials -n g-git-push-cv \
  --from-literal=password="$REDIS_PASS"

kubectl apply -f pilar3-despliegue/kubernetes/gpu-cluster/worker-modes-configmap.yaml
# los mineros los levanta el alta desde la UI; a mano: worker-deployment.yaml
```

En el k3s del profesor el namespace ya existe y nuestra ServiceAccount no
puede crear Roles: `worker-rbac.yaml` y `backend-proxy-rbac.yaml` se aplican
best-effort y los pods corren con la SA `default`.

Los mineros de altas anteriores a la creación de Services siguen anunciando la
IP de su pod: para pasarlos al esquema nuevo hay que darlos de baja y volver a
registrarlos.

> No se aplica `kustomization.yaml` completo: los tres `*-secret.yaml` del
> directorio son plantillas con valores vacíos, y aplicarlos pisaría los
> secretos reales.

### Paso 8: Pruebas de carga

```bash
# contra un entorno local o un port-forward de la API
./pilar3-despliegue/load-tests/scenarios/run_all.sh http://localhost:8000

# contra el despliegue real: ajusta N_ZEROS y FRAGMENT_SIZE entre corridas
./pilar3-despliegue/load-tests/scenarios/run_all_cloud.sh https://<host>

# N transacciones con M vs 2xM mineros (local, Docker Compose)
./run.sh scale
```

Resultados en `load-tests/resultados/`; análisis en el
[informe](../docs/informe/INFORME.md), sección 4.

### Paso 8.bis: Calibración de la dificultad

En el despliegue `n` es **dinámico** (`DYNAMIC_DIFFICULTY=true`, AGENT.md 11.3):
el NCT lo recalcula para cada ley según el cómputo vivo, para que promulgar
cueste unos `DIFFICULTY_TARGET_SECONDS` (60 s). También deriva `NONCE_SPACE` de
ese `n` y lo publica en el desafío. `N_ZEROS` pasa a ser el valor inicial.

Con `DYNAMIC_DIFFICULTY=false` `n` vuelve a ser fijo, y ahí **no es una perilla
suelta**: `N_ZEROS`, `NONCE_SPACE` y `WINDOW_SECONDS_*` se mueven juntos. El
espacio tiene que cubrir la derogación (`n+1`) o las ventanas vencen sin sellar,
y en los logs parece falta de mineros. El NCT avisa por log si la combinación es
incoherente, al arrancar y antes de abrir cada ventana. `WINDOW_SECONDS_*` sigue
siendo el techo del plazo también en modo dinámico.

Referencia con el minero CPU a ~954 kH/s (`load-tests/scenarios/bench_hardware.py`):

| `n` | promulgar | derogar (`n+1`) | `NONCE_SPACE` necesario |
|-----|-----------|-----------------|-------------------------|
| 4   | 0,1 s     | 1,1 s           | 5 M                     |
| 5   | 1,1 s     | 17,6 s          | 78 M                    |
| 6   | 17,6 s    | 281 s           | 1.240 M  ← `N_ZEROS` inicial |
| 7   | 281 s     | 4.501 s         | 19.800 M                |

Con GPU en la población estos tiempos caen por órdenes de magnitud: medir con
`bench_hardware.py` en el nodo GPU.

## CI/CD

Los workflows están en `.github/workflows/`:

| Workflow | Disparo | Qué hace |
|---|---|---|
| `ci-checks.yml` | push y PR a `main` y `dev` | gitleaks (falla ante un secreto hardcodeado) + pytest |
| `01-infra.yml` | manual (`plan` / `apply`) | OpenTofu |
| `02-services.yml` | push a `main` en `kubernetes/infrastructure/`, `cert-manager/` o `namespace.yaml` | verifica los secretos del paso 2, despliega Redis y RabbitMQ, los ClusterIssuer, el kubeconfig del k3s y el usuario de los workers |
| `03-apps.yml` | push a `main` en `pilar2-distribuido/`, `pilar1-minero/gpu/`, o en los manifests de `applications/`, `hpa/` y `monitoring/` | build y push de las 5 imágenes, apply de los manifests y pin al SHA |
| `04-gpu-workers.yml` | push a `main` en `kubernetes/gpu-cluster/` | workers en el k3s |

```
PR / push → ci-checks (gitleaks + pytest)
              ↓ (merge a main)
    ┌─────────┼──────────────┬───────────────┐
    │         │              │               │
01-infra   02-services    03-apps       04-gpu-workers
(manual)   (redis+rmq)  (build+deploy)   (k3s deploy)
```

Todos se autentican contra GCP por Workload Identity Federation: `01` con la SA
`voxchain-infra` (sólo ese workflow, desde `main`, puede asumirla) y `02`–`04`
con `voxchain-cicd`. Con la infraestructura dada de baja no hay pool de WIF
contra el cual autenticar; por eso `02`–`04` sólo corren por push si la variable
`CLOUD_ENABLED` vale `true`.

## Componentes

| Directorio       | Contenido |
|------------------|-----------|
| `kubernetes/`    | Manifests K8s: `infrastructure/`, `applications/`, `hpa/`, `monitoring/`, `cert-manager/`, `gpu-cluster/` (k3s) y `scripts/` |
| `terraform/gke/` | Infraestructura como código con OpenTofu |
| `certs/`         | CA pública del canal AMQPS y su documentación |
| `load-tests/`    | Pruebas de carga (bulk, dificultad, fragmentación, recursos) y resultados |
| `.secrets/`      | No se usa: resto de una idea de SOPS + Age (ver su README) |

## Decisiones de diseño

- OpenTofu declarativo para reproducibilidad.
- **GKE zonal** (`southamerica-east1-a`) con nodos públicos: más barato que uno
  regional y sin NAT. El costo es que el plano de control no tiene HA.
- Nodepool `infra` con taint para aislar Redis/RabbitMQ; nodepool `apps` con
  autoscaling para NCT, API y frontend.
- RabbitMQ con TLS de CA propia (AMQPS, 5671) para los workers externos.
- Workers en un k3s separado, conectados por LoadBalancer.
- External Secrets Operator en GKE para sincronizar los secretos de GCP Secret
  Manager, por Workload Identity.
- Workload Identity Federation para CI/CD (sin llaves estáticas de GCP).
- **Permisos del CI acotados por namespace**: la SA `voxchain-cicd` tiene
  `roles/container.developer` en IAM (todo el API de Kubernetes menos RBAC) y el
  ClusterRole `admin` sólo en `voxchain`, vía un RoleBinding de Terraform. Así
  puede crear los Roles de sus propias apps sin tener `container.admin` sobre
  todo el clúster. La prevención de escalada de Kubernetes le impide otorgar
  permisos que no tiene.
- **IP del Ingress estática y fuera del estado de OpenTofu**: el host
  `sslip.io` ata el certificado, Grafana y las passkeys. Si la IP fuera un
  recurso de Terraform, `destroy` la liberaría. Con `prevent_destroy`, el
  `destroy` fallaría.
- **Autoscaling**: HPA por CPU al 70% (`api-hpa` 2→5; `worker-hpa` 2→10 y
  `pool-miner-hpa` 1→10 en el k3s) y Cluster Autoscaler de GKE sobre los node
  pools. No hay HPA por métricas específicas.
- **Observabilidad (U5.5)**: kube-prometheus-stack (Prometheus + Grafana +
  Alertmanager) desplegado vía Helm en el namespace `monitoring`. Cada servicio
  expone `/metrics` con métricas de aplicación (propuestas, bloques, workers,
  latencia). ServiceMonitors para el auto-descubrimiento, 5 reglas de alerta
  propias y el dashboard precargado en un ConfigMap.
- **Alertas a Discord**: Alertmanager manda las alertas de VoxChain
  (`alertname` Voxchain\*) a un canal de Discord por webhook, con aviso al
  disparar y al resolverse (`monitoring/alertmanager-config.yaml`, un
  `AlertmanagerConfig`). Las del chart siguen visibles en Alertmanager y
  Grafana pero no notifican: en GKE varias disparan siempre, porque el control
  plane no se puede scrapear, y llenarían el canal. La URL del webhook es un
  secreto: se carga con `DISCORD_WEBHOOK_URL=... bootstrap-secrets.sh` y llega
  por External Secrets. Si falta, el operador descarta el `AlertmanagerConfig`
  y Alertmanager sigue con la config del chart, sin notificar (02 lo avisa).
  Para verificarlo en la demo: `kubernetes/scripts/probar-alerta.sh` dispara
  `VoxchainPrueba`, que llega en unos 30 s y se resuelve a los 5 min.
- **Alta disponibilidad de Redis**: Sentinel (×3, quórum 2) promueve una
  réplica cuando el master cae, y **HAProxy** (×2, `infrastructure/redis-haproxy.yaml`)
  es la dirección estable del master: chequea cada segundo qué pod responde
  `role:master` y manda todo ahí. El Service `redis` (API y NCT) y
  `redis-external` (mineros del k3s) apuntan a HAProxy, así que ningún cliente
  necesita hablar con Sentinel. Antes todos apuntaban a `redis-0` fijo y un
  failover no servía de nada. Además:
  - Redis y Sentinel arrancan preguntando quién es el master, en vez de asumir
    `redis-0`. Un master que vuelve después de un failover lo hace como réplica,
    y no queda un segundo master.
  - El `myid` de cada Sentinel sale del nombre del pod. Con uno al azar, cada
    reinicio dejaba en los otros un par fantasma que cuenta para la mayoría, y a
    los pocos reinicios el failover se volvía imposible. `02-services` limpia los
    que hayan quedado.
  - Los clientes de Python reintentan ante conexión cortada y ante `READONLY`,
    así que un failover se ve como una pausa de unos 10 s.

  Se probó con los scripts y la configuración de los manifests, sin cambios,
  en Docker: dos failovers seguidos, el master viejo que vuelve con otra IP,
  los Sentinel reiniciados de a uno y todos a la vez. El cliente no vio ningún
  error ni perdió escrituras confirmadas. Límite conocido: ante una partición de
  red en la que el master viejo sigue vivo pero aislado, puede aceptar
  escrituras que después se pierden. Evitarlo requiere `min-replicas-to-write`,
  que a cambio corta las escrituras si no hay réplicas.
- **Métricas de los mineros**: un ServiceMonitor no descubre pods de otro
  clúster, así que Prometheus no alcanza el `/metrics` de los mineros del k3s.
  El API exporta `voxchain_miner_*` (vivo, hashrate, GPU, modo, capacidad) a
  partir del latido `worker:status:*` que cada minero deja en Redis
  (`voxchain_api/services/miner_metrics.py`), y Prometheus lo recoge con el
  ServiceMonitor del API. Los gauges son el valor del último latido. Los
  contadores e histogramas de minería también viajan en el latido (campo
  `mining_stats`, acumulados desde que arrancó el minero) y el API los expone
  como counters e histogramas: `voxchain_miner_mining_tasks_total` y
  `voxchain_miner_mining_success_total` (tasa de éxito CPU vs GPU),
  `voxchain_miner_mining_duration_seconds` (tiempo por longitud de prefijo) y
  `voxchain_miner_challenge_latency_seconds` (latencia RabbitMQ → minero).
  Así funcionan `rate()` y `histogram_quantile()`, y un reinicio del minero se
  ve como el reset de un counter. Como el API tiene 2 réplicas, las consultas
  agregan primero con `max by (worker_id, ...)` y recién después suman.
- **Seguridad de contenedores**: todos los workloads corren con `securityContext`
  restrictivo — `runAsNonRoot` (uid 1000 apps, 999 Redis/RabbitMQ, 101 nginx),
  `allowPrivilegeEscalation: false`, `capabilities.drop: ALL` y seccomp
  `RuntimeDefault`. El frontend usa `nginx-unprivileged` (puerto 8080 no
  privilegiado). Los logs a disco van a un `emptyDir` montado en
  `/var/log/voxchain`. NCT, API, frontend y mineros corren con
  `readOnlyRootFilesystem: true`: sólo `/tmp` y los logs (`emptyDir`) son
  escribibles.
- **Separación de workloads**: el nodepool `infra` tiene taint
  `pool=infra:NoSchedule` y label `pool=infra` (Terraform). Redis, Sentinel y
  RabbitMQ declaran `nodeSelector` + `tolerations` para schedulearse allí, y
  anti-affinity *preferred* para repartir las réplicas entre nodos; los
  workloads de aplicación/minería quedan en el nodepool `apps` (sin toleration,
  el taint los excluye de `infra`).
- **Segmentación de red**: el clúster usa Dataplane V2, que aplica las
  NetworkPolicies de `infrastructure/` (Redis y RabbitMQ). Los LoadBalancer
  externos usan `externalTrafficPolicy: Local` y `02-services` los acota con
  `loadBalancerSourceRanges` a la variable del repo `K3S_EGRESS_CIDRS` (la IP de
  salida del k3s, separada por comas si son varias). Para averiguarla:
  `kubectl run -n g-git-push-cv egress --rm -it --restart=Never --image=curlimages/curl -- curl -s ifconfig.me`.
  Redis sigue sin TLS (deuda, informe §7.2). Rollback si algo deja de conectar
  tras un redespliegue: `kubectl delete networkpolicy -n voxchain --all`.
- **Registry público de lectura**: el k3s ajeno hace pull sin
  `imagePullSecrets`, que exigirían mandarle una llave estática de GCP. Las
  imágenes no contienen secretos.

## Plataforma de logging (colector de N servicios × M réplicas)

La plataforma de logs propia es **Loki + Grafana Alloy**, instalada por
OpenTofu (`helm_release.loki` y `helm_release.alloy` en `terraform/gke/main.tf`,
con los values en `terraform/gke/helm-values/`). Loki es además un datasource
de Grafana, así que logs y métricas se ven en el mismo lugar: el dashboard de
VoxChain tiene una fila **LOGS** con líneas por servicio y clúster, errores y
advertencias por servicio, y los últimos errores.

```
GKE:  pods de todos los namespaces ──(API de Kubernetes)──▶ Alloy ──▶ Loki ◀── Grafana
k3s:  mineros ──(HTTPS + basic auth, logs.voxchain.<IP>.sslip.io)──▶ Ingress ──▶ Loki
```

- **Loki** corre en modo monolítico (un StatefulSet, un PVC de 10 GiB) con 7
  días de retención, lo mismo que Prometheus. El modo distribuido y un bucket
  de objetos se justifican con cientos de GB por día; acá son MB. Desde marzo de
  2026 el chart OSS lo mantiene `grafana-community`.
- **Alloy** es el colector de GKE: un Deployment que lee por el API de
  Kubernetes (`loki.source.kubernetes`) el stdout/stderr de **todos los pods de
  todas las réplicas** (API ×2, NCT ×2, RabbitMQ ×3, Redis ×3+3, frontend ×2,
  ingress-nginx, etc.), con labels `namespace`, `pod`, `container`, `app` y
  `cluster="gke"`. De las líneas JSON de nuestros servicios saca `service` y
  `level`. Al leer por el API no necesita `hostPath`, root ni tolerations para
  el node pool de infra. Corre sin root, con el root filesystem de sólo lectura,
  y su ClusterRole sólo puede leer pods, sus logs y namespaces.
- **Mineros del k3s:** ahí no se puede desplegar un colector, porque nuestra
  cuenta no puede crear Roles ni ServiceAccounts. Cada minero manda sus
  registros directo a Loki desde el proceso (`LokiHandler` en
  `common/logging_setup.py`), en lotes y desde un hilo aparte. Si Loki no
  responde, los descarta y los cuenta: nunca frena la minería. Llegan con
  `cluster="k3s"`, `service`, `level` y `pod`, los mismos nombres que pone
  Alloy. Entran por el Ingress `loki-push` (`kubernetes/monitoring/`), que
  publica **sólo** el endpoint de push, con TLS de Let's Encrypt y basic auth.
  Las consultas no salen por ahí: se hacen desde Grafana, por adentro.
- **Credenciales del push:** `bootstrap-secrets.sh` genera la contraseña
  (`loki-push-password`) y su hash htpasswd (`loki-push-htpasswd`) en Secret
  Manager. External Secrets los sincroniza: el hash en `monitoring`, para el
  Ingress, y la contraseña en `voxchain`, de donde `04-gpu-workers` la copia al
  k3s, como hace con la de Redis. En los manifests de los mineros la URL y la
  contraseña son opcionales: sin ellas el minero arranca igual y loguea como
  antes.

**Cloud Logging de GKE sigue activo** en paralelo: un Fluent Bit gestionado por
nodo manda el mismo stdout a Logs Explorer. Para eso la SA propia de los nodos
necesita `roles/container.defaultNodeServiceAccount` (está en el Terraform).

La capa de aplicación, desde `common/logging_setup.py`:

- **Memoria/stdout**: handler de consola con formato **JSON estructurado**
  (`timestamp`, `level`, `logger`, `service`, `message`, `exception`). Es lo
  que parsean Alloy y Cloud Logging para filtrar por servicio y severidad.
- **Disco**: `RotatingFileHandler` en `/var/log/voxchain/<servicio>.log`
  (5 MB × 3 backups, montado como `emptyDir`), cumpliendo "registros de
  actividades gestionados en memoria y disco".
- **Loki** (sólo con `LOKI_PUSH_URL`): el envío directo de los mineros del k3s.

Consultas típicas en Grafana → Explore → Loki:

```
{service="nct", level=~"WARNING|ERROR"}
{cluster="k3s", service="worker"} | json | message=~".*nonce.*"
sum by (service, cluster) (count_over_time({service=~".+"}[5m]))
```

La misma consulta en Logs Explorer de GCP:

```
resource.type="k8s_container"
resource.labels.namespace_name="voxchain"
jsonPayload.service="nct"
severity>=WARNING
```

## Sincronización de relojes (NTP)

No se despliega un daemon NTP propio: **los nodos de ambos clusters ya sincronizan
sus relojes vía NTP por defecto**, y los contenedores heredan el reloj del kernel
del nodo (no existe un reloj por contenedor):

- **GKE (Container-Optimized OS)**: `systemd-timesyncd`/`chrony` sincroniza contra
  el servidor NTP interno de Google (`metadata.google.internal`, respaldado por
  los relojes atómicos de Google con *leap smearing*).
- **k3s (nodos propios)**: `systemd-timesyncd` contra los pools NTP de la distro.

Verificación en un nodo: `timedatectl show -p NTPSynchronized` → `yes`.

Los puntos del sistema sensibles al tiempo (deadline de ventanas, TTL de leases
en Redis, épocas de elección del pool que usan `floor(time()/30)`) toleran el
desvío típico de NTP (≪1 s); además los TTL críticos los arbitra un único reloj
(el de Redis), no los relojes de los clientes.

Para verificarlo sin entrar a un nodo, `GET /api/health` mide el desfase entre
el reloj del pod de la API y el `TIME` de Redis (compensando la ida y vuelta):
`clock` es `"ok"` por debajo de 500 ms y `"skew"` por encima, y el valor medido
va en `clock_skew_ms`.
