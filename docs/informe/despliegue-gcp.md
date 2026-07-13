# Despliegue en GCP — Qué se hizo y por qué

> Bitácora del despliegue del proyecto en Google Cloud (proyecto `voxchain-unlu`,
> 2026-07-13). Documenta cada paso, el comando ejecutado y **la razón detrás**,
> para poder explicarlo en la exposición. Complementa la guía genérica de
> `pilar3-despliegue/README.md` con los valores concretos de esta instalación.

---

## 0. Punto de partida

- Cuenta nueva de Google Cloud (free trial: US$300 / 90 días) con billing habilitado.
- Proyecto creado a mano en la consola: **`voxchain-unlu`**.
- Herramientas locales: `gcloud`, `tofu` (OpenTofu), `kubectl`, `helm`, `docker`, `gh`.

> **Por qué OpenTofu y no Terraform:** son compatibles (mismo HCL, mismos
> providers); OpenTofu es el fork open-source. El enunciado acepta cualquiera.

## 1. Autenticación (dos logins distintos)

```bash
gcloud auth login                       # identidad para la CLI gcloud
gcloud auth application-default login   # ADC: credenciales para librerías/OpenTofu
```

**Por qué son dos:** `gcloud auth login` autentica los comandos `gcloud ...`;
las **Application Default Credentials (ADC)** son las que usan los SDKs y el
provider de Google en OpenTofu. Son almacenes de credenciales separados.

*Problema real que apareció:* el primer intento de ADC falló con
`cloud-platform scope is required but not consented` — en la pantalla de
consentimiento del navegador no quedó tildado el permiso de Google Cloud
Platform. Se resolvió repitiendo el login y aceptando todos los permisos.

```bash
gcloud config set project voxchain-unlu
gcloud auth application-default set-quota-project voxchain-unlu
```

El **quota project** define contra qué proyecto se contabilizan las cuotas de
las llamadas API que hacen las librerías (sin él, algunas APIs rechazan pedidos).

## 2. Habilitación de APIs

```bash
gcloud services enable \
  container.googleapis.com            # GKE
  artifactregistry.googleapis.com     # registro de imágenes Docker
  secretmanager.googleapis.com        # secretos
  iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com  # WIF
  compute.googleapis.com              # VPC, discos, LBs
  cloudresourcemanager.googleapis.com # lectura de metadata del proyecto (Terraform)
```

**Por qué:** en GCP cada servicio tiene su API deshabilitada por defecto en
proyectos nuevos; sin esto, `tofu apply` falla con errores de permisos crípticos.

## 3. Parametrización del repo

El project ID viejo (`voxchain`) estaba en 12 lugares. Se reemplazó por
`voxchain-unlu` en:

- `pilar3-despliegue/terraform/gke/terraform.tfvars` → `project_id`
- `.github/workflows/03-apps.yml` → `env.PROJECT_ID` y `env.REGISTRY`
- 10 manifests de Kubernetes → rutas de imagen
  `southamerica-east1-docker.pkg.dev/voxchain-unlu/voxchain-images/...`

**Lección para el informe:** los project IDs de GCP son globalmente únicos, y
las rutas del Artifact Registry los incluyen — cambiar de proyecto implica
tocar todos los manifests. El workflow `03-apps` lo centraliza en una variable
de entorno; los manifests no (mejora posible: kustomize con un `images:` común).

## 4. Certificados TLS (para AMQPS)

```bash
bash kubernetes/scripts/generate-certs.sh ./certs
```

Genera una **CA autofirmada** (`ca.crt`) y el par cert/key del servidor RabbitMQ
con SAN `rabbitmq.voxchain.svc.cluster.local`. RabbitMQ expone el puerto 5671
(AMQPS) hacia los workers GPU del cluster k3s externo; esos workers confían en
la CA (montada como secret) para validar el canal.

**Qué se commitea y qué no:** solo `ca.crt` (público) va al repo; las claves
privadas (`*.pem`, `*.key`) están en `.gitignore`.

## 5. Secretos en GCP Secret Manager

Se crearon 8 secretos, con passwords generados con `openssl rand` (nunca
escritos en el repo ni mostrados en pantalla):

| Secreto | Contenido |
|---|---|
| `rabbitmq-user` / `rabbitmq-pass` | credenciales del broker |
| `rabbitmq-erlang-cookie` | cookie compartida del cluster RabbitMQ (3 nodos) |
| `redis-pass` | password de Redis |
| `rabbitmq-tls-crt` / `rabbitmq-tls-key` / `rabbitmq-ca-crt` | certs del paso 4 |
| `grafana-admin-password` | admin de Grafana (lo pide Terraform como variable) |

