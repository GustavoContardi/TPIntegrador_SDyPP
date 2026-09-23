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
│        └──────── LB:6379 ────┼──────────►┼ (estado worker:status:*)     │
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

Diagrama completo: [`docs/diagrams/arquitecturaVoxChain.jpeg`](../docs/diagrams/arquitecturaVoxChain.jpeg).

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
- **GPU opt-in**: los manifests piden `nvidia.com/gpu` sólo si se descomenta el
  recurso junto con las variables `NVIDIA_*` (ver el comentario en
  `gpu-cluster/worker-deployment.yaml`). Sin eso, el minero detecta que no hay
  GPU utilizable y mina con CPU. `gpu-cluster/gpu-miner-deployment.yaml` es un
  minero standalone que ya pide la GTX 1060: aplicarlo y borrarlo es la prueba
  de ingreso/egreso de un nodo GPU.

## Guía de setup paso a paso

> Los pasos 2 a 8 son lo que hacen los pipelines de `.github/workflows/`. La
> guía manual sirve para entenderlos o para un entorno sin CI.

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

### Paso 2: Subir los secretos a GCP Secret Manager

```bash
./kubernetes/scripts/bootstrap-secrets.sh            # crea los 7 secretos
./kubernetes/scripts/bootstrap-secrets.sh --rotate   # regenera las contraseñas
```

Crea `rabbitmq-user`, `rabbitmq-pass`, `rabbitmq-erlang-cookie`,
`rabbitmq-tls-crt`, `rabbitmq-tls-key`, `rabbitmq-ca-crt` y `redis-pass`, que son
los que leen los `ExternalSecret` de `kubernetes/infrastructure/`. Si faltan los
certificados, los genera con el script del paso 1. Se corre **una vez por
entorno**, a mano: para que lo hiciera el CI, la clave privada de la CA y las
contraseñas tendrían que vivir en el CI, y eso contradice *zero static keys*. El
pipeline `02` verifica que estén y falla con un mensaje claro si falta alguno.

### Paso 3: OpenTofu — crear el clúster GKE

```bash
cd terraform/gke
cp terraform.tfvars.example terraform.tfvars   # revisar project_id y github_repository
export TF_VAR_grafana_admin_password='...'     # sin default, a propósito
tofu init
tofu plan
tofu apply   # ~15 min
```

Crea VPC, clúster GKE **zonal** (`southamerica-east1-a`), node pools `infra`
(1→2, con taint) y `apps` (2→3), Artifact Registry, Workload Identity
Federation para GitHub Actions, las service accounts, y por Helm: External
Secrets Operator, kube-prometheus-stack, ingress-nginx y cert-manager.

Grafana:
- La contraseña del admin es la de `TF_VAR_grafana_admin_password`; no hay
  ninguna en el repo.
- Se publica por el Ingress de la app en `grafana.voxchain.<IP>.sslip.io`, a
  través del `ExternalName` de `monitoring/grafana-bridge-service.yaml`.
- Tiene un PVC de 10 Gi para que los dashboards persistan.

`tofu output` devuelve `workload_identity_provider` (para el paso 4),
`artifact_registry` y `get_credentials` (el comando de kubectl).

> El estado de OpenTofu es **local**: el backend GCS está comentado en
> `versions.tf`. Por eso `01-infra` todavía no sirve desde CI (arrancaría con el
> estado vacío). Ver el informe, §7.1.

### Paso 4: Configurar los secrets de GitHub Actions

| Secret | Valor |
|--------|-------|
| `GCP_WIF_PROVIDER` | `workload_identity_provider` de `tofu output` |
| `GCP_SERVICE_ACCOUNT` | `voxchain-cicd@voxchain-unlu.iam.gserviceaccount.com` |
| `K3S_KUBECONFIG` | kubeconfig del clúster k3s, en base64 |
| `RABBITMQ_USER` | `voxchain-worker` |
| `RABBITMQ_PASS` | `gcloud secrets versions access latest --secret rabbitmq-pass --project voxchain-unlu` |
| `RABBITMQ_CA_CERT` | contenido de `certs/ca.crt` |

Ninguno es una llave de GCP: los workflows se autentican por **Workload
Identity Federation** (OIDC). La única credencial estática es el kubeconfig del
k3s, porque es un clúster ajeno.

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

