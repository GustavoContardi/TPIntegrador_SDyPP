# Despliegue en GCP — Qué se hizo y por qué

> Bitácora del despliegue del proyecto en Google Cloud (proyecto `voxchain-unlu`).
> Documenta cada paso, el comando ejecutado y **la razón detrás**, para poder
> explicarlo en la exposición. Complementa la guía genérica de
> `pilar3-despliegue/README.md` con los valores concretos de esta instalación.
>
> - §0–§7: primer despliegue, **2026-07-13**.
> - §8: redespliegue desde cero, **2026-09-27/28**. Es el que está vivo: IP fija,
>   pipelines 02/03 corriendo desde CI y los problemas que eso destapó.

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
   - *Desde septiembre la IP es estática y estas URLs ya no valen (ver §8.3).*
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

## 8. Redespliegue desde cero (2026-09-27/28)

La infraestructura se había dado de baja tras el despliegue de agosto. Para la
presentación se reconstruyó sobre el mismo proyecto, esta vez con los
pipelines 02 y 03 corriendo desde GitHub Actions y no desde
`scripts/deploy-manual.sh`. Eso destapó cuatro problemas que el despliegue
manual escondía.

### 8.1 Punto de partida

| Pieza | Estado encontrado |
|---|---|
| Proyecto `voxchain-unlu` | Activo, pero con la **cuenta de facturación cerrada** (`open: false`): el free trial había terminado. Se reactivó a mano como cuenta paga. |
| Recursos | Ninguno: sin clúster, bucket, discos, IPs ni pool de WIF de GitHub. Sólo quedaban los 7 secretos de agosto en Secret Manager. |
| Certificados | La clave privada de la CA no estaba en ningún lado, y el `certs/ca.crt` del repo **no coincidía** con el `rabbitmq-ca-crt` de Secret Manager. |
| GitHub Secrets | Los de julio, apuntando a un WIF inexistente. Faltaban `GCP_INFRA_SERVICE_ACCOUNT` y `GRAFANA_ADMIN_PASSWORD`. |
| k3s | Inalcanzable: `181.229.77.252:19500` da timeout. La IP resuelve a una conexión de cable hogareña (`*.cab.prima.com.ar`), probablemente con IP dinámica. Consultado a la cátedra. |

Herramientas agregadas en la máquina local: OpenTofu 1.12, Helm 4 y
`gke-gcloud-auth-plugin` (con gcloud de Homebrew queda fuera del `PATH`: hay
que enlazarlo a mano en `/opt/homebrew/bin`).

### 8.2 Bootstrap con rotación

```bash
./pilar3-despliegue/kubernetes/scripts/bootstrap-secrets.sh --rotate
```

- **CA nueva**: sin la clave privada, la CA vieja no podía firmar nada más.
  `generate-certs.sh` genera CA y certificado del servidor. Los `*.pem` están en
  `.gitignore`; sólo `ca.crt` (público) se versiona.
- **Contraseñas rotadas**: RabbitMQ, cookie de Erlang y Redis. Además se
  agrega `grafana-admin-password` (antes se creaba a mano).
- **Versiones viejas deshabilitadas**: rotar agrega una versión, pero la
  anterior sigue legible. Se deshabilitó la versión 1 de cada secreto con
  `gcloud secrets versions disable 1 --secret <nombre>`, que es reversible, a
  diferencia de `destroy`.
- **Bucket del estado de OpenTofu** (`gs://voxchain-unlu-tfstate`, con
  versionado).

### 8.3 IP estática del Ingress

**Problema:** el host público es `voxchain.<IP>.sslip.io`, y la IP del
LoadBalancer de ingress-nginx era efímera: cambiaba en cada `destroy` +
`apply`. Estaba escrita a mano en 6 archivos, con **tres IPs distintas entre
sí** (restos de despliegues anteriores). Además, del host dependen:

- el certificado de Let's Encrypt,
- la URL de Grafana,
- el `rpId` de las **passkeys**: una passkey queda atada al dominio con el que
  se creó, así que un host nuevo invalida todas las identidades registradas
  con passkey.

