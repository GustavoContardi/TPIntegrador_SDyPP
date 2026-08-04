#!/usr/bin/env bash
# Crea en GCP Secret Manager los 7 secretos que consumen los ExternalSecret de
# kubernetes/infrastructure/. Se corre UNA VEZ por entorno, antes del pipeline
# 02-services (que verifica que existan y falla con un mensaje claro si no).
#
#   ./bootstrap-secrets.sh              usa los certs de pilar3-despliegue/certs
#                                       (los genera con generate-certs.sh si faltan)
#   ./bootstrap-secrets.sh --rotate     regenera todas las contraseñas
#
# Por qué esto NO es un paso de pipeline: para que el CI los suba, el material
# sensible (la clave privada de la CA, las contraseñas) tendría que vivir en el
# CI — y eso contradice el diseño de zero static keys, donde GitHub Actions se
# autentica por Workload Identity Federation justamente para no guardar
# credenciales. El bootstrap es un acto humano, deliberado y auditable; los
# pipelines sólo consumen lo que quedó.
set -euo pipefail

PROJECT="${PROJECT_ID:-voxchain-unlu}"
AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERTS="$(cd "$AQUI/../.." && pwd)/certs"
ROTAR="${1:-}"

rojo()  { printf '\033[31m%s\033[0m\n' "$*"; }
verde() { printf '\033[32m%s\033[0m\n' "$*"; }
info()  { printf '\033[1m%s\033[0m\n' "$*"; }

command -v gcloud  >/dev/null || { rojo "Falta gcloud.";  exit 1; }
command -v openssl >/dev/null || { rojo "Falta openssl."; exit 1; }

# Crea el secreto si no existe y le agrega una versión con el valor dado.
# Idempotente: correrlo dos veces no rompe nada, sólo agrega una versión.
subir() {
  local nombre="$1" valor="$2"
  if ! gcloud secrets describe "$nombre" --project="$PROJECT" >/dev/null 2>&1; then
    gcloud secrets create "$nombre" --project="$PROJECT" \
      --replication-policy=automatic >/dev/null
    echo "  + $nombre (creado)"
  else
    echo "  ~ $nombre (versión nueva)"
  fi
  printf '%s' "$valor" | gcloud secrets versions add "$nombre" \
    --project="$PROJECT" --data-file=- >/dev/null
}

# -- certificados de RabbitMQ ----------------------------------------------
# Los SANs son sólo nombres DNS internos del clúster, no la IP del
# LoadBalancer, así que recrear la infra con otra IP NO invalida el
# certificado: los workers externos se conectan por IP y el nombre esperado se
# fuerza por SNI (ssl_server_hostname en common/messaging/rabbitmq.py).
if [ ! -f "$CERTS/ca-cert.pem" ] || [ ! -f "$CERTS/rabbitmq-cert.pem" ]; then
  info "== No hay certificados: generándolos con generate-certs.sh =="
  "$AQUI/generate-certs.sh" "$CERTS"
else
  info "== Usando los certificados existentes de $CERTS =="
  openssl x509 -in "$CERTS/rabbitmq-cert.pem" -noout -enddate -ext subjectAltName \
    | sed 's/^/  /'
fi

# -- contraseñas ------------------------------------------------------------
# Si el secreto ya existe y no se pidió --rotate se reusa su valor actual, para
# no dejar a los pods con credenciales distintas de las que ya tienen montadas.
password_de() {
  local nombre="$1"
  if [ "$ROTAR" != "--rotate" ] && \
     gcloud secrets versions access latest --secret="$nombre" \
       --project="$PROJECT" 2>/dev/null; then
    return 0
  fi
  openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | head -c 32
}

info "== Subiendo secretos al proyecto $PROJECT =="
subir rabbitmq-ca-crt         "$(cat "$CERTS/ca-cert.pem")"
subir rabbitmq-tls-crt        "$(cat "$CERTS/rabbitmq-cert.pem")"
subir rabbitmq-tls-key        "$(cat "$CERTS/rabbitmq-key.pem")"
subir rabbitmq-user           "voxchain"
subir rabbitmq-pass           "$(password_de rabbitmq-pass)"
subir rabbitmq-erlang-cookie  "$(password_de rabbitmq-erlang-cookie)"
subir redis-pass              "$(password_de redis-pass)"

echo
verde "Listo. Secretos en Secret Manager:"
gcloud secrets list --project="$PROJECT" --format="table(name)"
echo
echo "Siguiente paso: disparar el pipeline 02-services."
echo
echo "OJO: los workers del k3s se conectan con estas mismas credenciales, así"
echo "que si rotaste contraseñas hay que actualizar los secrets RABBITMQ_USER,"
echo "RABBITMQ_PASS y RABBITMQ_CA_CERT del repo en GitHub antes de correr"
echo "04-gpu-workers. Para verlos:"
echo "  gcloud secrets versions access latest --secret=rabbitmq-pass --project=$PROJECT"
