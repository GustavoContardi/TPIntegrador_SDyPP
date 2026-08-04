#!/usr/bin/env bash
# Experimento "N transacciones con M recursos" vs "con 2xM recursos"
# (checklist §4 y §7), sobre el stack local docker-compose.scale.yml.
#
# Para cada M de la lista: levanta el stack de cero, escala los mineros, espera
# a que estén todos registrados en el pool y corre test_resources.py.
#
# Se levanta de cero en cada configuración a propósito: reusar el stack deja la
# cadena y la cola de la corrida anterior, y las mediciones dejan de ser
# comparables entre sí.
#
# M cuenta al pool-coordinator, que además de repartir fragmentos auto-mina.
# Es decir: M mineros = 1 coordinator + (M-1) pool-workers.
#
# Uso:
#   ./run_scaling.sh                      # M = 1,2,4 con 10 leyes y n_zeros=6
#   ./run_scaling.sh --miners 2,4         # sólo M vs 2xM
#   ./run_scaling.sh --laws 5 --n-zeros 5 # corrida corta de prueba
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/pilar2-distribuido/docker-compose.scale.yml"

MINERS_LIST="1,2,4"
LAWS=10
N_ZEROS=6
API_URL="http://localhost:8000"
POOL_URL="http://localhost:9001"
OUT_DIR="$SCRIPT_DIR/../resultados"
KEEP_UP=0

while [ $# -gt 0 ]; do
  case "$1" in
    --miners)  MINERS_LIST="$2"; shift 2 ;;
    --laws)    LAWS="$2"; shift 2 ;;
    --n-zeros) N_ZEROS="$2"; shift 2 ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    --keep-up) KEEP_UP=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "opción desconocida: $1" >&2; exit 1 ;;
  esac
done

export N_ZEROS
mkdir -p "$OUT_DIR"

SUMMARY_CSV="$OUT_DIR/resultados_recursos.csv"
DETAIL_CSV="$OUT_DIR/resultados_recursos_detalle.csv"

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

cleanup() {
  if [ "$KEEP_UP" -eq 1 ]; then
    echo "--- --keep-up: el stack queda corriendo (bajarlo con:"
    echo "    docker compose -f $COMPOSE_FILE down -v)"
    return
  fi
  echo "--- Bajando el stack ---"
  compose down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

wait_api() {
  echo -n "    API"
  for _ in $(seq 1 90); do
    if curl -s --max-time 5 "$API_URL/api/health" 2>/dev/null | grep -q '"nct":"ok"'; then
      echo " ✓"
      return 0
    fi
    echo -n "."
    sleep 2
  done
  echo " TIMEOUT"
  return 1
}

# Espera a que el coordinator tenga registrados los pool-workers esperados.
# Sin esto se mediría un arranque a medio poblar y el speedup saldría subestimado.
wait_miners() {
  local expected="$1"
  echo -n "    pool-workers registrados (esperando $expected)"
  for _ in $(seq 1 60); do
    local n
    n="$(curl -s --max-time 5 "$POOL_URL/health" 2>/dev/null \
         | python3 -c 'import json,sys; print(json.load(sys.stdin).get("miners",-1))' \
         2>/dev/null || echo -1)"
    if [ "$n" = "$expected" ]; then
      echo " ✓"
      return 0
    fi
    echo -n "."
    sleep 2
  done
  echo " TIMEOUT (sigue igual, la corrida queda marcada con el M pedido)"
  return 0
}

echo "=== VoxChain — escalado M vs 2xM (local) ==="
echo "Compose:    $COMPOSE_FILE"
echo "Mineros:    $MINERS_LIST   (incluyen al pool-coordinator)"
echo "Leyes:      $LAWS por configuración"
echo "Dificultad: n_zeros=$N_ZEROS"
echo "Resultados: $SUMMARY_CSV"
echo ""
echo "CPUs disponibles: $(nproc). Cada minero está limitado a 1 CPU."
echo ""

: > "$SUMMARY_CSV"
: > "$DETAIL_CSV"

echo "--- Construyendo imágenes (una vez) ---"
compose build >/dev/null

IFS=',' read -ra MINERS <<< "$MINERS_LIST"
for m in "${MINERS[@]}"; do
  workers=$(( m - 1 ))
  if [ "$workers" -lt 0 ]; then
    echo "M debe ser >= 1 (M incluye al coordinator); salteando '$m'" >&2
    continue
  fi

  echo ""
  echo "--- M=$m mineros (1 coordinator + $workers pool-worker) ---"
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  compose up -d --scale "pool-worker=$workers" >/dev/null
  wait_api
  wait_miners "$workers"
  # Margen para que el primer challenge encuentre a todos los mineros pidiendo
  # trabajo (el pool-worker duerme hasta 2 s entre pedidos).
  sleep 5

  python3 "$SCRIPT_DIR/test_resources.py" \
    --api-url "$API_URL" \
    --miners "$m" \
    --laws "$LAWS" \
    --n-zeros "$N_ZEROS" \
    --output "$SUMMARY_CSV" \
    --detail-output "$DETAIL_CSV" \
    --append
done

echo ""
echo "=== Completado ==="
column -s, -t < "$SUMMARY_CSV" 2>/dev/null || cat "$SUMMARY_CSV"
echo ""
echo "Detalle por ley: $DETAIL_CSV"
