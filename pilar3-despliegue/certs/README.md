# Certificados TLS

Todo lo relativo a los certificados del despliegue, en un solo lugar. Antes
estaba repartido entre `pilar3-despliegue/README.md`, `INSTRUCCIONES.md`,
`docs/informe/despliegue-gcp.md` y `docs/informe/INFORME.md`.

Sirven para **un solo canal**: el AMQPS (puerto 5671) por el que los workers del
cluster k3s externo hablan con el RabbitMQ que corre en GKE. Es el único tramo
que sale a Internet sin pasar por el Ingress.

## Qué hay en este directorio

| Archivo | Qué es | ¿En git? |
|---|---|---|
| `ca.crt` | Certificado de la CA autofirmada (público) | **Sí** |
| `ca-cert.pem` | Idéntico a `ca.crt` (mismo contenido, el script deja las dos copias) | No |
| `ca-key.pem` | **Clave privada de la CA.** Con esto se firman certs nuevos | No |
| `rabbitmq-cert.pem` | Certificado de servidor de RabbitMQ | No |
| `rabbitmq-key.pem` | Clave privada del servidor | No |
| `server-cert.pem` / `server-key.pem` | Copias idénticas de los dos anteriores | No |

Sólo `ca.crt` está trackeado, y está bien que así sea: es material público, lo
que los clientes necesitan para *validar*, no para *hacerse pasar por* el
servidor.

El resto lo excluye la regla `*.pem` de `.gitignore:30`. Ojo con el efecto
secundario: esa regla es por extensión, no por sensibilidad, así que también
tapa `ca-cert.pem` y `rabbitmq-cert.pem`, que son públicos. **Por eso el script
copia la CA a `ca.crt`**: para que el único archivo que sí queremos versionar
escape del patrón.

## Cómo se generan

```bash
cd pilar3-despliegue
./kubernetes/scripts/generate-certs.sh ./certs
```

Lo que hace, verificado contra los certs actuales:

| | Valor |
|---|---|
| CA | RSA 4096, autofirmada, `CN=VoxChain CA` |
| Servidor | RSA 2048, `CN=rabbitmq.voxchain.svc.cluster.local` |
| Validez | 3650 días (los actuales: **13/07/2026 → 10/07/2036**) |
| SANs | `rabbitmq.voxchain.svc.cluster.local`, `rabbitmq`, `localhost`, `*.voxchain.svc.cluster.local`, IP `127.0.0.1` |
| Uso extendido | `serverAuth`, `clientAuth` |

El script es idempotente en el sentido de que siempre regenera todo: **si lo
volvés a correr, los certs viejos se pisan y hay que resubir los secretos y
reiniciar RabbitMQ**. Los certs de julio se reusaron tal cual en el redeploy de
agosto justamente para evitar ese trabajo.

## La parte que no es obvia: SANs sin la IP del LoadBalancer

Los workers del k3s se conectan a la **IP del LoadBalancer** (hoy
`34.151.236.95:5671`), y esa IP **no está en los SANs**. Una validación TLS
normal fallaría con un error de hostname.

No falla porque el cliente valida contra un nombre distinto del que usa para
conectarse:

```yaml
# gpu-cluster/pool-coordinator-deployment.yaml:64-67
- name: RABBITMQ_TLS_CA_PATH
  value: /etc/rabbitmq-ca/ca.crt
- name: RABBITMQ_TLS_SERVER_NAME
  value: rabbitmq.voxchain.svc.cluster.local
```

`common/messaging/rabbitmq.py:68-75` arma el contexto SSL con esa CA y le pasa
`server_hostname` a `pika.SSLOptions`. O sea: **TCP contra la IP, verificación
contra el nombre interno**, que sí está en los SANs.

La ventaja es real: la IP del LoadBalancer cambia en cada recreación de la
infra, y los certs no hay que tocarlos. La contrapartida es que si alguien
levanta otro broker con la misma CA, el nombre no lo distingue.

Los mineros que crea el "Register Worker" desde la web reciben las mismas dos
variables por código, en `voxchain_api/routers/workers.py:165-166`.

## A dónde van los certs

**GCP Secret Manager** (los consume el cluster GKE vía external-secrets):

```bash
gcloud secrets create rabbitmq-tls-crt --data-file=./certs/rabbitmq-cert.pem
gcloud secrets create rabbitmq-tls-key --data-file=./certs/rabbitmq-key.pem
gcloud secrets create rabbitmq-ca-crt  --data-file=./certs/ca.crt
```

En la práctica esto lo hace `kubernetes/scripts/bootstrap-secrets.sh`, que
llama a `generate-certs.sh` si hace falta y sube los 7 secretos. Es
deliberadamente manual: automatizarlo exigiría guardar el material sensible en
el CI, en contra del diseño de zero static keys.

**GitHub Secrets** (los consume el pipeline `04-gpu-workers` para el k3s):

| Secret | Contenido |
|---|---|
| `RABBITMQ_CA_CERT` | El contenido de `certs/ca.crt` |

No hay que tocarlo mientras no se regenere la CA. En el k3s la CA termina
montada en `/etc/rabbitmq-ca/ca.crt` desde el secret `rabbitmq-ca` (definido en
`kubernetes/gpu-cluster/rabbitmq-ca-secret.yaml`).

## Limitaciones conocidas

- **Es TLS de un solo lado.** El cert tiene `clientAuth` en el uso extendido,
  pero no hay mTLS: RabbitMQ no exige certificado de cliente, la autenticación
  la da la contraseña de `voxchain-worker`. Es la decisión de "cifrar el borde
  y segmentar el interior" que argumenta `docs/informe/INFORME.md:522`.
- **El tráfico interno de GKE va en claro.** El puerto 5672 (AMQP sin TLS) es el
  que usan la API y el NCT dentro del cluster; el 5671 existe sólo para el k3s.
  La protección interna es de red (NetworkPolicies), no criptográfica.
- **CA autofirmada, sin revocación.** No hay CRL ni OCSP: si se filtra
  `ca-key.pem` la única salida es regenerar todo y resubir los secretos.
- **Sin rotación automática.** Diez años de validez es una elección de
  conveniencia para un TP, no una práctica defendible en producción.
- El HTTPS del sitio (`voxchain.<ip>.sslip.io`) **no usa nada de esto**: eso lo
  emite cert-manager con Let's Encrypt contra el Ingress, y se renueva solo.

## Ver también

- `kubernetes/scripts/generate-certs.sh` — el generador
- `kubernetes/scripts/bootstrap-secrets.sh` — sube los secretos a Secret Manager
- `docs/informe/despliegue-gcp.md` §4 — el paso en la bitácora del despliegue
- `docs/informe/INFORME.md` §6.2 — por qué el TLS interno es parcial
