# PENDIENTES — Gaps contra la checklist oficial

> Resultado de la auditoría local contra `2026_SDYPP_checklist-blockchain-v2.docx.pdf`
> (2026-07-13). Suite de tests validada localmente: **122/122 pasan** (requiere
> `fakeredis[lua]`). Sin secretos commiteados (verificado en historial git).
>
> Cada ítem tiene su sección de la checklist entre paréntesis. Marcar con `[x]` al resolver.

---

## 🔴 Prioridad 1 — Bloqueantes de la entrega

- [ ] **Correr los escenarios de carga y guardar resultados** (§4, §7).
  Los scripts ya existen (`pilar3-despliegue/load-tests/scenarios/` y
  `pilar2-distribuido/tests/stress/`), pero `pilar1-minero/benchmarks/resultados/`
  está vacío y no hay corridas documentadas de N transacciones con M vs 2xM recursos.
  - [ ] Bulks de transacciones: 1 → 100.000 (`test_bulk.py`)
  - [ ] Dificultad de prefijo: 1 → 8 (`test_difficulty.py`)
  - [ ] Fragmentación del pool: 1% → 50% (`test_fragmentation.py`)
  - [ ] Ingreso/egreso de nodos GPU con generación dinámica de CPU (documentar corrida)
- [ ] **Grabar el video** explicando servicios, componentes y configuraciones (§6).
  `docs/video/` está vacío. Requisito binario de la entrega.
- [ ] **Informe final** (§7): comparativa de resultados, gráficos comparativos de
  tiempos de respuesta por configuración, diagrama de arquitectura como entregable.
  Base ya escrita: `FUNCIONAMIENTO.md` (protocolo, fallas, limitaciones §14 sirve
  como reflexión crítica) y `docs/informe/hit*.md` (benchmarks pilar 1).

## 🟡 Prioridad 2 — Fixes rápidos (una tarde)

- [x] **CI: `fakeredis` sin soporte Lua** (§4). `ci-checks.yml` instala `fakeredis`
  pelado; los 11 tests del CAS Lua de `append_block` fallan sin `fakeredis[lua]`.
- [x] **`securityContext` en todos los workloads** (§3). No existía en ningún
  manifest: falta `runAsNonRoot`, `allowPrivilegeEscalation: false`, `capabilities.drop`.
- [x] **Tolerations/nodeSelector en Redis y RabbitMQ** (§3). El taint `pool=infra`
  existe en Terraform, pero solo con tolerations los pods de infra pueden
  schedulearse ahí. Sin esto, Redis/RabbitMQ caen en el nodepool `apps` y la
  separación infra/minería declarada en la docu no ocurre.
- [x] **Declaración de herramientas de IA** (§6). No existía en ningún README.
- [x] **NTP documentado** (§2). Nada configurado ni mencionado; los nodos GKE/COS
  sincronizan por defecto contra `metadata.google.internal` — dejarlo declarado.

> **✅ Validado en el deploy nuevo (2026-07-13, proyecto `voxchain-unlu`):**
> Redis ×3 + Sentinel ×3 + RabbitMQ ×3 `Running` como no-root (uid 999) en el
> nodepool `infra` — tolerations y securityContext funcionando en cluster real.
> Falta solo el frontend `nginx-unprivileged`, que se valida cuando el pipeline
> `03-apps` buildee las imágenes (el registry nuevo nace vacío).
> Bitácora completa del despliegue: `docs/informe/despliegue-gcp.md`.

## 🟠 Prioridad 3 — Métricas por tipo de recurso (§1) ✅

Implementadas en `common/metrics.py` e instrumentadas en `run_miner` (punto único
por donde pasa toda la minería CPU/GPU), el NCT y los handlers de desafío.
Cubierto por tests (`worker/tests/test_miner.py`).

- [x] Tasa de éxito CPU vs GPU →
  `voxchain_worker_mining_success_total / voxchain_worker_mining_tasks_total`
  filtrando por label `resource` (`cpu`/`gpu`). Un fallo del binario GPU cuenta
  como intento GPU sin éxito antes del fallback.
- [x] Hashes por segundo (por nodo) → Gauge `voxchain_worker_hashrate_hps{resource=}`
  (nonces probados / duración del intento; por nodo vía label `pod` de Prometheus).
- [x] Tiempos de minería por prefijo → Histogram
  `voxchain_worker_mining_duration_seconds{resource=, prefix_len=}`.
- [x] Latencia RabbitMQ → worker → Histogram
  `voxchain_worker_challenge_latency_seconds` (el NCT publica `published_at`
  en el desafío; el worker/pool observa la diferencia al recibirlo).
- [x] Tiempo de validación del bloque → Histogram
  `voxchain_nct_nonce_validation_seconds` (verify_nonce + try_seal + _seal).

Queries PromQL para el dashboard / informe:

```promql
# Tasa de éxito por recurso
rate(voxchain_worker_mining_success_total[5m]) / rate(voxchain_worker_mining_tasks_total[5m])
# Hashrate por nodo y recurso
voxchain_worker_hashrate_hps
# Tiempo de minería p95 por longitud de prefijo
histogram_quantile(0.95, sum by (le, prefix_len) (rate(voxchain_worker_mining_duration_seconds_bucket[5m])))
# Latencia RabbitMQ→worker p99
histogram_quantile(0.99, rate(voxchain_worker_challenge_latency_seconds_bucket[5m]))
# Tiempo de validación de bloque p95
histogram_quantile(0.95, rate(voxchain_nct_nonce_validation_seconds_bucket[5m]))
```

- [ ] (Opcional) Agregar paneles con estas queries al dashboard de Grafana
  (`pilar3-despliegue/kubernetes/monitoring/voxchain-dashboard.yaml`).

## 🟢 Prioridad 4 — Plataforma / defensa

- [x] **Plataforma de logging (colector)** (§2). Resuelto con la opción (a):
  documentado en `pilar3-despliegue/README.md` que Cloud Logging de GKE es el
  colector (Fluent Bit DaemonSet gestionado, N servicios × M réplicas), con la
  capa de aplicación (`logging_setup.py`: JSON a stdout + RotatingFileHandler
  en disco) como complemento.
- [x] **Alertas propias** (§2). Creado
  `pilar3-despliegue/kubernetes/monitoring/voxchain-alerts.yaml` (PrometheusRule,
  5 alertas: sin líder NCT, NCT sin métricas, ventanas sin sellar, sin GPU,
  latencia de desafío alta) + `ruleSelectorNilUsesHelmValues=false` en el Helm
  release de Terraform. **Aplicar con `kubectl apply` + `tofu apply` en el
  próximo deploy.**
- [ ] **Pull de imágenes del cluster k3s externo** (§3). `worker-deployment.yaml`
  apunta a Artifact Registry sin `imagePullSecrets` — verificar cómo autentica
  (¿repo público?) y documentarlo o agregar el secret.
- [ ] **Los pipelines de GitHub Actions nunca corrieron** (§5). Descubierto el
  2026-07-13: la API de Actions reporta 0 workflows registrados y 0 runs en
  toda la historia del repo (el despliegue anterior fue con
  `scripts/deploy-manual.sh`). Los workflows existen como código en `main`,
  pero GitHub no los registró. Para activarlos: hacer un push a `main` (p. ej.
  el merge de `dev`→`main` para la entrega) y verificar que `ci-checks`,
  `03-apps`, etc. aparezcan y corran en la pestaña Actions. Los GitHub Secrets
  ya están cargados (WIF, SA, RabbitMQ, K3S_KUBECONFIG). La checklist pide
  pipelines demostrables — hay que poder mostrar al menos un run verde de cada
  uno antes de la exposición.
- [x] **Diagrama de arquitectura en pilar 1** (§6). Agregado diagrama ASCII del
  pipeline GPU/CPU (grid-stride, fallback, interfaz CLI común) en
  `pilar1-minero/README.md`; corregida además la tabla de hits que apuntaba a
  archivos inexistentes.
- [ ] **TLS interno parcial** (§3) — *para defender, no necesariamente implementar*:
  RabbitMQ expone AMQPS 5671 con CA propia para workers externos ✅; Redis y el
  tráfico interno API↔NCT van sin TLS, mitigado con NetworkPolicies. Dejarlo
  explicado en el informe como decisión (TLS en el borde + segmentación interna).

## 📋 Puntos que YA cumplen (para la defensa, no tocar)

| Checklist | Evidencia |
|---|---|
| Claves pública/privada (§1) | `worker_pkg/identity.py`, firma en frontend, `test_signatures.py` |
| Pool coop + competitivo (§1) | Pool Coordinator + miners HTTP / workers standalone; SETNX sella al primero |
| Worker CPU y GPU (§1) | `cpu/src/brute_force.py`, `Dockerfile.gpu` (CUDA 12.4.1, `sm_61`) |
| Config CUDA documentada (§1) | `docs/informe/hit1-setup.md` + `Dockerfile.gpu` |
| Keep-alive con capacidad (§1) | keepalive con `capacity` + `has_gpu`; `pool:health:<id>` en Redis |
| Fallback sin GPU (§1) | fallback automático a CPU + HPA; dificultad fija es decisión declarada |
| BD / colas / secretos / ConfigMaps / HTTPS (§2) | Redis+Sentinel STS, RabbitMQ STS quorum, ESO+Secret Manager, cert-manager |
| Endpoint público de estado (§2) | `GET /api/health` → JSON `{api, nct, redis, workers}` vía Ingress TLS |
| Autoscaler + HPA + StatefulSets + PVC (§3) | nodepools con min/max, `hpa/`, STS con PVC |
| Namespaces + RBAC + zero static keys (§3) | `worker-rbac`, `rabbitmq-rbac`, WIF/OIDC en workflows |
| Logs memoria y disco (§3) | `common/logging_setup.py` (stdout + RotatingFileHandler) |
| Tests automatizados (§4) | 122 tests unit/integración, todos verdes |
| Pipelines 1..N + gitleaks (§5) | `01-infra` → `04-gpu-workers`, gitleaks rompe el CI |
| Sin secretos en el repo (§6) | `gustavo.yaml` ignorado y nunca commiteado; gitleaks activo |
