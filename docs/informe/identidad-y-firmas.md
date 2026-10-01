# Identidad, firmas y la clave privada en el navegador

Registro de una sesión de trabajo (25/09/2026) que empezó con una pregunta —*¿para
qué nos sirven las claves públicas y privadas?*— y terminó en cuatro cambios de
seguridad:

1. **Las firmas pasaron a ser obligatorias.** Hasta ahora eran opcionales en todos
   los despliegues, y eso dejaba proponer en nombre de cualquiera.
2. **Se arregló cómo se maneja la clave privada en el navegador.** Una "×" que
   decía "cerrar sesión" borraba la identidad para siempre, el respaldo no se
   podía usar desde la app y Trusted Types no se estaba exigiendo.
3. **La clave privada pasó a guardarse cifrada con una contraseña.** En disco
   queda solo un blob cifrado, la clave usable vive en memoria y el respaldo es un
   archivo cifrado que también sirve para el CLI.
4. **La identidad puede vivir en una passkey (WebAuthn).** La clave no está en el
   navegador, cada firma pide huella o PIN, y el backend verifica las firmas
   WebAuthn en el mismo punto que todas las demás.

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

1. **Identidad** (`identity.service.ts`). La pública se exporta como SPKI en
   base64. La privada, preferentemente, vive en una **passkey**
   (`createPasskey`): la genera el autenticador del dispositivo y nunca sale de
   ahí. Si no, se genera con Web Crypto (`generateKeypair`) y se guarda en
   IndexedDB **cifrada con una contraseña**. Cómo se llegó a esto está en §4
   a §7, y el estado final en §8.
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
| 1 | Dejar de perder identidades y exigir Trusted Types | ✅ Hecho, commit `da70610` (§5) |
| 2 | Cifrar la clave en reposo con una contraseña | ✅ Hecho, commit `d60a543` (§6) |
| 3 | Passkeys (WebAuthn) | ✅ Hecho, commit `91ce8c6` (§7) |

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

**Se agregó "Restaurar desde respaldo"**, con `IdentityService.restoreFromPem`
(en el nivel 2 pasó a llamarse `restore` y acepta también el respaldo cifrado):

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

## 6. Nivel 2: la clave cifrada con una contraseña

### Diseño

| Pieza | Qué es |
|---|---|
| Derivación | PBKDF2-HMAC-SHA256 con 600.000 iteraciones (recomendación OWASP 2023) y sal aleatoria de 16 bytes. La contraseña se normaliza a NFC para que Web Crypto y Python vean los mismos bytes. |
| Cifrado | AES-GCM-256 con IV aleatorio de 12 bytes (`crypto.subtle.wrapKey('pkcs8', …)`). La **pubkey va como dato autenticado (AAD)**: si le cambian la pubkey al registro, no se descifra. |
| Qué hay en disco | En IndexedDB, un único registro `vault` con `pubkey`, `kdf`, `cipher` y `wrapped`. No hay ningún `CryptoKey` ni ningún hash de la contraseña: una contraseña equivocada simplemente no descifra. |
| Qué hay en memoria | La clave utilizable, como `CryptoKey` **no extraíble** obtenido con `unwrapKey`. Sus bytes en claro nunca pasan por JavaScript. Al crear la identidad, el PKCS#8 lo produce `wrapKey` directamente cifrado. |
| Respaldo | El mismo registro, descargable como `.json` en cualquier momento (también con la identidad bloqueada), porque sin la contraseña no sirve de nada. El formato está versionado (`"format": "voxchain-identity", "v": 1`). |

**Flujo de uso.** Al abrir la app, la identidad está **bloqueada**. La primera vez
que algo necesita firmar, `IdentityService.sign()` abre un pedido de contraseña
global (`UnlockPromptComponent`) y, cuando el usuario desbloquea, la firma sigue
sola. Las pantallas que firman (proponer, mineros, equipos, deliberación) no
cambiaron: todas pasan por `sign()`. "Bloquear", en el header o en `/identity`,
olvida la clave de memoria; es lo más parecido a "cerrar sesión", pero sin borrar
nada.

**Identidades existentes (`legacy`).** Las creadas antes siguen funcionando, con la
etiqueta "sin contraseña" y un aviso. No se pueden cifrar retroactivamente, porque
`wrapKey` solo acepta claves extraíbles y estas no lo son. Se protegen restaurando
su PEM: la pantalla detecta que es un respaldo viejo y pide una contraseña nueva.

