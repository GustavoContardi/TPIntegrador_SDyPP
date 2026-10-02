#!/bin/bash
# Conecta el k3s simulado (VM voxchain-k3s, en voxchain-vpc) con GKE: lo que
# harían 02 y 04 con un k3s alcanzable desde GitHub. El API del k3s sólo escucha
# en la IP interna (10.0.0.9), así que los runners no llegan y esto se corre a
# mano, con kubectl apuntando a GKE.
#
# 1. kubeconfig de la SA voxchain-api (Role acotado a g-git-push-cv) -> secret
#    k3s-kubeconfig del API en GKE y secret K3S_KUBECONFIG de GitHub (para que
#    un 02 posterior no vuelva a poner el del k3s anterior).
# 2. Credenciales de Redis, RabbitMQ (usuario voxchain-worker) y Loki -> k3s.
# 3. Reinicia el API para que cargue el kubeconfig nuevo.
#
# Ningún valor pasa por la línea de comandos ni queda en disco.
set -euo pipefail

NS=g-git-push-cv
RMQ_HOST=$(kubectl get svc rabbitmq-external -n voxchain -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

case "$(kubectl config current-context)" in
  gke_voxchain-unlu_*) ;;
  *) echo "kubectl no apunta a GKE (voxchain-unlu)"; exit 1 ;;
esac

vm() {
  gcloud compute ssh voxchain-k3s --zone southamerica-east1-a --project voxchain-unlu \
    --tunnel-through-iap --quiet --command "$1" 2>/dev/null
}

# Imprime un Secret en JSON. Los valores salen de variables de entorno:
# secret_json <namespace> <nombre> VAR=clave [VAR=clave ...]
secret_json() {
  python3 - "$@" <<'PY'
import json, os, sys
ns, name, *pairs = sys.argv[1:]
data = {k: os.environ[v] for v, k in (p.split("=", 1) for p in pairs)}
print(json.dumps({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                  "metadata": {"name": name, "namespace": ns}, "stringData": data}))
PY
}

echo "== 1. kubeconfig del k3s para el API"
export KCFG
KCFG="$(vm 'T=$(sudo k3s kubectl get secret voxchain-api-token -n g-git-push-cv -o jsonpath="{.data.token}" | base64 -d)
C=$(sudo k3s kubectl get secret voxchain-api-token -n g-git-push-cv -o jsonpath="{.data.ca\.crt}")
cat <<EOF
apiVersion: v1
kind: Config
clusters:
- name: voxchain-k3s
  cluster:
    server: https://10.0.0.9:6443
    certificate-authority-data: $C
users:
- name: voxchain-api
  user:
    token: $T
contexts:
- name: voxchain-k3s
  context:
    cluster: voxchain-k3s
    user: voxchain-api
    namespace: g-git-push-cv
current-context: voxchain-k3s
EOF')"
if ! grep -q 'token: .' <<<"$KCFG"; then
  echo "No se pudo leer el token de la SA voxchain-api en el k3s"; exit 1
fi
secret_json voxchain k3s-kubeconfig KCFG=kubeconfig | kubectl apply -f -
printf '%s' "$KCFG" | base64 | gh secret set K3S_KUBECONFIG
echo "secret K3S_KUBECONFIG de GitHub actualizado"

echo "== 2. credenciales de los mineros en el k3s"
if ! kubectl exec -n voxchain rabbitmq-0 -- rabbitmqctl list_users -q 2>/dev/null | grep -q '^voxchain-worker'; then
  echo "AVISO: no existe el usuario voxchain-worker en RabbitMQ (lo crea 02-services)"
fi
export REDIS_PASS LOKI_PASS RMQ_USER=voxchain-worker RMQ_PASS KEDA_HOST
REDIS_PASS="$(kubectl get secret redis-credentials -n voxchain -o jsonpath='{.data.password}' | base64 -d)"
LOKI_PASS="$(kubectl get secret loki-push-credentials -n voxchain -o jsonpath='{.data.password}' | base64 -d)"
RMQ_PASS="$(gcloud secrets versions access latest --secret rabbitmq-pass --project voxchain-unlu)"
KEDA_HOST="amqps://${RMQ_USER}:${RMQ_PASS}@${RMQ_HOST}:5671/"
secret_json $NS redis-credentials REDIS_PASS=password | vm 'sudo k3s kubectl apply -f -'
secret_json $NS loki-push-credentials LOKI_PASS=password | vm 'sudo k3s kubectl apply -f -'
secret_json $NS rabbitmq-credentials RMQ_USER=username RMQ_PASS=password KEDA_HOST=keda-host \
  | vm 'sudo k3s kubectl apply -f -'

echo "== 3. reinicio del API"
kubectl rollout restart deployment/voxchain-api -n voxchain
kubectl rollout status deployment/voxchain-api -n voxchain --timeout=180s
sleep 5
kubectl logs -n voxchain deploy/voxchain-api --tail=300 | grep -i -E "kubeconfig|kubernetes config" | tail -3
echo "Listo."
