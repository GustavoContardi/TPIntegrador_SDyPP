#!/usr/bin/env bash
# Dispara una alerta de prueba en el Alertmanager de GKE para comprobar que el
# receptor de Discord (monitoring/alertmanager-config.yaml) funciona, sin tener
# que romper nada del sistema.
#
#   ./probar-alerta.sh            con el kubeconfig actual apuntando a GKE
#
# La alerta se llama VoxchainPrueba, así que entra por la misma ruta que las
# reales. No se le pone fin: Alertmanager la da por resuelta cuando vence
# `resolve_timeout` (5 min) y manda también el aviso de "Resuelta".
set -euo pipefail

NS=monitoring
SVC=svc/kube-prometheus-stack-alertmanager
PUERTO="${PUERTO_LOCAL:-19093}"

command -v kubectl >/dev/null || { echo "Falta kubectl."; exit 1; }
command -v curl    >/dev/null || { echo "Falta curl.";    exit 1; }

kubectl -n "$NS" port-forward "$SVC" "$PUERTO:9093" >/dev/null &
PF=$!
trap 'kill "$PF" 2>/dev/null || true' EXIT

for _ in $(seq 1 20); do
  curl -sf "http://127.0.0.1:$PUERTO/-/ready" >/dev/null && break
  sleep 0.5
done

curl -sf -X POST "http://127.0.0.1:$PUERTO/api/v2/alerts" \
  -H 'Content-Type: application/json' \
  -d '[{
    "labels": {"alertname": "VoxchainPrueba", "severity": "warning"},
    "annotations": {
      "summary": "Alerta de prueba",
      "description": "Disparada a mano con probar-alerta.sh para verificar el receptor de Discord."
    }
  }]'

echo "Alerta enviada. Tiene que llegar a Discord en unos 30 s (group_wait),"
echo "y el aviso de resuelta unos 5 min después."
