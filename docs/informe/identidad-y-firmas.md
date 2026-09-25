# Identidad, firmas y la clave privada en el navegador

Registro de una sesión de trabajo (25/09/2026) que empezó con una pregunta —*¿para
qué nos sirven las claves públicas y privadas?*— y terminó en dos cambios de
seguridad:

1. **Las firmas pasaron a ser obligatorias.** Hasta ahora eran opcionales en todos
   los despliegues, y eso dejaba proponer en nombre de cualquiera.
2. **Se arregló cómo se maneja la clave privada en el navegador.** Una "×" que
   decía "cerrar sesión" borraba la identidad para siempre, el respaldo no se
   podía usar desde la app y Trusted Types no se estaba exigiendo.

El documento sigue ese mismo orden: primero el concepto, después el recorrido de
una firma por el sistema, los dos hallazgos con lo que se hizo, y al final lo que
queda pendiente.

---

## 1. Para qué sirven las claves

En VoxChain **no hay cuentas, contraseñas ni sesiones: la identidad es tener la
clave privada**. Nadie guarda en ningún lado quién sos. Lo único que el sistema
verifica es que quien manda un mensaje tiene la clave que corresponde a una
clave pública.

Cada identidad es un par de claves ECDSA sobre la curva P-256
(`common/identity/signing.py`, `generate_private_key()`):

- **Clave privada:** es secreta y no sale nunca de quien la generó. Sirve para
  **firmar**.
- **Clave pública (`pubkey`):** se puede mostrar a cualquiera y viaja en los
  mensajes (`author_pubkey`). Sirve para **verificar** firmas y, a la vez, es el
  "nombre" de esa identidad en la red.

Una firma hecha con la privada se puede verificar con la pública, pero con la
pública no se puede fabricar una firma: de la pública no se deduce la privada.

### Dónde se firma

| Acción | Mensaje firmado | Quién firma | Qué impide |
|---|---|---|---|
| Proponer una ley | `author_pubkey\|action\|text_hash\|law_id\|created_at\|category` | Ciudadano | Proponer en nombre de otro (y dejarlo en cooldown) |
| Registrar un minero | `worker_id\|register\|timestamp` | Ciudadano | Gastar el cupo de mineros de otro |
| Dar de baja un minero | `worker_id\|delete\|timestamp` | Ciudadano | Borrarle un minero a otro |
| Administrar (equipos, modo, categorías) | `recurso\|acción\|timestamp` | Ciudadano | Administrar mineros ajenos |
| Responder un nonce | `voting_window_id\|nonce\|winning_node_or_pool` | Nodo (minero) | Atribuirse una victoria ajena y esquivar la regla 3.4 |

Tres detalles del diseño:

- **La firma no valida el trabajo.** El Proof of Work se verifica contando ceros del
  MD5, sin ninguna clave. La firma decide **a quién se le acredita** ese trabajo.
- **Hay dos identidades separadas.** El ciudadano tiene la suya, y cada minero genera
  su propio par al arrancar. Así la clave del ciudadano no tiene que viajar
  nunca hasta el minero. Las dos se vinculan con un token de un solo uso
  (`POST /api/workers/enroll`), sin mover ninguna clave privada.
- **Lo que las firmas no resuelven es el ataque Sybil.** Cualquiera puede generarse
  todas las identidades que quiera. Las firmas impiden hacerse pasar por *otro*,
  no que una misma persona tenga muchas identidades (AGENT.md §9).

---

## 2. Recorrido de una propuesta firmada

```
Navegador (Angular)              API (FastAPI)                 RabbitMQ          NCT
───────────────────              ─────────────                 ────────          ───
1. identidad (par P-256)
2. arma mensaje canónico
   y lo firma            ──POST /api/laws──▶ 3. verifica firma
                                               y text_hash
                                               ──publica──▶ cola `propuestas` ──▶ 4. vuelve a verificar
                                                                                    + frescura (anti-replay)
                                                                                    5. reglas de gobierno → encola
```

1. **Identidad** (`identity.service.ts`, `generateKeypair`). Se genera el par con Web
   Crypto. La pública se exporta como SPKI en base64. La privada se guarda como
   `CryptoKey` **no extraíble** en IndexedDB (ver §4).