**CLI.** `common/identity/backup.py` implementa el mismo formato en Python
(`encrypt_backup`, `decrypt_backup`, `load_backup`), y `scripts/propose_law.py`
acepta `--backup archivo.json`. La contraseña se pide con `getpass`, o se toma de
`VOXCHAIN_BACKUP_PASSPHRASE`; nunca por argumento, porque quedaría en el historial
del shell y en `ps`. Además de descifrar, comprueba que la privada corresponda a la
pubkey del registro.

### Dónde está cada cosa, sin jerga

Dos preguntas que salieron al explicar el diseño.

#### ¿La clave privada se guarda en disco?

Sí, **cifrada**. Y no es nuevo: la clave siempre estuvo en el disco de la
computadora del usuario. Lo que cambió es **en qué forma** está.

- **En el disco de quién:** en el del usuario. IndexedDB es el almacenamiento del
  navegador y vive en una carpeta de su perfil. Nunca va a ningún servidor: ni al
  API, ni a Redis, ni a nada de VoxChain. Eso no cambió en ningún nivel.

| | Qué hay en el disco | Quién puede usar la clave |
|---|---|---|
| **Antes** (y las identidades `legacy`) | La clave privada **tal cual**. La app no podía leerla, pero el archivo estaba ahí sin protección. | Cualquiera que copie la carpeta del navegador (malware, alguien con acceso a la máquina). |
| **Ahora** | Un **bloque cifrado**. Sin la contraseña es ruido. | Solo quien sepa la contraseña. |

Cómo se usa para firmar:

1. Al abrir la app, en el disco solo está la versión cifrada: la identidad aparece
   **bloqueada**.
2. Al hacer algo que necesita firma, la app pide la contraseña.
3. Con la contraseña, el navegador descifra la clave y la deja **en la memoria
   RAM**, no en el disco.
4. Mientras la pestaña esté abierta, firma sin volver a preguntar.
5. Al cerrar la pestaña o tocar "Bloquear", la clave desaparece de la memoria. En
   el disco sigue estando solo la versión cifrada.

Es como una caja fuerte: la caja (el bloque cifrado) queda guardada en casa, y se
abre solo cuando hace falta. El **respaldo** que se descarga es exactamente ese
bloque cifrado. Por eso se puede guardar en cualquier lado (un pendrive, el mail,
la nube): sin la contraseña no sirve de nada. La contracara es que si se olvida la
contraseña, ni el respaldo salva la identidad.

#### ¿Y la contraseña dónde se guarda?

**En ningún lado:** ni en el disco, ni en el servidor, ni en el navegador. Tampoco
un hash de ella.

**¿Cómo sabe la app si la contraseña es correcta?** No la compara contra nada
guardado. Cuando el usuario la escribe:

1. El navegador **deriva** una clave de cifrado a partir de la contraseña, con
   PBKDF2. Es una cuenta matemática que da siempre el mismo resultado para la
   misma contraseña.
2. Con esa clave intenta **descifrar** el bloque del disco.
3. Si la contraseña es la correcta, sale la clave privada. Si es otra, AES-GCM
   detecta que el resultado no es válido y falla. Ese fallo es el mensaje "La
   contraseña no es correcta."

Es como un candado de combinación: no tiene la combinación anotada adentro,
simplemente abre con la correcta y no abre con las demás.

Dónde existe la contraseña, y por cuánto tiempo:

- **Mientras se tipea**, en el campo del formulario.
- **Durante el desbloqueo** (medio segundo, aproximadamente), se usa para derivar
  la clave de cifrado. Esa clave derivada tampoco se guarda: se usa una vez y se
  descarta. En cada desbloqueo se vuelve a calcular.
- **Después del desbloqueo**, la app vacía el campo. Lo que queda en memoria es la
  **clave privada descifrada**, no la contraseña.

Un matiz: JavaScript no permite borrar un texto de la memoria a voluntad. La copia
del string puede quedar en la RAM hasta que el recolector de basura la limpie.
Nunca va al disco, pero no se puede garantizar que desaparezca de la RAM en el
instante exacto.

