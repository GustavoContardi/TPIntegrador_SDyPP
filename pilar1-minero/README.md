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
- **CUDA**: hay dos compilaciones, para dos placas distintas.
  - **Desarrollo** (`gpu/Makefile`): `-arch=sm_75`, la Tesla T4 de Google Colab
    donde se hicieron los hits (CUDA 12.x). La máquina local no tiene GPU
    NVIDIA; detalle en [`hit1-setup.md`](../docs/informe/hit1-setup.md).
  - **Despliegue** (`pilar2-distribuido/worker/Dockerfile.gpu`): toolkit
    **12.4.1**, `-arch=sm_61` y `cudart` enlazado estático. `sm_61` es Pascal
    (compute capability 6.1), la GTX 1060 del clúster k3s donde corren los
    workers. `-arch=sm_61` embebe el binario para esa arquitectura **y** el PTX
    de `compute_61`, que el driver compila al vuelo en placas más nuevas: la
    misma imagen corre en la 1060 y en una T4. Al revés no: un binario `sm_75`
    no corre en la 1060.
  - La imagen de runtime es `python:3.11-slim` y no trae `libcudart`: el driver
    lo monta el runtime de NVIDIA del nodo, y sólo en los pods que piden
    `nvidia.com/gpu`. Sin GPU, el self-test del binario falla y el worker mina
    con CPU.

## Estructura

```
cpu/            — Minero CPU (Python)
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

Desde la raíz del repo, sin configurar nada:

```bash
./run.sh miner                          # base "voxchain", prefijo "00000", rango [0, 50M)
./run.sh miner hola 0000 0 100000000    # base, prefijo, desde, hasta
```

Si hay `nvcc` compila y corre el minero GPU (hit #7); si no, corre el minero CPU,
que es el mismo fallback que usan los workers del Pilar 2.

A mano:

```bash
# GPU: compila un hit (make <nombre del .cu sin extensión>) y lo ejecuta con ARGS
cd gpu
make 05_brute_force_range ARGS='hola 0000 0 100000000'
./bin/05_brute_force_range hola 0000 0 100000000

# CPU: misma interfaz
python3 cpu/src/brute_force.py hola 0000 0 100000000
```

Benchmarks CPU vs GPU y por longitud de prefijo: ver
[`benchmarks/README_TESTS.md`](benchmarks/README_TESTS.md).

## Decisiones de diseño

- Un `.cu` por hit, autónomo, reutiliza kernels desde `include/`
- Makefile genera un binario por fuente objetivo
- CPU equivalente en `cpu/src/` para comparativa final
