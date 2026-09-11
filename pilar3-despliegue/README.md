# Pilar 3 — Despliegue, prueba y escalabilidad

## Arquitectura

```
GCP — GKE (Cluster 1)                    GPU Cluster — k3s (Cluster 2)
┌──────────────────────────────┐         ┌───────────────────────────┐
│ Namespace: voxchain          │         │ Namespace: g-git-push-cv  │
│                              │         │                           │
│ RabbitMQ ───LB:5671 (AMQPS)──┼─────────┼──► Worker Deployment      │
│ Redis (interno)              │ TLS     │   - KEDA ScaledObject     │
│ NCT primary + standby        │ self-   │   - HPA (max 10)          │
│ Pool Coordinator             │ signed  │   - GPU tolerations       │
│ voxchain-api :8000           │         │   - CA cert volume        │
│ voxchain-frontend :443       │         │                           │
└──────────────────────────────┘         └───────────────────────────┘
```

## Guía de setup paso a paso

### Paso 1: Certs TLS autofirmados

```bash
cd pilar3-despliegue
./kubernetes/scripts/generate-certs.sh ./certs
```

Esto genera:
- `certs/ca.crt` → para workers GPU y KEDA
- `certs/rabbitmq-cert.pem` + `certs/rabbitmq-key.pem` → para RabbitMQ

Detalle completo (SANs, qué se versiona y qué no, cómo validan los workers
contra la IP del LoadBalancer, limitaciones): **[`certs/README.md`](certs/README.md)**.

### Paso 2: Subir secrets a GCP Secret Manager

```bash
gcloud secrets create rabbitmq-user --data-file=<(echo -n "voxchain-worker")
gcloud secrets create rabbitmq-pass --data-file=<(echo -n "CHANGE_ME")
gcloud secrets create rabbitmq-tls-crt --data-file=./certs/rabbitmq-cert.pem
gcloud secrets create rabbitmq-tls-key --data-file=./certs/rabbitmq-key.pem
gcloud secrets create rabbitmq-ca-crt --data-file=./certs/ca.crt
```

### Paso 3: Terraform — Crear cluster GKE

```bash
cd pilar3-despliegue/terraform/gke
cp terraform.tfvars.example terraform.tfvars
# Editar terraform.tfvars si es necesario
tofu init
tofu plan    # Revisar lo que va a crear
tofu apply   # ~15 min, crea VPC + GKE + nodepools + ESO + Artifact Registry + kube-prometheus-stack
```

El Terraform también despliega **kube-prometheus-stack** (Prometheus + Grafana + Alertmanager)
en el namespace `monitoring`. Grafana viene con:
- Admin password: `voxchain`
- Ingress en `grafana.voxchain.local` (requiere configuración de DNS o editar hosts)
- PVC de 10Gi para dashboards persistentes

Al final, correr `tofu output` para obtener:
- `artifact_registry` (URL para las imágenes Docker)
- `get_credentials` (comando kubectl)

### Paso 4: Configurar GitHub Actions Secrets

| Secret | Valor |
|--------|-------|
| `GCP_WIF_PROVIDER` | `projects/.../locations/global/workloadIdentityPools/github-actions/providers/github-provider` (de `tofu output`) |
| `GCP_SERVICE_ACCOUNT` | `voxchain-cicd@voxchain.iam.gserviceaccount.com` |
| `K3S_KUBECONFIG` | Contenido de `~/.kube/config` del cluster k3s (base64) |
| `RABBITMQ_USER` | `voxchain-worker` |
| `RABBITMQ_PASS` | El password que usaste en el Paso 2 |
| `RABBITMQ_CA_CERT` | Contenido de `certs/ca.crt` |

### Paso 5: Reemplazar REGISTRY en los deployments

En todos los `*-deployment.yaml` del cluster GCP hay `image: REGISTRY/...`.
Reemplazar `REGISTRY` por el valor de `artifact_registry` del Paso 3.

Ejemplo (si no se hace en CI/CD):
```bash
# southamerica-east1-docker.pkg.dev/voxchain/voxchain-images
find pilar3-despliegue/kubernetes -name "*.yaml" -exec \
  sed -i 's|REGISTRY|southamerica-east1-docker.pkg.dev/voxchain/voxchain-images|g' {} \;
```

### Paso 6: Aplicar manifests al cluster GCP

```bash
# Una vez que tofu apply terminó y tenés credenciales:
gcloud container clusters get-credentials voxchain --region southamerica-east1

kubectl apply -f pilar3-despliegue/kubernetes/namespace.yaml
kubectl apply -f pilar3-despliegue/kubernetes/infrastructure/
kubectl apply -f pilar3-despliegue/kubernetes/applications/
kubectl apply -f pilar3-despliegue/kubernetes/hpa/
kubectl apply -f pilar3-despliegue/kubernetes/monitoring/
```

Verificar:
```bash
kubectl get pods -n voxchain
```

### Paso 7: Instalar KEDA en k3s (GPU cluster)

```bash
# En el cluster k3s:
kubectl apply --server-side -f https://github.com/kedacore/keda/releases/download/v2.14.0/keda-2.14.0.yaml
```

### Paso 8: Desplegar workers en k3s

Conocer la IP del LoadBalancer de RabbitMQ en GCP:
```bash
kubectl get svc rabbitmq-external -n voxchain -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
# → 34.XX.XX.XX
```