**La única excepción es que la guarde el usuario.** Los campos están marcados
como contraseña (`autocomplete="new-password"` y `"current-password"`). Si el
usuario tiene activado el gestor de contraseñas del navegador, o uno como Bitwarden
o 1Password, le va a ofrecer guardarla. Si acepta, queda en ese gestor, con su
propio cifrado. Es su decisión, y razonable: si la olvida, pierde la identidad.

### Qué mejora y qué empeora

| Amenaza | Antes (nivel 1) | Ahora |
|---|---|---|
| Robo del disco o del perfil | Clave usable directamente | Solo un blob cifrado: hay que forzar la contraseña, a 600.000 iteraciones por intento |
| XSS almacenado en una visita futura | Firma sin que el usuario haga nada | No puede firmar hasta que el usuario escriba la contraseña |
| XSS activo mientras la identidad está desbloqueada | Puede pedir firmas | Igual: puede pedir firmas |
| XSS activo **al tipear la contraseña** | No aplica | Puede capturarla junto con el blob y descifrarlo afuera: **se lleva la identidad** |
| Respaldo filtrado | PEM en claro: identidad robada | Archivo cifrado: hay que forzar la contraseña |
| Olvido | Sin respaldo, identidad perdida | **Sin la contraseña**, identidad perdida (el respaldo está cifrado con ella) |

La fila del XSS al tipear es el precio de cifrar. Por eso la CSP y Trusted Types
obligatorio del nivel 1 forman parte de este diseño, no son un agregado.

### Verificación

**Tests de Python:** 550 en total, 13 de ellos nuevos, en `common/tests/test_backup.py`:
- ida y vuelta: se cifra, se descifra y se firma con la misma identidad;
- contraseña equivocada;
- normalización NFC de la contraseña;
- pubkey cambiada (la AAD lo impide);
- cifrado alterado;
- iteraciones fuera de rango;
- archivos que no son un respaldo;
- contraseña corta;
- **un vector generado por el navegador real**, que Python descifra y cuya pubkey
  coincide. Es la prueba de que Web Crypto y `cryptography` hablan el mismo
  formato.

**En el navegador**, con el stack local (`./run.sh demo`) y Trusted Types obligatorio:

| Prueba | Resultado |
|---|---|
| Identidad `legacy` del nivel 1 | Aparece como "sin contraseña" con su aviso; el header no ofrece bloquear. |
| Crear con contraseñas distintas | "Las dos contraseñas no coinciden."; el botón queda deshabilitado. |
| Crear con contraseña | Pide confirmar el reemplazo. En IndexedDB queda solo `vault` (600.000 iteraciones, ningún `CryptoKey`) y se borra la clave `legacy`. Aparece "Descargá tu respaldo ahora". |
| Recargar | La identidad queda bloqueada: el header muestra "Desbloquear" y el punto de sesión deja de latir. |
| Registrar un minero estando bloqueada | Aparece el pedido de contraseña, con el foco ya en el campo. Una contraseña incorrecta da "La contraseña no es correcta." y el diálogo sigue abierto. Con la correcta, la firma sigue sola y el backend acepta el alta. |
| Bloquear, proponer y **cancelar** | "Tu identidad sigue bloqueada, así que no se firmó nada." No salió ningún `POST /api/laws`. |
| Proponer y desbloquear | Ley enviada (`ley-9d8f355d`). |
| Descargar respaldo (bloqueada) | `voxchain-cifrada-respaldo.json`, `application/json`, solo el registro cifrado y nada en claro. |
| Restaurar el `.json` | Con una contraseña incorrecta: "La contraseña no corresponde a ese respaldo." Con la correcta, misma pubkey y desbloqueada. |
| Restaurar un PEM viejo | La pantalla pide "Contraseña nueva" y su confirmación. Queda cifrada, con la pubkey del PEM y sin clave `legacy`. |
| CLI `propose_law.py --backup` | Con el respaldo del navegador firma, y la firma verifica. Con una contraseña incorrecta: "no se pudo abrir el respaldo". |
| Consola | Sin errores ni violaciones de Trusted Types en ningún flujo. |

---

## 7. Nivel 3: passkeys (WebAuthn)

### Qué es y por qué

