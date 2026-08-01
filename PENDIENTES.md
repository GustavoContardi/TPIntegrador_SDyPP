# PENDIENTES — Gaps contra la checklist oficial

> Resultado de la auditoría local contra `2026_SDYPP_checklist-blockchain-v2.docx.pdf`
> (2026-07-13). Suite de tests validada localmente: **122/122 pasan** (requiere
> `fakeredis[lua]`). Sin secretos commiteados (verificado en historial git).
>
> Cada ítem tiene su sección de la checklist entre paréntesis. Marcar con `[x]` al resolver.

---

## 🔴 Prioridad 1 — Bloqueantes de la entrega

- [x] **Correr los escenarios de carga y guardar resultados** (§4, §7).
  ✅ Corridos el 2026-07-14 contra el despliegue real (GKE + workers k3s) con
  `run_all_cloud.sh` (nuevo: varía `N_ZEROS` en el ConfigMap de GKE y
  `FRAGMENT_SIZE` en los coordinators del k3s entre corridas, y restaura al
  terminar). Resultados en `pilar3-despliegue/load-tests/resultados/*.csv`.
  Los scripts originales tenían bugs contra el API real (campo `author` en vez
  de `author_pubkey`, `--api-url` ignorado, fragmentación que no medía sellado)
  — arreglados.
  - [x] Bulks de transacciones (`test_bulk.py`): 1/10/100/1000 → throughput
    escala de 0.6 a 23 props/s; el lote de 100.000 de la checklist queda
    pendiente si se considera necesario (a 23 props/s serían ~72 min de ingesta).
  - [x] Dificultad de prefijo (`test_difficulty.py`): 1→6 ceros, curva
    exponencial (0.3s → 107s). Con 7-8 ceros y workers CPU el sellado se va a
    horas; documentar como límite de la config CPU-only.
  - [x] Fragmentación del pool (`test_fragmentation.py`): 1%→50%. A dificultad 4
    el nonce ganador aparece muy temprano en el espacio, así que el tamaño del
    fragmento casi no incide (2.5-3.7s; 0.47s con 50%).
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

## 🔵 Decisión de diseño abierta (heredada del TODO de workers, ya resuelto)

- [ ] **¿Persistir el modo del worker entre reinicios?** El ConfigMap
  `worker-modes` y el RBAC de `backend-proxy` son restos de un diseño a medio
  hacer: el worker LEE el ConfigMap al arrancar pero nada lo escribe, así que
  un switch-mode por RabbitMQ se pierde si el pod se reinicia. Decidir:
  (a) conectar `switch_worker_mode` para que patchee el ConfigMap (la API ya
  tiene acceso al k3s vía kubeconfig, mismo mecanismo del spawn), o
  (b) aceptar el switch efímero y borrar el código/RBAC muerto
  (`backend-proxy-rbac.yaml`, `worker-modes-configmap.yaml`,
  `_read_mode_from_configmap`). Nota: el SA `gustavo` no puede crear
  roles/rolebindings en el k3s, así que el RBAC de backend-proxy no se puede
  aplicar de todos modos.

## 🐛 Bug de failover encontrado el 2026-07-14 (fix en repo, falta redeploy)

- [ ] **Rebuild + redeploy de la imagen del NCT con el fix de failover.**
  Al correr los load tests se descubrió que un NCT follower que arranca fresco
  y nunca recibe un heartbeat jamás dispara la elección
  (`nct/monitor.py`: `_last_heartbeat = 0.0` cortocircuitaba el `tick()`).
  Escenario real: el líder muere mientras el follower se reinicia → clúster
  acéfalo hasta reinicio manual. Además, `rollout restart` del NCT se
  deadlockea: el pod nuevo (surge) no puede tomar el lease mientras el viejo
  lo renueva → nunca pasa a Ready (para reiniciar: scale 0 → esperar TTL del
  lease 15 s → scale 1, como hace `run_all_cloud.sh`).
  Fix aplicado en `monitor.py` (el timeout corre desde el arranque/stepdown)
  con test de regresión (`test_follower_fresco_con_lider_muerto_dispara_eleccion`,
  verificado que falla contra el código viejo); 44/44 tests del NCT verdes.
  **La imagen desplegada en GKE todavía tiene el bug** — rebuildear y rolear
  en el próximo deploy.

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
