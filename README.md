# TPIntegrador_SDyPP
Cierre de la cursada: Sistema distribuido completo de minería blockchain con CUDA.

## Estructura

| Pilar | Contenido |
|---|---|
| [`pilar1-minero/`](pilar1-minero/README.md) | Algoritmos de hashing y minería PoW en CPU (Python) y GPU (CUDA) |
| [`pilar2-distribuido/`](pilar2-distribuido/README.md) | Servicios distribuidos: API, NCT, workers, pools, RabbitMQ, Redis |
| [`pilar3-despliegue/`](pilar3-despliegue/README.md) | Kubernetes (GKE + k3s), Terraform/OpenTofu, CI/CD, observabilidad |

## Cómo ejecutar

Todo se corre desde la terminal, sin abrir un IDE. El único punto de entrada es
`run.sh`:

```bash
./run.sh demo       # levanta el sistema completo en local y sella una ley
./run.sh test       # corre la suite de tests (132)
./run.sh miner      # compila (si hay CUDA) y corre el minero de Pilar 1
./run.sh scale      # experimento de escalado: N transacciones con M vs 2xM
./run.sh bench      # techo de cómputo de esta máquina
./run.sh graficos   # regenera los gráficos del informe
./run.sh stop       # baja el sistema local
```

`./run.sh` sin argumentos muestra la ayuda. Cada subcomando verifica sus
prerequisitos (Docker, CUDA, dependencias de Python) y dice qué falta si no
están; el entorno virtual se crea solo la primera vez.

**Único requisito para `demo`:** Docker corriendo (`sudo systemctl start docker`).
Después de `./run.sh demo` el sistema queda en <http://localhost:4200> (frontend)
y <http://localhost:8000/api/health> (estado).

Cuánto tarda `demo`: **~75 s** con las imágenes ya construidas, casi todo en
levantar los contenedores y esperar los healthchecks (el minado a dificultad 4
son un par de segundos). **La primera vez tarda varios minutos más**, porque hay
que construir las cuatro imágenes desde cero — incluido el build de Angular del
frontend.

Para el despliegue en Kubernetes, la guía paso a paso está en el
[README de Pilar 3](pilar3-despliegue/README.md).

## Informe final

El informe con la comparativa de resultados, el análisis de escalado, los
gráficos y la reflexión crítica está en
**[`docs/informe/INFORME.md`](docs/informe/INFORME.md)**.

## Herramientas de IA utilizadas

Declaración requerida por la consigna (§6 de la checklist):

- **Claude Code (Anthropic)** — asistente principal durante el ciclo de
  desarrollo: exploración y auditoría del código, escritura de tests,
  manifests de Kubernetes y Terraform, documentación técnica
  (`FUNCIONAMIENTO.md`, informes de hits) y revisión de decisiones de diseño.
  Los archivos `AGENT.md` y `CONTEXTO.md` son el contexto de dominio que se le
  dio al agente para trabajar con las reglas de negocio cerradas.
<!-- Si se usaron otras herramientas (Copilot, ChatGPT, Cursor...), agregarlas acá. -->

Todo el código generado con asistencia de IA fue revisado, entendido y validado
por el autor: la suite de tests (132 unitarios y de integración) y las corridas
de los pipelines de CI son el mecanismo de verificación. Las decisiones de
arquitectura son propias y están justificadas en la documentación: PoW de
gobierno, failover del NCT por lease atómico en Redis, elección del coordinator
del pool por mini-PoW, y el criterio de cifrar el borde y segmentar el interior.