Con una passkey, la clave privada **no está en el navegador**. La genera y la
guarda el autenticador del dispositivo (Secure Enclave, TPM, una llave física o el
gestor de passkeys) y no sale nunca de ahí. **Cada firma exige un gesto del
usuario:** huella, cara o PIN. Es lo que los niveles 1 y 2 no podían dar: no hay
nada en la página que un XSS pueda usar ni robar.

En el navegador solo quedan la pubkey y el id de la credencial, que son públicos.
La pubkey se pide en ES256 (P-256) y se obtiene en SPKI con `getPublicKey()`, el
mismo formato que el resto de las identidades. Para el resto del sistema es una
identidad más.

### Cómo se verifica (backend)

WebAuthn no firma el mensaje tal cual: el autenticador firma
`authenticatorData || SHA-256(clientDataJSON)`. El mensaje canónico entra por el
`challenge`, que es su SHA-256.

- **Un solo punto de cambio.** La firma viaja en el mismo campo `signature` (o la
  cabecera `X-Signature`) que las demás, como un sobre
  `wa1.<authData>.<clientDataJSON>.<firma DER>` en base64url.
  `common.identity.verify` lo reconoce por el prefijo y delega en
  `common/identity/webauthn.py`. El API y el NCT no cambiaron: las passkeys valen
  para proponer, mineros, equipos y deliberación sin que ningún endpoint sepa
  que existen.
- **Qué se exige:**
  - `type == "webauthn.get"`;
  - `challenge == base64url(SHA-256(mensaje))`;
  - un `origin` de `WEBAUTHN_ORIGINS`, sin `crossOrigin`;
  - un `rpIdHash` de `WEBAUTHN_RP_IDS`;
  - las banderas **UP** (hubo alguien) y **UV** (se verificó quién);
  - la firma ECDSA válida.
- **Qué no se hace, a propósito:**
  - No hay challenge emitido por el servidor: la frescura y el anti-replay ya los
    dan los mensajes firmados (timestamp o `created_at` + `sig:used`), igual que
    con cualquier otra firma.
  - No se sigue el contador de firmas: las passkeys sincronizadas lo reportan
    siempre en cero, y seguirlo exigiría estado por credencial.
- **Configuración:** `WEBAUTHN_RP_IDS` y `WEBAUTHN_ORIGINS` en el ConfigMap
  (`voxchain.34.39.171.193.sslip.io` y `https://…`) y en compose (`localhost` y
  `http://localhost:4200`).

### Cómo se usa (frontend)

- **Crear:** "Con passkey (recomendado)" viene elegida si el navegador soporta
  WebAuthn en un contexto seguro (HTTPS o localhost). No pide contraseña: el
  navegador muestra su diálogo de passkey. Se pide `residentKey: 'required'` para
  que la passkey sea descubrible en otros dispositivos.
- **Firmar:** `sign()` llama a `navigator.credentials.get` con la credencial de
  esta identidad y `userVerification: 'required'`. Las pantallas no cambiaron.
  Las ceremonias se ponen en fila, porque el navegador rechaza dos a la vez. Si el
  usuario cancela: "No se firmó nada: cancelaste la passkey o se venció el
  tiempo."
- **Otro dispositivo:** WebAuthn no devuelve la pública al firmar, así que en un
  dispositivo nuevo la app no puede saberla. Se restaura **pegando el código
  público**: la app le pide una firma a la passkey (descubrible, sin id) y la
  verifica localmente contra ese código antes de aceptarlo.
- **Borrar:** la identidad se borra de la app, pero la passkey queda en el
  dispositivo (un sitio no puede borrarla). El aviso lo dice.
- **La contraseña sigue disponible** para navegadores sin WebAuthn y para quien
  use el CLI: con una passkey no hay clave que `propose_law.py` pueda usar.

### Qué resuelve y qué no

| Amenaza | Contraseña (nivel 2) | Passkey |
|---|---|---|
| Robo del disco o del perfil | Hay que forzar la contraseña | No hay nada que robar |
| XSS almacenado | No firma hasta que el usuario desbloquee | No firma sin un gesto del usuario |
| XSS activo | Firma mientras esté desbloqueada; puede capturar la contraseña | No puede firmar en silencio ni robar nada. Sí puede **disparar** el diálogo con un mensaje suyo y esperar que el usuario lo confirme, porque el diálogo del navegador no muestra qué se firma |
| Pérdida | Sin la contraseña, identidad perdida | Si la passkey no se sincroniza y se pierde el dispositivo, identidad perdida: no hay respaldo posible |
| Cambio de dominio | No afecta | **Las passkeys dejan de servir**: el autenticador las ata al rpId, y `voxchain.<ip>.sslip.io` cambia con la IP del LoadBalancer |