**Solución:** `bootstrap-secrets.sh` reserva una IP regional
(`voxchain-ingress-ip` = **`35.199.68.144`**) y Terraform sólo la lee:

```hcl
data "google_compute_address" "ingress" {
  name   = "voxchain-ingress-ip"
  region = var.region
}
# ingress-nginx: controller.service.loadBalancerIP = esa IP
# Grafana:       grafana.ini.server.root_url = https://grafana.voxchain.<IP>.sslip.io
```

**Por qué fuera del estado de OpenTofu:** si la IP fuera un recurso de
Terraform, `tofu destroy` la liberaría y volveríamos al problema. Con
`lifecycle { prevent_destroy = true }` el `destroy` completo fallaría. Como el
bucket, es un recurso "de entorno" que vive más que el clúster.

**Costo:** una IP reservada sin usar sale unos US$7 por mes. Es el precio de que
las URLs, el certificado y las passkeys sobrevivan a apagar el clúster.

Las IPs de RabbitMQ y Redis siguen siendo efímeras a propósito: sólo las
consume el pipeline 04, que las lee del Service en cada corrida.

### 8.4 OpenTofu: dos arreglos antes del apply

Revisando `main.tf` antes de aplicar aparecieron dos configuraciones que se
ignoraban en silencio:

1. **Los nodos no podían escribir logs.** La SA propia de los nodos sólo tenía
   `artifactregistry.reader`. GKE exige `roles/container.defaultNodeServiceAccount`
   (escribir logs y métricas) cuando no se usa la SA por defecto de Compute.
   Sin ese rol, el Fluent Bit gestionado recolecta, pero Cloud Logging rechaza
   la escritura: la "plataforma de logging" del checklist quedaba vacía.
2. **La URL de Grafana no se aplicaba.** Iba como
   `grafana.extraEnvVars.GF_SERVER_ROOT_URL`, una clave que el chart de Grafana
   no conoce. Pasó a `grafana.ini.server.root_url`.

```bash
cd pilar3-despliegue/terraform/gke
tofu init
TF_VAR_grafana_admin_password="$(gcloud secrets versions access latest --secret grafana-admin-password)" \
  tofu plan -out=plan.out     # 31 to add, 0 to change, 0 to destroy
tofu apply plan.out
rm plan.out                   # el plan guarda la contraseña de Grafana en claro
```

Resultado: GKE 1.35, 2 nodos `apps` + 1 `infra` (con el taint `pool`),
Dataplane V2 activo (pods de Cilium) y las 4 releases de Helm desplegadas.

### 8.5 Secretos de GitHub y primer push

Los valores sensibles pasan de Secret Manager a `gh` por un pipe, sin quedar en
pantalla ni en el historial de la shell:

```bash
gcloud secrets versions access latest --secret rabbitmq-pass | gh secret set RABBITMQ_PASS
```

| Secret / variable | Valor |
|---|---|
| `GCP_WIF_PROVIDER` | `tofu output -raw workload_identity_provider` |
| `GCP_SERVICE_ACCOUNT` | `voxchain-cicd@voxchain-unlu.iam.gserviceaccount.com` |
| `GCP_INFRA_SERVICE_ACCOUNT` | `tofu output -raw infra_service_account` |
| `GRAFANA_ADMIN_PASSWORD`, `RABBITMQ_PASS` | desde Secret Manager |
| `RABBITMQ_USER` | `voxchain-worker` (no el admin) |
| `RABBITMQ_CA_CERT` | `certs/ca.crt` |
| `CLOUD_ENABLED` (variable) | `true`: los push vuelven a disparar 02/03 |

**Por qué hace falta un push para desplegar:** el runner de Actions arranca
vacío y hace `checkout` del repo. Lo que no está commiteado no existe para él.
Es intencional: lo desplegado es siempre un commit concreto (las imágenes
llevan su SHA), y nada depende del disco de alguien. Es lo opuesto a lo que
pasó en julio (§7.1).

Resultado del primer push: **03 en verde** (5 imágenes buildeadas en paralelo y
apps desplegadas), **02 en rojo** y **gitleaks en rojo**.

### 8.6 El CI no podía crear RBAC