**Cómo llegan al cluster (la parte importante para la defensa):** el
**External Secrets Operator (ESO)**, desplegado por Terraform, sincroniza estos
secretos de Secret Manager a objetos `Secret` de Kubernetes
(`rabbitmq-external-secret.yaml`, `redis-external-secret.yaml`). ESO se
autentica contra GCP con **Workload Identity** (su ServiceAccount de Kubernetes
está vinculada a una service account de GCP con rol `secretmanager.secretAccessor`).

**Resultado — "zero static keys":** no hay ningún password ni key en el repo,
ni en los manifests, ni en los pipelines. La única credencial que existe es la
identidad federada. Esto responde directamente el ítem de la checklist
*"configuración de los secretos para habilitar despliegues 2 a N"*: un segundo
despliegue solo necesita recrear los secretos en Secret Manager del proyecto
nuevo (paso 5) — nada que rotar en el código.

## 6. Infraestructura con OpenTofu (`tofu init` / `plan` / `apply`)

```bash
cd pilar3-despliegue/terraform/gke
tofu init
TF_VAR_grafana_admin_password=$(gcloud secrets versions access latest --secret=grafana-admin-password) \
  tofu plan -out=plan.out
tofu apply plan.out
```

El plan creó **21 recursos**:

| Grupo | Recursos | Para qué |
|---|---|---|
| Red | VPC + subnet | red propia (no usar la default) |
| GKE | cluster zonal `voxchain` (southamerica-east1-a) | plano de control gestionado |
| Nodepools | `infra` (1-2 × e2-standard-2, **taint** `pool=infra:NoSchedule`) y `apps` (2-3 × e2-standard-2, autoscaling) | separar Redis/RabbitMQ de las apps; **Cluster Autoscaler** por rango min/max |
| Registry | Artifact Registry `voxchain-images` | imágenes Docker propias |
| Identidad | 3 service accounts (`nodes`, `cicd`, `external-secrets`) + Workload Identity Pool/Provider para GitHub | zero static keys: GitHub Actions se autentica por OIDC, sin JSON keys |
| Helm | cert-manager, external-secrets, ingress-nginx, kube-prometheus-stack | HTTPS automático, secretos, entrada HTTP, observabilidad |

**Conceptos para la defensa:**

- **`plan` vs `apply`:** el plan es una previsualización inmutable (se guarda en
  `plan.out`); el apply ejecuta exactamente ese plan. Evita sorpresas.
- **Workload Identity Federation (WIF):** GitHub Actions presenta un token OIDC
  firmado por GitHub; GCP lo valida contra el Workload Identity Pool y lo
  intercambia por credenciales temporales de la SA `cicd`. No existe ninguna
  key estática que se pueda filtrar.
- **Grafana password como `TF_VAR_...`:** la variable no tiene default a
  propósito — obliga a inyectarla desde Secret Manager o GitHub Secrets, nunca
  hardcodeada.
- **Costo:** ~US$5-7/día con todo encendido. `tofu destroy` cuando no se usa;
  `tofu apply` lo reconstruye en ~15-20 min (los datos de Redis/RabbitMQ viven
  en PVCs que se destruyen con el cluster — aceptable para un TP, se declara).

## 7. Post-apply (CI/CD y manifests) — lo que efectivamente pasó

**Outputs del apply** (`tofu output`):

| Output | Valor |
|---|---|
| `artifact_registry` | `southamerica-east1-docker.pkg.dev/voxchain-unlu/voxchain-images` |
| `cluster_endpoint` | `34.39.251.144` |
| `workload_identity_provider` | `projects/852675833447/.../providers/github-provider` |
| SA de CI/CD | `voxchain-cicd@voxchain-unlu.iam.gserviceaccount.com` |

1. **GitHub Secrets cargados** con `gh secret set`: `GCP_WIF_PROVIDER`,
   `GCP_SERVICE_ACCOUNT`, `RABBITMQ_USER`, `RABBITMQ_PASS` (leído de Secret
   Manager, nunca impreso), `RABBITMQ_CA_CERT`, `K3S_KUBECONFIG`.
2. **kubectl**: `gcloud container clusters get-credentials ...`. *Problema real:*
   el gcloud de pacman no incluye `gke-gcloud-auth-plugin` ni permite
   `gcloud components install`. Workaround temporal: contexto de kubectl con
   `--token=$(gcloud auth print-access-token)` (válido 1 h); solución
   definitiva: instalar el plugin desde AUR.