### Verificación

**Tests de Python:** 565 en total, 15 de ellos nuevos, en `common/tests/test_webauthn.py`.
Arman aserciones como las de un autenticador real (WebAuthn §6.1 y §5.8.1) y
comprueban:
- que una firma válida pasa;
- que falla con otro mensaje (el challenge la ata a este);
- que falla con otra clave;
- que falla con otro rpId (una passkey de otro sitio) y con otro origen;
- que falla con `crossOrigin`;
- que falla sin UP o sin UV;
- que falla una aserción de registro (`webauthn.create`);
- que un sobre malformado no lanza excepciones;
- que las firmas crudas de siempre siguen funcionando.

**En el navegador.** El navegador embebido no tiene un autenticador que se pueda
operar sin la huella del usuario. Por eso las pruebas usaron un autenticador
emulado en la página, que sigue la especificación: firma
`authenticatorData || SHA-256(clientDataJSON)` con una clave P-256 y entrega la
firma en DER. Todo lo demás es real: el frontend, el API y el NCT reconstruidos
con el código nuevo.

| Prueba | Resultado |
|---|---|
| Crear con passkey | Pide ES256, `residentKey: required` y `userVerification: required`. En localStorage quedan solo pubkey, nombre e id de la credencial; en IndexedDB no queda nada. Etiqueta "con passkey"; el header no ofrece bloquear. |
| Registrar un minero | Un pedido a la passkey (con una sola credencial permitida y UV obligatorio); el API responde 200. |
| Proponer y **cancelar** la passkey | "No se firmó nada: cancelaste la passkey o se venció el tiempo." No sale ningún `POST`. |
| Proponer con una aserción sin UV | El API responde **401 Firma inválida**. |
| Proponer con UP y UV | Ley enviada (`ley-e8fa3803`). El NCT la vuelve a verificar al recibirla por RabbitMQ y la encola. |
| Borrar | El aviso explica que la passkey queda en el dispositivo. |
| Restaurar con un código público ajeno | "Esa passkey no corresponde a ese código público."; no se adopta nada. |
| Restaurar con el código correcto | Misma pubkey; se recupera el id de la credencial desde la aserción. |
| Opción "Con contraseña" | Siguen apareciendo sus dos campos y el flujo del nivel 2. |
| Consola | Sin errores ni violaciones de Trusted Types. |

**Queda pendiente:** probar con un autenticador real (Touch ID, Windows Hello o una
llave). Necesita que una persona ponga la huella, y crea una passkey para
`localhost` en su gestor, que después conviene borrar.

---

## 8. El estado después de los tres niveles

### El cuadro de amenazas, actualizado

Es el mismo cuadro de §4, ahora con una columna para cada forma de guardar la
identidad:

| Amenaza | Antes | Con contraseña (nivel 2) | Con passkey (nivel 3) |
|---|---|---|---|
| Un XSS **roba** la clave | ✅ Sí | ⚠️ Parcial. No puede exportarla, pero si está activo **cuando se tipea la contraseña** puede capturarla junto con el blob y descifrarlo afuera. | ✅ Sí. La clave nunca está en el navegador. |
| Un XSS **firma en nombre del usuario** | ❌ No | ⚠️ Parcial. Solo mientras la identidad está desbloqueada; un XSS almacenado en una visita futura no puede hasta que el usuario escriba la contraseña. | ⚠️ Parcial. No puede firmar en silencio: cada firma pide huella o PIN. Sí puede **disparar el diálogo** con un mensaje suyo y esperar que el usuario lo confirme, porque el diálogo no muestra qué se firma. |
| Extensión maliciosa | ❌ No | ⚠️ Igual que un XSS. | ⚠️ Igual que un XSS. |
| Robo del disco, del perfil o malware que lee archivos | ❌ No | ✅ Sí. Solo hay un blob cifrado, y cada intento de adivinar la contraseña cuesta 600.000 iteraciones de PBKDF2. | ✅ Sí. No hay nada que robar. |
| El respaldo | ⚠️ Débil (PEM en claro) | ✅ Archivo cifrado; filtrado, hay que adivinar la contraseña. | No hay respaldo: depende de que el gestor sincronice la passkey. |
| **Pérdida** de la identidad | ❌ Grave | ⚠️ Olvidar la contraseña es perderla. El borrado accidental, el reemplazo sin aviso y la falta de restauración están resueltos (nivel 1). | ⚠️ Perder el dispositivo con una passkey sin sincronizar es perderla. |