```
roles.rbac.authorization.k8s.io is forbidden: User "voxchain-cicd@..." cannot
create resource "roles" ... requires one of ["container.roles.create"]
```

**Causa:** la SA de CI tiene `roles/container.developer`, que da acceso a casi
todo el API de Kubernetes **excepto RBAC**. `rabbitmq-rbac.yaml` nunca se había
aplicado desde un pipeline: en los despliegues anteriores lo aplicaba a mano
el owner del proyecto.

**Alternativas descartadas:**

- `roles/container.admin` para la SA de CI: le daría control de todo el clúster.
- Un rol IAM propio con `container.roles.*`: vale para todo el proyecto, no
  para un namespace.

**Solución:** Terraform (que sí es admin) crea el namespace `voxchain` y le da
a la SA de CI el ClusterRole `admin` **sólo en ese namespace**:

```hcl
resource "kubernetes_role_binding_v1" "cicd_admin" {
  metadata {
    name      = "cicd-admin"
    namespace = "voxchain"
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = "admin"
  }
  subject {
    api_group = "rbac.authorization.k8s.io"
    kind      = "User"
    name      = google_service_account.cicd.email
  }
}
```

Es el patrón habitual: la plataforma provisiona el namespace y el pipeline
despliega adentro. Además, la prevención de escalada de Kubernetes impide que
el CI otorgue permisos que él mismo no tiene. Como el namespace ya existía
(lo había creado el 02), hubo que importarlo antes del apply:

```bash
tofu import kubernetes_namespace_v1.voxchain voxchain
tofu apply      # 1 to add: el RoleBinding
```

Verificación:

```bash
kubectl auth can-i create roles -n voxchain   --as voxchain-cicd@voxchain-unlu.iam.gserviceaccount.com  # yes
kubectl auth can-i create roles -n monitoring --as voxchain-cicd@voxchain-unlu.iam.gserviceaccount.com  # no
```

### 8.7 Split-brain silencioso de RabbitMQ

Consecuencia directa de §8.6: sin el Role, el peer discovery de Kubernetes no
pudo listar los endpoints, y **cada nodo arrancó como un clúster de un solo
miembro**:

```
rabbitmq-0 → running_nodes: [rabbit@rabbitmq-0]     (cada uno con su propio cluster ID)
```

Los tres pods estaban `Running` y sus probes pasaban, así que nada lo
señalaba. Pero el Service balanceaba entre **tres brokers independientes**: un
mensaje publicado en uno no llegaba a un consumidor conectado a otro. Todas las
colas y consumidores habían caído, por azar, en `rabbitmq-0`.

**Arreglo:** con los nodos 1 y 2 vacíos (verificado con `list_queues`), se
resetearon y se unieron a `rabbitmq-0`:

```bash
for i in 1 2; do kubectl exec -n voxchain rabbitmq-$i -- sh -c '
  rabbitmqctl stop_app && rabbitmqctl reset &&
  rabbitmqctl join_cluster rabbit@rabbitmq-0.rabbitmq.voxchain.svc.cluster.local &&
  rabbitmqctl start_app'; done
```

Un nodo que ya tiene datos se reincorpora solo a su clúster al reiniciar, así
que esto se hace una sola vez. Con el Role presente, un despliegue desde cero
forma el clúster sin intervención.

**Lección:** que un pod esté `Ready` no significa que el sistema esté bien. Queda
como mejora una alerta sobre la cantidad de miembros del clúster de RabbitMQ
(menos de 3).

### 8.8 gitleaks: falso positivo con el host

El host nuevo en `WEBAUTHN_RP_IDS` (`voxchain.35.199.68.144.sslip.io`) tiene
suficiente entropía para que la regla `generic-api-key` lo tome por una clave.
Se agregó a la allowlist global de `.gitleaks.toml` la expresión `\.sslip\.io`,
contra el match completo. Verificado en local: con la config vieja aparece 1
hallazgo, con la nueva 0, y el historial completo está limpio.

### 8.9 Estado final

Con el 02 relanzado a mano (`gh workflow run 02-services.yml`), todo quedó en
verde:

```
$ curl https://voxchain.35.199.68.144.sslip.io/api/health
{"api":"ok","nct":"ok","redis":"ok","rabbitmq":"ok","frontend":"ok",
 "workers":"none","clock":"ok","clock_skew_ms":0.0}
```

| Comprobación | Resultado |
|---|---|
| HTTPS de la app y de Grafana | 200, certificado de Let's Encrypt (`CN=YR1`) válido hasta 2026-12-27 |
| RabbitMQ | 3 nodos en un solo clúster, `partitions: {}` |
| Usuario `voxchain-worker` | creado por el 02, con permisos en `/` |
| AMQPS (`34.95.226.120:5671`) | `openssl s_client` con la CA nueva y SNI `rabbitmq.voxchain.svc.cluster.local` → `Verify return code: 0` |
| ExternalSecrets | los 3 en `SecretSynced` |
| Cloud Logging | recibe api, frontend, redis y rabbitmq. Los logs de la API llegan como `jsonPayload` con `service: voxchain-api`. |

`workers: none` es lo esperado hasta conectar el k3s.

URLs vigentes (fijas mientras exista `voxchain-ingress-ip`):

- App: `https://voxchain.35.199.68.144.sslip.io`
- Grafana: `https://grafana.voxchain.35.199.68.144.sslip.io`

### 8.10 Pendiente

- **k3s / pipeline 04**: a la espera de la IP y el puerto actuales del clúster.
  Hay que actualizar `K3S_KUBECONFIG` y definir `K3S_EGRESS_CIDRS`.
- **Mientras tanto, Redis está expuesto a internet sin TLS** (`34.39.157.247:6379`),
  protegido sólo por una contraseña de 32 caracteres. `K3S_EGRESS_CIDRS` lo
  acota a la IP del k3s. TLS en Redis sigue siendo deuda (informe §7.2).
- **Cuota de IPs**: el límite regional es 8 IPs en uso. Con los nodos al máximo
  (5, todos con IP pública) más 3 LoadBalancers se llega justo al límite, y un
  upgrade con *surge* lo pasaría.
- **Alerta de split-brain** de RabbitMQ (§8.7).

### 8.11 Cambios posteriores, para el próximo redespliegue

Lo que entró al repositorio después de esta bitácora (29 y 30 de septiembre) y
todavía no se aplicó sobre la nube:

| Cambio | Commit | Qué hay que correr |
|---|---|---|
| Failover de Redis con HAProxy delante de Sentinel | `b92117d` | `02` |
| Métricas de los mineros del k3s vía el latido (`voxchain_miner_*`) y paneles nuevos | `0acd039`, `9da32e1` | `03` (imagen del API y dashboard) y `04` (mineros) |
| Loki + Alloy, e Ingress de push para los mineros del k3s | `76f6e33` | `bootstrap-secrets.sh` (2 secretos nuevos), `01`, `02`, `03`, `04` |
| Alertas a Discord | `d7e30d7` | `bootstrap-secrets.sh` con `DISCORD_WEBHOOK_URL`, `01`, `03` |

Secretos nuevos en Secret Manager: `loki-push-password`, `loki-push-htpasswd` y
`alertmanager-discord-webhook`. Los dos primeros los genera el script; el
webhook lo crea el dueño del canal de Discord. `02` falla si faltan los de Loki
y avisa, sin cortar, si falta el de Discord.

Orden completo: `DISCORD_WEBHOOK_URL=... bootstrap-secrets.sh` → `01-infra` →
`02-services` → `03-apps` → `04-gpu-workers` (este último con `K3S_KUBECONFIG`
y `K3S_EGRESS_CIDRS` al día). Para comprobar las alertas:
`kubernetes/scripts/probar-alerta.sh`.

## 9. Resumen en una frase (para abrir la explicación)

*"Con una cuenta nueva y un comando de OpenTofu reconstruimos toda la
plataforma — red, cluster, registry, identidad federada y observabilidad — en
20 minutos, sin una sola credencial estática en el repositorio: los secretos
viven en Secret Manager y llegan al cluster por External Secrets con Workload
Identity."*
