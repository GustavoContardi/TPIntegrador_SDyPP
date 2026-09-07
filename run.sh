#!/usr/bin/env bash
# VoxChain — punto de entrada único del proyecto.
#
# Todo se ejecuta desde la terminal, sin abrir un IDE. Cada subcomando verifica
# sus prerequisitos y explica qué falta si no están.
#
#   ./run.sh demo       levanta el sistema completo en local y sella una ley
#   ./run.sh test       corre la suite de tests
#   ./run.sh worker ID  levanta un minero registrado desde la UI
#   ./run.sh miner      compila (si hay CUDA) y corre el minero de Pilar 1
#   ./run.sh scale      experimento de escalado N transacciones con M vs 2xM
#   ./run.sh bench      techo de cómputo de esta máquina
#   ./run.sh graficos   regenera los gráficos del informe
#   ./run.sh limpiar    borra mineros fantasma y equipos huérfanos
#   ./run.sh stop       baja el sistema local
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE="$RAIZ/pilar2-distribuido/docker-compose.yml"
VENV="$RAIZ/.venv"
API="http://localhost:8000"

rojo()  { printf '\033[31m%s\033[0m\n' "$*"; }
verde() { printf '\033[32m%s\033[0m\n' "$*"; }
info()  { printf '\033[1m%s\033[0m\n' "$*"; }

# -- prerequisitos ---------------------------------------------------------

necesita() {
  if ! command -v "$1" >/dev/null 2>&1; then
    rojo "Falta '$1'. $2"
    exit 1
  fi
}

necesita_docker() {
  necesita docker "Instalalo desde https://docs.docker.com/engine/install/"
  if ! docker info >/dev/null 2>&1; then
    rojo "El daemon de Docker no está corriendo."
    echo "  Arrancalo con:  sudo systemctl start docker"
    exit 1
  fi
}

# Crea el venv sólo la primera vez. Queda en .venv/ (ignorado por git).
asegura_venv() {
  if [ ! -x "$VENV/bin/python" ]; then
    info "Creando entorno virtual en .venv/ (sólo la primera vez)..."
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet \
      -r "$RAIZ/pilar2-distribuido/nct-coordinator/requirements.txt" \
      -r "$RAIZ/pilar2-distribuido/worker/requirements.txt"
    "$VENV/bin/pip" install --quiet pytest "fakeredis[lua]"
  fi
}

# -- subcomandos -----------------------------------------------------------

cmd_demo() {
  necesita_docker
  info "== Levantando VoxChain en local =="
  echo "RabbitMQ + Redis + NCT (primary y standby) + workers + API + frontend"
  echo
  docker compose -f "$COMPOSE" up -d --build

  echo -n "Esperando a que el sistema esté listo "
  local listo=0
  for _ in $(seq 1 90); do
    if curl -s --max-time 5 "$API/api/health" 2>/dev/null | grep -q '"nct":"ok"'; then
      listo=1
      break
    fi
    echo -n "."
    sleep 2
  done
  echo
  if [ "$listo" -eq 0 ]; then
    rojo "El sistema no respondió a tiempo. Revisá los logs con:"
    echo "  docker compose -f $COMPOSE logs"
    exit 1
  fi
  verde "Sistema arriba."
  echo
  curl -s "$API/api/health"; echo
  echo
  info "== Proponiendo una ley de ejemplo =="
  local texto="Ley de ejemplo generada por run.sh ($(date +%H:%M:%S))"
  curl -s -X POST "$API/api/laws" \
    -H 'Content-Type: application/json' \
    -d "{\"text\":\"$texto\",\"author_pubkey\":\"pk-demo-$RANDOM\"}" | head -c 300
  echo; echo
  echo -n "Esperando a que los mineros la sellen "
  local antes
  antes="$(curl -s "$API/api/chain" | grep -o '"block_hash"' | wc -l)"
  for _ in $(seq 1 60); do
    local ahora
    ahora="$(curl -s "$API/api/chain" | grep -o '"block_hash"' | wc -l)"
    if [ "$ahora" -gt "$antes" ]; then
      echo " ✓"
      verde "Bloque sellado. La cadena tiene $ahora bloques."
      break
    fi
    echo -n "."
    sleep 2
  done
  echo
  info "Todo listo. Podés ver:"
  echo "  Frontend      → http://localhost:4200"
  echo "  API / cadena  → $API/api/chain"
  echo "  Estado        → $API/api/health"
  echo "  RabbitMQ      → http://localhost:15672  (guest/guest)"
  echo
  echo "Para bajarlo:  ./run.sh stop"
}