2. **Firma en el navegador** (`propose-law.component.ts`). Se arma el mensaje
   `pubkey|action|text_hash|law_id|created_at|category` y se firma.
   - **Se firma el hash del texto, no el texto.** Si alguien cambia una coma en
     el camino, la verificación falla.
   - **El mensaje no es JSON.** Es una concatenación con `|` en orden fijo, para
     que sea idéntico byte a byte en TypeScript y en Python.
   - **La categoría va firmada.** Si no, un intermediario podría re-etiquetar la
     ley para que la mine otro equipo, o ninguno.
3. **El API verifica** (`routers/laws.py`, `_verify_proposal_signature`):
   - si falta un campo firmado, responde 400;
   - si `text_hash` no coincide con `sha256(text)`, responde 400;
   - si la firma no valida contra `author_pubkey`, responde 401.

   Web Crypto entrega la firma en formato `r||s` (64 bytes) y la librería
   `cryptography` de Python espera DER. `verify()` hace la conversión; sin ella
   fallaría toda verificación.
4. **El NCT vuelve a verificar** (`coordinator.py`, `_signature_ok`). No alcanza
   con que verifique el API: a la cola `propuestas` se puede llegar sin pasar por
   él. Además, el NCT rechaza un `created_at` con más de
   `PROPOSAL_MAX_AGE_SECONDS` (300 s) de antigüedad, para que no se pueda
   reenviar una propuesta capturada.
5. **Reglas de gobierno.** Recién después de la firma se evalúa quién puede
   proponer (3.2) y el cooldown (3.4), porque son condiciones sobre la
   identidad, y hasta ese punto no está probado que la propuesta sea suya.

---

## 3. Hallazgo 1: las firmas eran opcionales

`REQUIRE_SIGNATURES` estaba en `"false"` en el ConfigMap de Kubernetes y en los dos
compose. Ese es un **modo de migración**:

- una firma **inválida** se rechaza;
- una propuesta **sin firma** se acepta, y el NCT solo lo registra en el log.

En la práctica, `POST /api/laws` con `author_pubkey = <la de otro>` y **sin**
`signature` pasaba. Eso es justo lo que las firmas tenían que impedir.

### Qué se cambió (commit `d3629c5`)

**La bandera en `true`** en `voxchain-config.yaml`, `docker-compose.yml` y
`docker-compose.scale.yml`. También se cambió el **default del código** en
`common/config.py` y `voxchain_api/config.py`: si la variable falta o está vacía,
se exige firma. El modo permisivo hay que pedirlo explícitamente con `false`.

**Claves de nodo para los mineros.** La misma bandera hace que el NCT descarte
los **nonces** sin firma, y un minero solo firma si tiene `WORKER_PRIVKEY_PEM`
configurado. Los mineros del compose y los Deployments `worker`,
`pool-coordinator` y `pool-miner` no lo tenían: con solo cambiar la bandera,
esos mineros habrían seguido minando pero sin sellar ningún bloque. Ahora
cada uno genera su clave al arrancar: en Kubernetes en un `emptyDir` en memoria
(igual que `gpu-miner`), en compose en `/tmp`.

**Todo lo que envía propuestas o nonces ahora firma:**

| Productor | Cambio |
|---|---|
| `run.sh demo` | La propuesta de ejemplo se firma dentro del contenedor del API, en el mismo paso que registra la identidad de demo. La clave no sale de ahí. |
| `load-tests/scenarios/*` | Helper nuevo `firma.py`: una identidad nueva por propuesta, para no chocar con el cooldown. El mensaje canónico se importa de `common.identity` en vez de copiarlo, así no se puede desincronizar. |
| `scripts/demo_carga.py` | Firma promulgaciones y derogaciones. En una derogación firma la categoría de la **ley original**, que es la que impone el API. |
| `tests/stress` | Firmar pasa a ser el default (`USE_SIGNATURES=true`). `mining_race` y la carrera de `demo.py` inyectaban nonces sin firma; ahora cada competidor firma con su propia clave de nodo (`NodeIdentity`). |

**Tests.** `test_proposers_api.py` mandaba propuestas sin firma. Ahora firma con
claves reales, y se agregaron dos tests:

- `test_sin_firma_401_antes_que_la_restriccion`: una propuesta sin firma con la
  pubkey de un dueño válido se rechaza con 401. Es el caso de suplantación.
- `test_require_signatures_default_true`: sin la variable de entorno, los dos
  configs arrancan exigiendo firma.

### Verificación

- Los 537 tests pasan.
- Un script aparte, en modo estricto, contra la verificación real del API y del
  NCT:
  - una propuesta sin firma da 401;
  - las propuestas de `firma.py`, `demo_carga` y `SignedIdentity` las aceptan el
    API y el NCT, y el NCT abre la ventana;
  - un nonce firmado sella el bloque y uno sin firma se descarta.
- **En vivo** (`./run.sh demo`): la propuesta firmada de `run.sh` se selló, y el
  NCT registró como ganador del bloque la **pubkey de un nodo**. Eso prueba
  que los workers firman sus nonces con su propia clave.

### Consecuencias a tener en cuenta

- `load-tests` y `demo_carga.py` ahora necesitan `pip install cryptography`.
- **Caída del NCT:** las propuestas firmadas que esperen en RabbitMQ más de 300 s
  se descartan por el control anti-replay. Si en la demo de failover el NCT de
  respaldo tarda más que eso en tomar el control, esas propuestas se pierden.

---

## 4. Hallazgo 2: la clave privada en el navegador

### Cómo se guarda

- **Clave privada:** es un `CryptoKey` **no extraíble** en IndexedDB (base
  `voxchain-identity`, entrada `signing-key`). La API de Web Crypto no deja leer
  sus bytes desde JavaScript (`exportKey` lanza `InvalidAccessError`). Solo se le
  pueden pedir firmas.
- **localStorage:** guarda solo la parte pública (pubkey y nombre para mostrar).
- **Identidades viejas:** las que tenían la privada en texto plano en
  localStorage se migran solas al abrir la app, y el original se borra.
- **Respaldo:** al crear la identidad, la clave se genera extraíble por un
  instante para mostrar el PEM **una sola vez**.

### ¿Es vulnerable?

| Amenaza | ¿Protegida? | Por qué |
|---|---|---|
| Un XSS **roba** la clave | ✅ Sí | No se puede exportar. |
| Un XSS **firma en tu nombre** | ❌ No | Cualquier script del mismo origen abre IndexedDB y llama a `crypto.subtle.sign` sin que el usuario haga nada. Un XSS **almacenado** se vuelve a ejecutar en cada visita. |
| Extensión maliciosa con permiso sobre el sitio | ❌ No | Comparte el almacenamiento del origen: es equivalente a un XSS. |
| Robo del disco, del perfil del navegador o malware local | ❌ No | "No extraíble" es una restricción **de la API, no cifrado**: el navegador guarda los bytes de la clave en el perfil sin cifrar. |
| El respaldo PEM | ⚠️ Débil | Se muestra en claro, sin passphrase, y el botón lo copia al portapapeles, donde lo alcanzan los gestores de portapapeles y la sincronización entre dispositivos. |
| **Pérdida** de la identidad | ❌ Grave | Ver la lista de abajo. |

Los problemas de pérdida, que eran los más urgentes:

1. **La "×" del header, titulada "Cerrar sesión", borraba la clave para siempre**,
   de un clic y sin confirmación.
2. **No había forma de restaurar el respaldo en la UI.** El PEM solo servía con
   `scripts/propose_law.py`, aunque la pantalla decía que era "la única forma de
   no perder tu identidad".
3. **Crear una identidad teniendo otra activa pisaba la anterior sin avisar**,
   porque las dos se guardan en la misma entrada de IndexedDB.
4. **IndexedDB es *best-effort*:** el navegador puede borrarlo si le falta espacio,
   y Safari borra el almacenamiento de un sitio después de 7 días sin visitarlo.

Además, **Trusted Types** (la protección contra XSS por `innerHTML` y similares)
estaba solo en `Report-Only` y sin un endpoint para los reportes. En la práctica no
bloqueaba nada ni le avisaba a nadie.

### El plan, en tres niveles