3. **IP del LoadBalancer de ingress-nginx: `34.95.245.215`** → actualizada en
   `voxchain-ingress.yaml`, los ClusterIssuers y `GF_SERVER_ROOT_URL`.
   **sslip.io** resuelve `cualquiercosa.<IP>.sslip.io → <IP>`: nombres DNS sin
   comprar dominio, lo que permite emitir certs Let's Encrypt vía cert-manager
   (challenge HTTP-01).
   - URL app: `https://voxchain.34.95.245.215.sslip.io`
   - URL Grafana: `https://grafana.voxchain.34.95.245.215.sslip.io`
4. **Manifests aplicados** (namespace, cert-manager, infrastructure,
   applications, hpa, monitoring — incluido el PrometheusRule con las 5
   alertas propias). *Gotcha encontrado:* el `ClusterSecretStore` tenía el
   `projectID` viejo hardcodeado — lugar N°13 donde vivía el project ID.
5. **Verificación de la cadena de secretos**: `ClusterSecretStore` → `Valid`,
   los 3 `ExternalSecrets` → `SecretSynced`. Los Secrets de Kubernetes
   aparecieron sin que ninguna credencial pasara por el repo.
6. **Estado de pods**: Redis ×3 + Sentinel ×3 + RabbitMQ ×3 `Running` **en el
   nodo del pool `infra`** — validando en cluster real las tolerations y el
   `securityContext` no-root agregados esta semana. Las apps propias quedaron
   en `ErrImagePull` hasta que el pipeline `03-apps` buildee y pushee las
   imágenes al registry nuevo (el registry nace vacío).
7. Pendiente para el cluster k3s externo: recrear `worker-config` (IP nueva del
   LB de RabbitMQ), `rabbitmq-ca` (CA nueva) y `rabbitmq-credentials` (password
   nuevo desde Secret Manager) en el namespace `g-git-push-cv`.

### 7.1 Lo que el despliegue desde cero destapó (deuda del deploy manual)

Redesplegar en un proyecto virgen reveló **tres configuraciones que en el
despliegue anterior se habían hecho a mano y nunca se versionaron** — el
argumento más concreto a favor de la infraestructura declarativa:

1. **GitHub Actions nunca corrió.** La API de Actions reporta 0 workflows
   registrados y 0 runs en toda la historia del repo: los pipelines existían
   como código pero el despliegue real siempre fue `scripts/deploy-manual.sh`.
   (Pendiente: push a `main` para registrarlos y mostrar runs verdes.)
   Como workaround se buildearon las imágenes localmente replicando los
   comandos exactos del workflow `03-apps`.
2. **El usuario de RabbitMQ no existía en ningún manifest.** Los NCT
   crasheaban con `ACCESS_REFUSED`: el usuario `voxchain-worker` se había
   creado a mano con `rabbitmqctl` en el cluster viejo. Fix declarativo:
   `RABBITMQ_DEFAULT_USER/PASS` desde el Secret (sincronizado por ESO) en el
   StatefulSet — el broker nace con el usuario correcto en el primer arranque.
   De paso se observó la **auto-recuperación real**: los NCT reintentaron la
   conexión en loop, crashearon con backoff y levantaron solos al arreglarse
   el broker, sin intervención.
3. **El readiness del NCT no implementaba el diseño documentado.** El Service
   `nct` selecciona primary y standby, y `/health` devuelve 503 en el
   follower → la API reportaba `nct: error` de forma intermitente (50% de los
   requests). FUNCIONAMIENTO.md ya decía que el readiness debía ser `/health`
   (readiness = liderazgo), pero los deployments usaban `tcpSocket`. Con el
   fix, el follower queda `0/1 NotReady` **a propósito** y el Service enruta
   solo al líder.
4. **El Ingress de Grafana apuntaba a un service de otro namespace** (un
   Ingress solo puede referenciar services de su namespace; Grafana vive en
   `monitoring`). Fix: service `ExternalName` puente, versionado en
   `monitoring/grafana-bridge-service.yaml`.

**Estado final del despliegue:**

```
https://voxchain.34.95.245.215.sslip.io/api/health
→ {"api":"ok","nct":"ok","redis":"ok","workers":"unknown"}   (TLS Let's Encrypt)

https://grafana.voxchain.34.95.245.215.sslip.io  → HTTP 200
```

`workers: unknown` es lo esperado hasta conectar el cluster k3s (paso 7.7).

## 8. Resumen en una frase (para abrir la explicación)

*"Con una cuenta nueva y un comando de OpenTofu reconstruimos toda la
plataforma — red, cluster, registry, identidad federada y observabilidad — en
20 minutos, sin una sola credencial estática en el repositorio: los secretos
viven en Secret Manager y llegan al cluster por External Secrets con Workload
Identity."*