# Registrar un minero en la UI lo anota en la red, pero no lo enciende: sin
# Kubernetes configurado no hay quien le cree un proceso. Esto es ese proceso.
#
# El contenedor arranca sin WORKER_MODE: el worker lee su modo de
# `worker:desired_mode:<id>` en Redis, así que si su dueño ya lo asignó a un
# equipo arranca directamente como minero de ese equipo.
nombre_contenedor() {
  # Los nombres de contenedor sólo admiten [a-zA-Z0-9_.-]; el ID del minero lo
  # elige una persona y puede traer cualquier cosa.
  printf 'voxchain-worker-%s' \
    "$(printf '%s' "$1" | tr -c 'a-zA-Z0-9_.-' '-' | sed 's/^-*//;s/-*$//')"
}

cmd_worker() {
  local id="${1:-}"
  if [ -z "$id" ]; then
    rojo "Falta el ID del minero."
    echo "  Uso:  ./run.sh worker <id-del-minero>"
    echo "  Es el mismo ID con el que lo registraste en la UI (pestaña Minería)."
    exit 1
  fi
  necesita_docker

  local nombre; nombre="$(nombre_contenedor "$id")"

  if docker ps -a --format '{{.Names}}' | grep -qx "$nombre"; then
    info "Ya existe un contenedor para '$id'; lo reemplazo."
    docker rm -f "$nombre" >/dev/null
  fi

  if ! docker compose -f "$COMPOSE" ps --status running --services 2>/dev/null | grep -qx rabbitmq; then
    rojo "El sistema no está levantado. Corré primero:  ./run.sh demo"
    exit 1
  fi

  info "== Levantando el minero '$id' =="
  # `run` en vez de un servicio nuevo del compose: el ID lo elige el usuario en
  # tiempo de ejecución, así que no se puede declarar de antemano.
  docker compose -f "$COMPOSE" run -d --rm \
    --name "$nombre" \
    -e WORKER_ID="$id" \
    -e WORKER_ADDRESS="http://$nombre:9001" \
    --no-deps \
    worker-1 >/dev/null

  echo -n "Esperando a que reporte su estado "
  for _ in $(seq 1 30); do
    if curl -s --max-time 3 "$API/api/workers/status" 2>/dev/null \
       | grep -q "\"worker_id\":\"$id\",\"mode\""; then
      echo " ✓"
      verde "Minero '$id' corriendo."
      curl -s "$API/api/workers/status" | tr ',' '\n' | grep -A0 "$id" | head -1
      echo
      echo "Se ve en la UI: http://localhost:4200/workers"
      echo "Para bajarlo:   docker rm -f $nombre"
      return 0
    fi
    echo -n "."
    sleep 2
  done
  echo
  rojo "El minero no reportó a tiempo. Revisá sus logs con:"
  echo "  docker logs $nombre"
  exit 1
}

# Probar la pantalla de Minería deja registros de mineros sin proceso detrás y
# equipos cuyo coordinador no existe. No rompen nada —la UI los muestra como
# "Sin arrancar"— pero ensucian una demo.
cmd_limpiar() {
  asegura_venv
  necesita_docker
  info "== Limpieza de mineros fantasma y equipos huérfanos =="

  # El estado vive en Redis, así que sin Redis no hay nada que limpiar. Si el
  # sistema está bajado arrancamos sólo ese contenedor: levantar el stack entero
  # para borrar unas claves sería desproporcionado, y fallar con un error de
  # conexión, inútil.
  if ! docker compose -f "$COMPOSE" ps --status running --services 2>/dev/null | grep -qx redis; then
    echo "Redis no está levantado; lo arranco para poder limpiar."
    # compose escribe su progreso a stderr, así que hay que callar los dos.
    docker compose -f "$COMPOSE" up -d redis >/dev/null 2>&1
    # El healthcheck del compose tarda un par de segundos en dar verde.
    for _ in $(seq 1 15); do
      docker compose -f "$COMPOSE" exec -T redis redis-cli ping >/dev/null 2>&1 && break
      sleep 1
    done
  fi

  REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}" \
    "$VENV/bin/python" "$RAIZ/pilar2-distribuido/scripts/limpiar_registros.py" "$@"
}