Hay dos cambios transversales:
- **Trusted Types** obligatorio baja la probabilidad de un XSS en todas las filas.
- **Las identidades viejas** ("sin contraseña") siguen en la columna "Antes" hasta
  que el usuario las restaure desde su PEM con una contraseña.

### Otras vulnerabilidades de las claves privadas

De más a menos grave:

1. ~~**No hay revocación ni rotación de claves.**~~ La revocación ya está (§9); la
   rotación sigue pendiente. Era así: Si una clave se filtra (un PEM
   viejo pegado en un chat, un respaldo con contraseña débil), la identidad queda
   comprometida **para siempre**. No existe forma de decir "esta pubkey ya no
   vale" ni de pasar los mineros y equipos a una clave nueva. Es la brecha
   estructural más importante.
2. **El dominio `sslip.io` está atado a una IP que puede cambiar de dueño.** Si se
   libera la IP del LoadBalancer y otra persona la obtiene, `voxchain.<ip>.sslip.io`
   pasa a apuntar a su servidor. Esa persona puede sacar un certificado válido y
   servir **el mismo origen**. Entonces puede:
   - leer el IndexedDB de quien entre (blobs cifrados, identidades sin contraseña);
   - mostrar un diálogo de desbloqueo falso para capturar contraseñas;
   - pedir firmas a las passkeys, porque el rpId es legítimo.

   Se resuelve con un dominio propio, que es lo mismo que ya necesitaban las
   passkeys.
3. **Las contraseñas débiles se pueden forzar fuera de línea.** Solo se exigen 10
   caracteres, sin medir fortaleza. PBKDF2 es barato para quien tiene GPUs: un
   respaldo filtrado con una contraseña tipo "voxchain2026" cae rápido. Mejoraría
   con un medidor de fortaleza (zxcvbn), con Argon2id (necesita WASM) o con más
   iteraciones.
4. **Un XSS que dispara la passkey.** WebAuthn no le muestra al usuario qué está
   firmando. Se puede mitigar con una confirmación dentro de la app, aunque un XSS
   también controla esa pantalla. En la práctica depende de que no haya XSS: la
   CSP, Trusted Types y revisar las dependencias npm. Una dependencia comprometida
   es un XSS que la CSP no frena, porque se sirve desde el mismo origen.
5. **La clave de los mineros está en claro.** `WORKER_PRIVKEY_PEM` es un PEM sin
   cifrar: en un `emptyDir` en memoria en Kubernetes y en `/tmp` del contenedor en
   compose. Quien tenga `kubectl exec` o `docker exec` la lee y puede firmar nonces
   como ese nodo. El impacto es acotado, porque no permite firmar como el
   ciudadano, solo atribuirse victorias de ese nodo.
6. **Repetir acciones firmadas** (anti-replay por string, ver "Otros hallazgos").
   No roba la clave, pero reutiliza sus firmas dentro de los 300 s.
7. **Memoria del proceso.** Mientras la identidad está desbloqueada, la clave vive
   en la RAM del navegador; un malware con acceso a la memoria del proceso la puede
   usar. Queda fuera de lo que una web puede defender; con passkey no aplica.

---

## 9. Revocación de identidades

### El problema

Una identidad es una clave. Antes de esto, una clave filtrada (un PEM viejo pegado
en un chat, un respaldo con contraseña débil, un dispositivo robado) quedaba
comprometida **para siempre**: no había forma de decirle a la red "esta pubkey ya
no vale".

### Diseño

- **Certificado de revocación.** Es la firma de
  `revoke|<pubkey>|voxchain-revocation-v1` hecha con la propia clave
  (`revocation_message` en `common/identity/signing.py`). El sufijo fijo separa
  este mensaje de cualquier otro que el usuario firme: una firma de otra cosa
  nunca se puede presentar como revocación.