Los hosts del Ingress (`voxchain-ingress.yaml`) y el `GF_SERVER_ROOT_URL` de
Grafana (`terraform/gke/main.tf`) llevan la IP del LoadBalancer de
ingress-nginx en formato `sslip.io`. **Cambia en cada redespliegue** y hay que
actualizar los dos.

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

Todos se autentican contra GCP por Workload Identity Federation. Con la
infraestructura dada de baja, `03` y `04` fallan en el paso de autenticación
(`invalid_target`) porque el pool de WIF no existe. Es esperable y se resuelve
con `tofu apply`.

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
- **Autoscaling**: HPA por CPU al 70% (`api-hpa` 2→5; `worker-hpa` 2→10 y
  `pool-miner-hpa` 1→10 en el k3s) y Cluster Autoscaler de GKE sobre los node
  pools. No hay HPA por métricas específicas.
- **Observabilidad (U5.5)**: kube-prometheus-stack (Prometheus + Grafana +
  Alertmanager) desplegado vía Helm en el namespace `monitoring`. Cada servicio
  expone `/metrics` con métricas de aplicación (propuestas, bloques, workers,
  latencia). ServiceMonitors para el auto-descubrimiento, 5 reglas de alerta
  propias y el dashboard precargado en un ConfigMap. Alertmanager no tiene
  receptor configurado.
- **Seguridad de contenedores**: todos los workloads corren con `securityContext`
  restrictivo — `runAsNonRoot` (uid 1000 apps, 999 Redis/RabbitMQ, 101 nginx),
  `allowPrivilegeEscalation: false`, `capabilities.drop: ALL` y seccomp
  `RuntimeDefault`. El frontend usa `nginx-unprivileged` (puerto 8080 no
  privilegiado). Los logs a disco van a un `emptyDir` montado en
  `/var/log/voxchain`. `readOnlyRootFilesystem` no está activado.
- **Separación de workloads**: el nodepool `infra` tiene taint
  `pool=infra:NoSchedule` y label `pool=infra` (Terraform). Redis, Sentinel y
  RabbitMQ declaran `nodeSelector` + `tolerations` para schedulearse allí; los
  workloads de aplicación/minería quedan en el nodepool `apps` (sin toleration,
  el taint los excluye de `infra`).
- **Segmentación de red: pendiente.** `infrastructure/` trae NetworkPolicies
  para Redis y RabbitMQ, pero el clúster se crea con
  `network_policy_config { disabled = true }`, así que GKE no las aplica. Y
  `redis-external` expone Redis a internet sin TLS ni `loadBalancerSourceRanges`.
  Las dos cosas están declaradas como deuda en el informe (§6.2 y §7.2).
- **Registry público de lectura**: el k3s ajeno hace pull sin
  `imagePullSecrets`, que exigirían mandarle una llave estática de GCP. Las
  imágenes no contienen secretos.

## Plataforma de logging (colector de N servicios × M réplicas)

El colector centralizado es **Cloud Logging de GKE**, activo por defecto en el
cluster: un agente Fluent Bit corre como DaemonSet gestionado en cada nodo y
recolecta el stdout/stderr de **todos los pods de todas las réplicas** (API ×2,
NCT ×2, RabbitMQ ×3, Redis ×3+3, frontend ×2, workers), lo etiqueta con
namespace/pod/container y lo indexa en Logs Explorer de GCP.

La capa de aplicación complementa esto desde `common/logging_setup.py`:

- **Memoria/stdout**: handler de consola con formato **JSON estructurado**
  (`timestamp`, `level`, `logger`, `service`, `message`, `exception`) — lo que
  Cloud Logging parsea como payload estructurado, permitiendo filtrar por
  servicio y severidad.
- **Disco**: `RotatingFileHandler` en `/var/log/voxchain/<servicio>.log`
  (5 MB × 3 backups, montado como `emptyDir`), cumpliendo "registros de
  actividades gestionados en memoria y disco".

Consulta típica en Logs Explorer:

```
resource.type="k8s_container"
resource.labels.namespace_name="voxchain"
jsonPayload.service="nct"
severity>=WARNING
```

En el cluster k3s externo (fuera de GCP) los logs quedan accesibles vía
`kubectl logs` y los archivos rotativos del `emptyDir`.

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