cmd_stop() {
  necesita_docker
  info "Bajando el sistema local..."
  docker compose -f "$COMPOSE" down
  # Los mineros levantados con `./run.sh worker` son contenedores sueltos
  # (docker compose run), así que `down` no se los lleva.
  local sueltos
  sueltos="$(docker ps -aq --filter 'name=^voxchain-worker-' 2>/dev/null || true)"
  if [ -n "$sueltos" ]; then
    echo "Bajando los mineros levantados a mano..."
    # shellcheck disable=SC2086
    docker rm -f $sueltos >/dev/null
  fi
  # El stack del experimento de escalado es aparte; se baja solo, pero por si
  # quedó algo colgado de una corrida interrumpida:
  docker compose -f "$RAIZ/pilar2-distribuido/docker-compose.scale.yml" \
    down -v --remove-orphans >/dev/null 2>&1 || true
  verde "Listo."
}

cmd_test() {
  asegura_venv
  info "== Suite de tests =="
  cd "$RAIZ/pilar2-distribuido"
  "$VENV/bin/python" -m pytest "$@"
}

cmd_miner() {
  local base="${1:-voxchain}"
  local prefijo="${2:-00000}"
  local desde="${3:-0}"
  local hasta="${4:-50000000}"

  if command -v nvcc >/dev/null 2>&1; then
    info "== Minero GPU (CUDA) =="
    make -C "$RAIZ/pilar1-minero/gpu" 05_brute_force_range >/dev/null
    echo "base='$base' prefijo='$prefijo' rango=[$desde, $hasta)"
    "$RAIZ/pilar1-minero/gpu/bin/05_brute_force_range" "$base" "$prefijo" "$desde" "$hasta"
  else
    info "== Minero CPU =="
    echo "(no se encontró nvcc; se usa el minero CPU, que es el mismo fallback"
    echo " que usan los workers cuando no hay GPU disponible)"
    echo "base='$base' prefijo='$prefijo' rango=[$desde, $hasta)"
    python3 "$RAIZ/pilar1-minero/cpu/src/brute_force.py" "$base" "$prefijo" "$desde" "$hasta"
  fi
}

cmd_scale() {
  necesita_docker
  info "== Escalado: N transacciones con M vs 2xM recursos =="
  "$RAIZ/pilar3-despliegue/load-tests/scenarios/run_scaling.sh" "$@"
}

cmd_bench() {
  info "== Techo de cómputo de esta máquina =="
  echo "Correlo con la máquina descargada; si hay contenedores levantándose o"
  echo "bajándose, el número sale mal."
  echo
  python3 "$RAIZ/pilar3-despliegue/load-tests/scenarios/bench_hardware.py" "$@"
}

cmd_graficos() {
  asegura_venv
  if ! "$VENV/bin/python" -c "import matplotlib" 2>/dev/null; then
    info "Instalando matplotlib..."
    "$VENV/bin/pip" install --quiet matplotlib
  fi
  info "== Regenerando los gráficos del informe =="
  "$VENV/bin/python" "$RAIZ/pilar3-despliegue/load-tests/scenarios/graficos.py" "$@"
}

uso() {
  # Imprime el bloque de comentarios de la cabecera y corta en la primera
  # línea que no sea comentario.
  awk 'NR>1 { if (/^#/) { sub(/^# ?/,""); print } else { exit } }' \
    "${BASH_SOURCE[0]}"
  echo
  echo "Ejemplos:"
  echo "  ./run.sh demo"
  echo "  ./run.sh test -k failover"
  echo "  ./run.sh worker minero-gustavo-1"
  echo "  ./run.sh limpiar            # muestra qué borraría"
  echo "  ./run.sh limpiar --apply    # lo borra"
  echo "  ./run.sh miner voxchain 0000 0 10000000"
  echo "  ./run.sh scale --miners 1,2,4 --laws 10 --n-zeros 6"
}

case "${1:-}" in
  demo)     shift; cmd_demo "$@" ;;
  stop)     shift; cmd_stop "$@" ;;
  test)     shift; cmd_test "$@" ;;
  worker)   shift; cmd_worker "$@" ;;
  limpiar)  shift; cmd_limpiar "$@" ;;
  miner)    shift; cmd_miner "$@" ;;
  scale)    shift; cmd_scale "$@" ;;
  bench)    shift; cmd_bench "$@" ;;
  graficos) shift; cmd_graficos "$@" ;;
  ""|-h|--help|help) uso ;;
  *) rojo "Subcomando desconocido: $1"; echo; uso; exit 1 ;;
esac
