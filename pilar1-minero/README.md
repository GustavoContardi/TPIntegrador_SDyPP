# Pilar 1 — Minería CPU + GPU CUDA

Desarrollo progresivo de los algoritmos de hashing y minería Proof of Work.

## Arquitectura

```
                 desafío: (base, prefijo, [range_min, range_max))
                                     │
              ┌──────────────────────┴──────────────────────┐
              ▼                                             ▼
   GPU — CUDA C/C++ (gpu/)                       CPU — Python (cpu/)
   ┌───────────────────────────┐                 ┌───────────────────────┐
   │ Host                      │                 │ brute_force.py        │
   │  · reserva rango          │    fallback     │  · loop secuencial    │
   │  · lanza kernel           │ ◄─ automático ─ │    md5(base+nonce)    │
   │ Device (kernel MD5)       │   (sin GPU /    │  · misma CLI y salida │
   │  · N bloques × M threads  │    error CUDA)  │    "Nonce = N"        │
   │  · grid-stride loop:      │                 └───────────────────────┘
   │    nonce += grid×block    │
   │  · flag global found_nonce│
   │    corta la búsqueda      │
   └───────────────────────────┘
              │                                             │
              └──────────────────────┬──────────────────────┘
                                     ▼
                      Nonce = N  (MD5 empieza con el prefijo)
                Consumido por el worker del Pilar 2 (subproceso)
```

- **Interfaz común**: ambos mineros reciben `base prefijo range_min range_max`
  por CLI y emiten `Nonce = N` + el hash — el worker del Pilar 2 (`miner.py`)
  los invoca de forma intercambiable y parsea la salida con la misma regex.
- **CUDA**: toolkit 12.4.1, arquitectura `sm_61`, `cudart` estático (el runtime
  NVIDIA del nodo monta el driver). Desarrollo inicial en Colab (T4, CUDA 12.x);
  detalle en `docs/informe/hit1-setup.md`.

## Estructura

```
cpu/            — Minero CPU (Python/Node/Java)
gpu/            — Minero GPU en CUDA C/C++
benchmarks/     — Comparativas CPU vs GPU
```

## Hits desarrollados

| Hit  | Archivo GPU               | Descripción                          |
|------|---------------------------|--------------------------------------|
| #2   | `01_hello.cu`             | Hello World CUDA                     |
| #3   | `02_thrust_vector.cu`     | Thrust Vectors (CCCL)                |
| #4   | `03_md5_hash.cu`          | MD5 de un string por parámetro       |
| #5   | `04_brute_force.cu`       | Fuerza bruta (hash + cadena → nonce) |
| #6   | `benchmarks/run_prefix_bench.py` | Métricas por longitud de prefijo (sobre el binario del hit #7) |
| #7   | `05_brute_force_range.cu` | Fuerza bruta con límites de rango    |

## Ejecución

```bash
cd gpu
make 04   # compila y ejecuta el hit #4
```

## Decisiones de diseño

- Un `.cu` por hit, autónomo, reutiliza kernels desde `include/`
- Makefile genera un binario por fuente objetivo
- CPU equivalente en `cpu/src/` para comparativa final