- **Sin timestamp, a propósito.** Es un certificado al estilo PGP: se genera por
  adelantado y se guarda aparte del respaldo, para presentarlo el día que se pierda
  el control de la clave, cuando ya no se la pueda usar para firmar.
- **Registro.** `POST /api/identity/revoke` lo verifica con el mismo
  `common.identity.verify`, así que sirve con contraseña, con passkey y con
  identidades viejas. Queda en `identity:revoked:<pubkey>`, sin vencimiento. Es
  idempotente y conserva el momento original. `GET /api/identity/revocation/<pubkey>`
  es pública.
- **Es permanente.** Si se pudiera "des-revocar", quien robó la clave también
  podría.
- **Qué rechaza una clave revocada:** todo lo que firma.
  - En el API (`voxchain_api/services/revocation.py`): propuestas, alta y baja de
    mineros, y todas las acciones de `require_signed_action` (equipos, modos,
    deliberación).
  - En el NCT: propuestas y nonces, porque a las colas se puede llegar sin pasar
    por el API.

  El chequeo va antes de verificar la firma y aplica también a propuestas sin
  firma. `common.identity.verify` sigue siendo pura: una firma de una clave
  revocada es matemáticamente válida, lo que cambia es que el sistema no la acepta.

### Qué hay que asumir

- **Quien tenga el certificado puede anular la identidad**, aunque no firmar con
  ella. Por eso va aparte del respaldo.
- **Quien robó la clave también puede revocarla.** Solo destruye una identidad que
  ya estaba comprometida.
- **Lo ya sellado en la cadena no cambia.** Los mineros de una identidad revocada
  siguen registrados y minando con su clave de nodo, pero nadie puede volver a
  administrarlos.
- **No hay rotación.** Revocar no pasa los mineros ni los equipos a una clave
  nueva. Hacerlo con un mensaje firmado por la clave vieja tiene un problema de
  carrera: si la clave se filtró, el atacante puede rotar primero y quedarse con
  todo. La solución conocida es comprometer por adelantado la clave siguiente
  (pre-rotación, como en KERI). Queda pendiente.

### En la pantalla

- **Identidad activa:**
  - "Certificado de revocación" firma el certificado (pide contraseña o passkey
    como cualquier firma) y lo descarga.
  - "Revocar identidad" lo firma y lo presenta en el acto, con una confirmación
    que explica que no se puede deshacer.
  - Una identidad revocada muestra la etiqueta "revocada" y un aviso con la fecha.
    La app consulta el estado cada vez que cambia la identidad, porque una
    identidad restaurada puede estar revocada.
- **Tarjeta "¿Perdiste el control de tu clave?":** permite subir o pegar un
  certificado y revocar **sin tener la identidad en ese navegador**, que es el
  caso para el que existe.

### Verificación

**Tests:** 578 en total, 13 de ellos nuevos.
- `tests/test_identity_revocation.py`:
  - revocar con un certificado válido;
  - nadie revoca a otro;
  - una firma de otra cosa no revoca;
  - revocar es idempotente y conserva el momento;
  - la consulta de una identidad vigente;
  - una clave revocada no propone, no registra ni da de baja mineros, y no pasa
    `require_signed_action`;
  - revocar una identidad no afecta a otra.
- `nct-coordinator/tests/test_signatures.py`: una propuesta de una identidad
  revocada no se encola, y un nonce firmado por una clave revocada no sella.
- `common/tests/test_signing.py`: el mensaje de revocación tiene su propio dominio.

**En vivo**, con el stack local reconstruido:

| Prueba | Resultado |
|---|---|
| "Certificado de revocación" con una identidad con passkey | Pide la passkey y descarga `voxchain-identidad-revocacion.json` con `format`, `v`, `pubkey` y la firma (sobre `wa1.…`). |
| Revocar con ese certificado desde la tarjeta | El API lo acepta; la pantalla muestra "revocada", el aviso con la fecha, y desaparecen los botones de revocar. |
| Dar de baja el minero de esa identidad con una firma válida de su passkey | **401 "Esta identidad fue revocada y ya no puede firmar nada."**; el minero sigue registrado. |
| Identidad nueva con contraseña | Aparece vigente: el aviso de la anterior no se arrastra. |
| "Revocar identidad" en el acto | Pide confirmación, firma y presenta el certificado; queda revocada en el API y en la pantalla. |
| Consola | Sin errores ni violaciones de Trusted Types. |