| Nivel | Qué | Estado |
|---|---|---|
| 1 | Dejar de perder identidades y exigir Trusted Types | ✅ Hecho (§5) |
| 2 | Cifrar la clave en reposo con una passphrase | Pendiente (§6) |
| 3 | Passkeys (WebAuthn) | Trabajo futuro (§6) |

---

## 5. Nivel 1: lo que se hizo

**Se eliminó la "×" del header** (`app.component.ts`). No hay sesión que cerrar: la
identidad es la clave guardada, y el único "salir" posible es borrarla. Eso ahora
está solo en `/identity`, con el botón "Borrar identidad de este navegador" y una
confirmación que explica que sin el respaldo no hay forma de recuperarla.

**Se agregó la confirmación al reemplazar** (`identity.component.ts`). Crear o
restaurar teniendo una identidad activa pregunta antes. Además hay una red de
seguridad en el servicio: `generateKeypair` y `restoreFromPem` lanzan un error si
ya hay una identidad y no se les pasa `{ replace: true }`, así ningún otro
llamador la puede pisar por accidente.

**Se agregó "Restaurar desde respaldo"**, con `IdentityService.restoreFromPem`:

- Se pega el PEM y, opcionalmente, un nombre para mostrar.
- La pública se **deriva de la privada**: se importa la privada, se exporta como
  JWK, que trae las coordenadas `x`/`y`, y con eso se reconstruye la pública en
  SPKI. No hace falta pedirla aparte ni confiar en que el usuario pegue la
  correcta.
- Para derivarla, la privada se importa extraíble un instante. No expone nada
  nuevo, porque el PEM ya está en JavaScript desde que el usuario lo pegó. Lo
  que se guarda es, como siempre, una reimportación no extraíble.
- El campo del PEM se vacía apenas se usa.

**Persistencia del almacenamiento.** Al crear o restaurar se llama a
`navigator.storage.persist()`. Se hace justo después de una acción del usuario
porque Firefox muestra un permiso y conviene que aparezca en contexto. Si el
navegador no lo concede, la pantalla muestra el aviso "Tu navegador podría borrar
la clave por su cuenta".

**Trusted Types pasó a modo obligatorio** (`nginx.conf`):
`require-trusted-types-for 'script'; trusted-types angular`. Solo la política del
sanitizador de Angular puede escribir en `innerHTML` y los demás puntos de
inyección. Antes de exigirlo se comprobó que el bundle solo crea la política
`angular` y que ninguna pantalla genera violaciones.

**Documentación.** AGENT.md §3.1 ahora describe la restauración, el borrado
explícito, la persistencia y Trusted Types. También deja explícito lo que sigue sin
cubrir: un XSS que llegue a ejecutarse puede *usar* la clave, y el cifrado en
reposo está pendiente.

### Verificación en el navegador (stack local con `./run.sh demo`)

| Prueba | Resultado |
|---|---|
| Crear identidad | Clave en IndexedDB con `extractable: false` y `usages: ["sign"]`; `exportKey` lanza `InvalidAccessError`. localStorage guarda solo pubkey y nombre. |
| Header | Solo queda "Mi identidad"; la "×" ya no existe. |
| Aviso de persistencia | El navegador de prueba no concedió persistencia (`persisted: false`) y el aviso apareció. |
| Crear otra identidad y **cancelar** | Aparece la confirmación; la identidad original no cambia. |
| Borrar y **cancelar** / **aceptar** | Cancelando no pasa nada. Aceptando se borran la clave de IndexedDB y la parte pública de localStorage. |
| Restaurar con texto inválido | "Eso no parece una clave secreta de VoxChain…"; no se crea nada. |
| Restaurar con el respaldo real | Misma pubkey que la original, clave no extraíble, campo del PEM vacío. Una firma de la clave restaurada verifica contra la pubkey original. |
| Trusted Types en Report-Only | Recorriendo las 9 rutas, **cero violaciones**. Una violación provocada a propósito sí se registró, así que la detección funcionaba. |
| Trusted Types **obligatorio** | Las 9 rutas cargan sin errores. Con la identidad restaurada se registró un minero y se propuso una ley desde la UI: el API y el NCT aceptaron las dos firmas en modo estricto (`ley-6447e21e` encolada). |

