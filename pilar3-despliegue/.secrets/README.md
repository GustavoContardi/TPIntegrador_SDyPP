# Secrets para el cluster GPU (k3s)

> **Este directorio no se usa en el despliegue actual.** Quedó de una idea
> temprana de manejar los secretos del k3s con SOPS + Age en modo GitOps. Se
> descartó a favor de GitHub Secrets, que ya estaban disponibles y no exigían
> distribuir una clave Age. Se conserva porque el enfoque sigue siendo válido
> si en algún momento se quiere versionar los secretos cifrados en el repo.

## Cómo funciona hoy, de verdad

Los secretos del k3s los inyecta el workflow **`04-gpu-workers.yml`** desde
GitHub Secrets, sin SOPS y sin nada de este directorio:

| GitHub Secret | Para qué |
|---|---|
| `RABBITMQ_USER` / `RABBITMQ_PASS` | credenciales de `voxchain-worker` en el broker |
| `RABBITMQ_CA_CERT` | CA para validar el AMQPS (ver `../certs/README.md`) |
| `K3S_KUBECONFIG` | acceso al cluster del profesor |

Del lado de GKE los secretos vienen de **GCP Secret Manager** vía
external-secrets, y los sube `../kubernetes/scripts/bootstrap-secrets.sh`.
Nada de eso pasa por acá.

## Qué hay en este directorio

| Archivo | Contenido |
|---|---|
| `rabbitmq-credentials.yaml` | Plantilla del Secret, **con placeholders** (`CHANGE_ME_IN_PROD`), sin cifrar |
| `.sops.yaml` | Config de SOPS que quedó del enfoque descartado |

`rabbitmq-credentials.yaml` está trackeado en git y **no contiene credenciales
reales**: es una plantilla. Por eso `gitleaks` pasa en CI. Si alguna vez se
completa con valores de verdad, hay que cifrarlo antes de commitear.

## Si se quisiera retomar SOPS

```bash
# 1. Generar par de claves Age
age-keygen -o ~/.config/sops/age/keys.txt

# 2. Poner la clave pública en .sops.yaml

# 3. Cifrar
sops --encrypt --age age1... \
  rabbitmq-credentials.yaml > rabbitmq-credentials.enc.yaml
```

Faltaría además que `04-gpu-workers.yml` descifre en el runner (con la clave
privada Age como GitHub Secret) antes de aplicar los manifests. Ese paso no
existe: hoy el workflow construye los Secrets directamente con `kubectl create
secret`.