En esta última tanda el panel del navegador estaba oculto, así que los clics se
dispararon con JavaScript sobre los mismos botones (los handlers de Angular son
los mismos). La baja del minero se probó armando la firma con la passkey emulada
y llamando al API directamente, porque la navegación del router no avanza con el
panel oculto.

---

## 10. Lo que queda pendiente

### Lo que el nivel 2 deja abierto

- **Cambiar la contraseña.** No está implementado. Hoy se puede hacer restaurando
  el PEM, pero con una identidad creada con contraseña no hay PEM. Se haría
  descifrando con `unwrapKey(extractable=true)` solo para volver a cifrarla con
  la contraseña nueva.
- **Bloqueo automático por inactividad.** La clave queda en memoria hasta que el
  usuario toca "Bloquear" o cierra la pestaña. Un temporizador (por ejemplo, 15
  minutos sin interacción) achicaría la ventana de un XSS.
- **Firmas que esperan el desbloqueo.** Las pantallas calculan `timestamp` o
  `created_at` *antes* de llamar a `sign()`. Si el usuario tarda más que la
  ventana de frescura del backend (300 s) en escribir la contraseña, la firma
  sale válida pero vieja, y el backend la rechaza. Pasa solo con un diálogo
  abandonado mucho tiempo; se evitaría calculando la marca de tiempo después de
  desbloquear.
- **Argon2id en vez de PBKDF2.** Resiste mejor a quien fuerza con GPU, pero Web
  Crypto no lo trae: necesita WASM, y eso obliga a agregar `'wasm-unsafe-eval'`
  a la CSP.

### Otros hallazgos de la sesión

- **El anti-replay de las acciones firmadas se puede esquivar.** `sig:used` guarda
  el hash del **string** de la firma, no de lo que autoriza. Hay otros strings que
  también validan para el mismo mensaje: `b64decode` descarta caracteres fuera del
  alfabeto (agregar un "!" da otro string), y ECDSA es maleable (`s → n−s`). Quien
  vea pasar una acción firmada podría repetirla dentro de los 300 s. El arreglo es
  que la marca dependa de `pubkey|mensaje` y no de la firma; quedó como tarea
  aparte.
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

## 11. Archivos tocados

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
`voxchain-frontend/nginx.conf` y `AGENT.md` (commit `da70610`).

**Clave cifrada, nivel 2:**
`voxchain-frontend/src/app/core/services/identity.service.ts`,
`voxchain-frontend/src/app/core/components/unlock-prompt.component.ts` (nuevo),
`voxchain-frontend/src/app/features/identity/identity.component.ts`,
`voxchain-frontend/src/app/app.component.ts`,
`common/identity/backup.py` (nuevo), `common/identity/__init__.py`,
`common/tests/test_backup.py` (nuevo), `scripts/propose_law.py`, `AGENT.md` y
`docs/informe/INFORME.md` (commit `d60a543`).

**Revocación:**
`common/identity/signing.py`, `common/identity/__init__.py`,
`common/storage/redis_store.py`, `voxchain_api/routers/identity.py` (nuevo),
`voxchain_api/services/revocation.py` (nuevo), `voxchain_api/main.py`,
`voxchain_api/routers/laws.py`, `voxchain_api/routers/workers.py`,
`nct-coordinator/nct/coordinator.py`, `tests/test_identity_revocation.py`
(nuevo), `nct-coordinator/tests/test_signatures.py`,
`common/tests/test_signing.py`,
`voxchain-frontend/src/app/core/services/{identity,api}.service.ts`,
`voxchain-frontend/src/app/features/identity/identity.component.ts`, `AGENT.md`
y `docs/informe/INFORME.md`.

**Passkeys, nivel 3:**
`common/identity/webauthn.py` (nuevo), `common/identity/signing.py`,
`common/config.py`, `common/tests/test_webauthn.py` (nuevo),
`docker-compose.yml`, `voxchain-config.yaml`,
`voxchain-frontend/src/app/core/services/identity.service.ts`,
`voxchain-frontend/src/app/features/identity/identity.component.ts`, `AGENT.md`
y `docs/informe/INFORME.md` (commit `91ce8c6`).