En el cluster k3s:
```bash
kubectl create namespace g-git-push-cv

kubectl create configmap worker-config -n g-git-push-cv \
  --from-literal=rabbitmq-host="34.XX.XX.XX"

kubectl create secret generic rabbitmq-ca -n g-git-push-cv \
  --from-file=ca.crt=./certs/ca.crt

kubectl create secret generic rabbitmq-credentials -n g-git-push-cv \
  --from-literal=username="voxchain-worker" \
  --from-literal=password="CHANGE_ME"

kubectl apply -f pilar3-despliegue/kubernetes/gpu-cluster/worker-deployment.yaml
kubectl apply -f pilar3-despliegue/kubernetes/gpu-cluster/worker-hpa.yaml
kubectl apply -f pilar3-despliegue/kubernetes/gpu-cluster/worker-scaledobject.yaml
```

### Paso 9: Ejecutar load tests

```bash
cd pilar3-despliegue/load-tests/scenarios
./run_all.sh http://<FRONTEND_INGRESS_IP> ../resultados
```

### Paso 9.bis: Revisar la calibración de la dificultad

`N_ZEROS` es fijo por diseño (AGENT.md 10 y 11): el consenso no negocia su
dificultad porque toda regla autónoma es gameable por Sybil (11.2). El valor se
fija al desplegar, según el hardware de la población de mineros, y sólo lo cambia
una persona (11.3).

**No es una perilla suelta.** `N_ZEROS`, `NONCE_SPACE` y `WINDOW_SECONDS_*` se
mueven juntos: el espacio tiene que cubrir la derogación (`n+1`) o las ventanas
vencen sin sellar y en los logs parece falta de mineros. El NCT avisa por log si
la combinación es incoherente, al arrancar y antes de abrir cada ventana.

Referencia con el minero CPU a ~954 kH/s (`load-tests/scenarios/bench_hardware.py`):

| `n` | promulgar | derogar (`n+1`) | `NONCE_SPACE` necesario |
|-----|-----------|-----------------|-------------------------|
| 4   | 0,1 s     | 1,1 s           | 5 M                     |
| 5   | 1,1 s     | 17,6 s          | 78 M                    |
| 6   | 17,6 s    | 281 s           | 1.240 M  ← desplegado    |
| 7   | 281 s     | 4.501 s         | 19.800 M                |

Con GPU en la población estos tiempos caen por órdenes de magnitud: medir con
`bench_hardware.py` en el nodo GPU antes de fijar `n`.

### Paso 10: Disparar CI/CD (ya configurado)

Los workflows de GitHub Actions están listos en `.github/workflows/`:
- `ci-checks.yml` → en cada PR (gitleaks + pytest)
- `01-infra.yml` → manual (tofu apply)
- `02-services.yml` → automático al tocar `kubernetes/infrastructure/`
- `03-apps.yml` → automático al tocar `pilar2-distribuido/`
- `04-gpu-workers.yml` → automático al tocar `kubernetes/gpu-cluster/`

## CI/CD diagrama

```
PR → ci-checks (gitleaks + pytest)
              ↓ (merge a main)
    ┌─────────┼──────────────┐
    │         │              │
01-infra   02-services   03-apps      04-gpu-workers
(tofu)    (redis+rmq)   (build+deploy)  (k3s deploy)
```

## Componentes

| Directorio       | Contenido |
|------------------|-----------|
| `kubernetes/`    | Manifiestos K8s (infra, apps, HPA, GPU cluster) |
| `terraform/`     | Infraestructura como código con OpenTofu |
| `load-tests/`    | Pruebas de carga (bulk, dificultad, fragmentación) |
| `.secrets/`      | Ejemplos de secrets (cifrar con SOPS si se usa GitOps) |

## Decisiones de diseño

- OpenTofu declarativo para reproducibilidad
- GKE regional (1 nodo/AZ) para HA del plano de control
- Nodepool `infra` tainted para aislar Redis/RabbitMQ
- Nodepool `apps` con autoscaling para NCT, API, Frontend
- RabbitMQ con TLS autofirmado (AMQPS puerto 5671) para workers externos
- Workers GPU en k3s separado, conectados vía LoadBalancer externo
- KEDA para autoscaling event-driven de workers (por profundidad de cola RabbitMQ)
- External Secrets Operator en GKE para sincronizar secrets de GCP Secret Manager
- Workload Identity Federation para CI/CD (sin keys estáticas)
- HPA para escalado horizontal de API y workers por CPU
- **Observabilidad (U5.5)**: kube-prometheus-stack (Prometheus + Grafana + Alertmanager)
  desplegado via Helm en el namespace `monitoring`. Cada servicio expone `/metrics`
  con métricas de aplicación (propuestas, bloques, workers, latencia). ServiceMonitors
  configurados para auto-descubrimiento. Dashboard pre-cargado en ConfigMap.
- **Seguridad de contenedores**: todos los workloads corren con `securityContext`
  restrictivo — `runAsNonRoot` (uid 1000 apps, 999 Redis/RabbitMQ, 101 nginx),
  `allowPrivilegeEscalation: false`, `capabilities.drop: ALL` y seccomp
  `RuntimeDefault`. El frontend usa `nginx-unprivileged` (puerto 8080 no
  privilegiado). Los logs a disco van a un `emptyDir` montado en
  `/var/log/voxchain`.
- **Separación de workloads**: el nodepool `infra` tiene taint
  `pool=infra:NoSchedule` y label `pool=infra` (Terraform). Redis, Sentinel y
  RabbitMQ declaran `nodeSelector` + `tolerations` para schedulearse allí; los
  workloads de aplicación/minería quedan en el nodepool `apps` (sin toleration,
  el taint los excluye de `infra`).

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
