# TPIntegrador_SDyPP
Cierre de la cursada: Sistema distribuido completo de minería blockchain con CUDA.

## Estructura

| Pilar | Contenido |
|---|---|
| [`pilar1-minero/`](pilar1-minero/README.md) | Algoritmos de hashing y minería PoW en CPU (Python) y GPU (CUDA) |
| [`pilar2-distribuido/`](pilar2-distribuido/README.md) | Servicios distribuidos: API, NCT, workers, pools, RabbitMQ, Redis |
| [`pilar3-despliegue/`](pilar3-despliegue/README.md) | Kubernetes (GKE + k3s), Terraform/OpenTofu, CI/CD, observabilidad |

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
por el autor: la suite de tests (117 unitarios y de integración) y las corridas
de los pipelines de CI son el mecanismo de verificación. Las decisiones de
arquitectura son propias y están justificadas en la documentación: PoW de
gobierno, failover del NCT por lease atómico en Redis, elección del coordinator
del pool por mini-PoW, y el criterio de cifrar el borde y segmentar el interior.