El build de Angular (`ng build`) pasa sin avisos nuevos. El componente de
identidad queda dentro del presupuesto de estilos.

---

## 6. Lo que queda pendiente

### Nivel 2: cifrar la clave con una passphrase

Es el paso que AGENT.md ya marcaba como siguiente:

- Derivar una clave de cifrado a partir de la passphrase con **PBKDF2**, que Web
  Crypto trae de forma nativa. Argon2id sería mejor, pero necesita WASM y habría
  que aflojar la CSP con `wasm-unsafe-eval`.
- Guardar en IndexedDB **solo la privada cifrada** (`wrapKey` con AES-GCM).
- Al entrar, `unwrapKey(..., extractable=false)`: la clave se descifra dentro de
  Web Crypto y sus bytes nunca pasan por JavaScript.
- Mantener la clave descifrada **solo en memoria**. Al cerrar la pestaña, nadie
  puede firmar sin volver a escribir la passphrase, tampoco un XSS almacenado en
  una visita futura.
- Reemplazar el PEM en el portapapeles por un archivo de respaldo cifrado.

Tiene dos costos que hay que asumir:

- **Trade-off con XSS:** si hay un XSS activo en el momento en que el usuario
  escribe la passphrase, puede capturarla junto con el blob cifrado y
  descifrarlo en otro lado.
- **No se puede aplicar a las identidades actuales.** `wrapKey` solo funciona con
  claves extraíbles, y las que ya existen no lo son. Cada usuario tendría que
  reimportar su respaldo (el flujo del nivel 1 ya lo permite) o crear una
  identidad nueva.

### Nivel 3: passkeys (WebAuthn)

La clave vive en el hardware del dispositivo (Secure Enclave, TPM o una llave
física) y **cada firma exige un gesto del usuario**, como la huella o el PIN.
Resuelve a la vez el robo del disco y el XSS que firma en silencio, y las passkeys
que se sincronizan resuelven el respaldo. Usa P-256, igual que ahora.

Los costos:

- **Hay que cambiar la verificación en el API y en el NCT.** WebAuthn no firma el
  mensaje tal cual: firma `authenticatorData || sha256(clientDataJSON)`, y el
  `challenge` tiene que corresponder al mensaje canónico.
- **Las passkeys quedan atadas al dominio.** El despliegue usa
  `voxchain.<ip>.sslip.io`: si cambia la IP del LoadBalancer, cambia el dominio y
  todas las passkeys dejan de servir. Hace falta un dominio estable antes de
  considerarlo.

### Otros hallazgos de la sesión

- **Las fuentes no cargan.** La CSP (`font-src 'self'`) bloquea las fuentes de
  Google que el build de Angular referencia (`fonts.gstatic.com`). La app se ve
  con fuentes de reemplazo, y la consola muestra cientos de errores de CSP. Se
  arregla sirviendo las fuentes desde el propio origen o permitiendo ese dominio
  en `font-src`.
- **El valor por defecto del constructor de `NCTCoordinator`** (`require_signatures=False`)
  se dejó como estaba. En producción no se usa, porque `main.py` siempre le pasa
  el valor de la configuración. Solo lo usan los tests unitarios del NCT que no
  prueban firmas.

---

## 7. Archivos tocados

**Firmas obligatorias** (commit `d3629c5`): `common/config.py`,
`voxchain_api/config.py`, `docker-compose.yml`, `docker-compose.scale.yml`,
`voxchain-config.yaml`, los Deployments `worker`, `pool-coordinator` y
`pool-miner`, `run.sh`, `load-tests/scenarios/{firma,test_bulk,test_difficulty,test_fragmentation,test_resources}.py`,
`scripts/demo_carga.py`, `tests/stress/**`, `tests/test_proposers_api.py` y
`docs/informe/INFORME.md`.

**Clave en el navegador, nivel 1:**
`voxchain-frontend/src/app/core/services/identity.service.ts`,
`voxchain-frontend/src/app/features/identity/identity.component.ts`,
`voxchain-frontend/src/app/app.component.ts`,
`voxchain-frontend/nginx.conf` y `AGENT.md`.
