#!/usr/bin/env bash
# Batería de pruebas de carga contra el despliegue real (GKE + k3s de workers).
#
# A diferencia de run_all.sh (que asume un entorno local ya configurado), este
# runner ajusta la config real entre corridas:
#   - N_ZEROS: ConfigMap voxchain-config en GKE + restart del NCT.
#   - FRAGMENT_SIZE: env de los deployments con pool coordinator en el k3s.
# Al terminar restaura N_ZEROS=4 y borra el override de FRAGMENT_SIZE.
#
# Orden: dificultad → fragmentación → bulk. Bulk va último porque encola
# cientos de leyes y taparía la cola para las mediciones de time_to_seal.
#
# Requisitos: contexto kubectl `gke-token` con token vigente (el script lo
# renueva vía gcloud), y gustavo.yaml para el k3s.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

API_URL="${1:-https://voxchain.34.95.245.215.sslip.io}"
OUT_DIR="${2:-$SCRIPT_DIR/../resultados}"

GKE_CTX="gke-token"
GKE_NS="voxchain"
K3S_KUBECONFIG="$REPO_ROOT/gustavo.yaml"
K3S_NS="g-git-push-cv"
# Deployments del k3s que pueden actuar de pool coordinator (fragmentan el
# espacio de nonces).
K3S_COORD_DEPLOYS=(worker pool-coordinator worker-pool-coordinator)

N_ZEROS_DEFAULT=4
NONCE_SPACE=50000000

mkdir -p "$OUT_DIR"

kgke() { kubectl --context "$GKE_CTX" -n "$GKE_NS" "$@"; }
kk3s() { KUBECONFIG="$K3S_KUBECONFIG" kubectl -n "$K3S_NS" "$@"; }

wait_health() {
  echo -n "    esperando health nct=ok "
  for _ in $(seq 1 60); do
    if curl -sk --max-time 10 "$API_URL/api/health" | grep -q '"nct":"ok"'; then
      echo "✓"
      return 0
    fi
    echo -n "."
    sleep 5
  done
  echo "TIMEOUT"
  return 1
}

set_n_zeros() {
  local n="$1"
  # El token del contexto gke-token dura 1 h: renovarlo en cada cambio.
  kubectl config set-credentials gke-token-user \
    --token="$(gcloud auth print-access-token)" >/dev/null
  echo "    N_ZEROS=$n + restart NCT"
  kgke patch configmap voxchain-config --type merge -p "{\"data\":{\"N_ZEROS\":\"$n\"}}" >/dev/null
  # No usar `rollout restart`: el pod nuevo no puede tomar el lease de líder
  # mientras el viejo lo renueva → nunca pasa a Ready → deadlock. Además, un
  # follower que nunca vio un heartbeat no dispara la elección (monitor.py),
  # así que hay que garantizar que el primary arranque con el lease expirado:
  # drenar ambos deployments, esperar el TTL del lease (15 s) y recién ahí
  # levantar SOLO el primary (adquiere liderazgo en el arranque). El standby
  # queda en 0 durante la batería; restore_all lo repone.
  kgke scale deploy/nct-primary deploy/nct-standby --replicas=0 >/dev/null
  kgke wait --for=delete pod -l 'app in (nct-primary,nct-standby)' --timeout=120s >/dev/null 2>&1 || true
  sleep 20
  kgke scale deploy/nct-primary --replicas=1 >/dev/null
  kgke rollout status deploy/nct-primary --timeout=180s >/dev/null
  wait_health
}

set_fragment_size() {
  local size="$1"
  echo "    FRAGMENT_SIZE=$size en k3s (${K3S_COORD_DEPLOYS[*]})"
  for d in "${K3S_COORD_DEPLOYS[@]}"; do
    if [ "$size" = "-" ]; then
      kk3s set env "deploy/$d" FRAGMENT_SIZE- >/dev/null
    else
      kk3s set env "deploy/$d" "FRAGMENT_SIZE=$size" >/dev/null
    fi
  done
  for d in "${K3S_COORD_DEPLOYS[@]}"; do
    kk3s rollout status "deploy/$d" --timeout=180s >/dev/null
  done
  sleep 15  # elección bully + registro de miners en el pool
}

restore_all() {
  echo "--- Restaurando configuración por defecto ---"
  set_n_zeros "$N_ZEROS_DEFAULT" || true
  set_fragment_size "-" || true
  kgke scale deploy/nct-standby --replicas=1 >/dev/null 2>&1 || true
}
trap restore_all EXIT

echo "=== VoxChain — Batería de pruebas de carga (cloud) ==="
echo "API: $API_URL"
echo "Resultados: $OUT_DIR"
echo ""

echo "Renovando token GKE (dura 1 h)..."
kubectl config set-credentials gke-token-user --token="$(gcloud auth print-access-token)" >/dev/null

echo ""
echo "--- Dificultad (N_ZEROS 1..6) ---"
: > "$OUT_DIR/resultados_dificultad.csv"
for n in 1 2 3 4 5 6; do
  set_n_zeros "$n"
  python3 "$SCRIPT_DIR/test_difficulty.py" --api-url "$API_URL" \
    --min-zeros "$n" --max-zeros "$n" \
    --output "$OUT_DIR/.dificultad_part_$n.csv"
  if [ ! -s "$OUT_DIR/resultados_dificultad.csv" ]; then
    cat "$OUT_DIR/.dificultad_part_$n.csv" > "$OUT_DIR/resultados_dificultad.csv"
  else
    tail -n +2 "$OUT_DIR/.dificultad_part_$n.csv" >> "$OUT_DIR/resultados_dificultad.csv"
  fi
  rm -f "$OUT_DIR/.dificultad_part_$n.csv"
done

echo ""
echo "--- Fragmentación (vuelve a N_ZEROS=$N_ZEROS_DEFAULT) ---"
set_n_zeros "$N_ZEROS_DEFAULT"
: > "$OUT_DIR/resultados_fragmentacion.csv"
for pct in 1 5 10 25 50; do
  size=$(( NONCE_SPACE * pct / 100 ))
  set_fragment_size "$size"
  python3 "$SCRIPT_DIR/test_fragmentation.py" --api-url "$API_URL" \
    --nonce-space "$NONCE_SPACE" --fragments-pct "$pct" \
    --output "$OUT_DIR/.frag_part_$pct.csv"
  if [ ! -s "$OUT_DIR/resultados_fragmentacion.csv" ]; then
    cat "$OUT_DIR/.frag_part_$pct.csv" > "$OUT_DIR/resultados_fragmentacion.csv"
  else
    tail -n +2 "$OUT_DIR/.frag_part_$pct.csv" >> "$OUT_DIR/resultados_fragmentacion.csv"
  fi
  rm -f "$OUT_DIR/.frag_part_$pct.csv"
done
set_fragment_size "-"

echo ""
echo "--- Bulk (al final: encola cientos de leyes) ---"
python3 "$SCRIPT_DIR/test_bulk.py" --api-url "$API_URL" \
  --sizes "1,10,100,1000" \
  --output "$OUT_DIR/resultados_bulk.csv"

echo ""
echo "=== Completado ==="
ls -lh "$OUT_DIR"
