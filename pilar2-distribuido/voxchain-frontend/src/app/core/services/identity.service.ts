import { Injectable, signal } from '@angular/core';

export interface Identity {
  pubkey: string;
  username?: string; // nombre para mostrar
}

/**
 * Cómo está guardada la clave de la identidad en este navegador.
 *
 * - `passkey`: la clave no está en el navegador. Vive en el autenticador del
 *   dispositivo (Secure Enclave, TPM, llave física o gestor de passkeys) y cada
 *   firma pide huella, cara o PIN. En el navegador sólo quedan la pubkey y el id
 *   de la credencial, que son públicos. Es la opción recomendada.
 * - `vault`: cifrada con la contraseña del usuario (el caso normal). En disco
 *   sólo hay un blob cifrado; la clave usable vive en memoria mientras la
 *   identidad esté desbloqueada.
 * - `legacy`: `CryptoKey` no extraíble **sin cifrar**, de antes de que hubiera
 *   contraseña. Funciona, pero cualquiera con el perfil del navegador puede
 *   usarla. No se puede cifrar retroactivamente (`wrapKey` exige una clave
 *   extraíble): se protege restaurándola desde su respaldo con una contraseña.
 */
export type Protection = 'passkey' | 'vault' | 'legacy';

/**
 * El registro cifrado de una identidad. Es lo que se guarda en IndexedDB y,
 * tal cual, el archivo de respaldo: sin la contraseña no sirve para nada, así
 * que se puede descargar cuantas veces se quiera.
 *
 * `scripts/propose_law.py --backup` lo lee (common/identity/backup.py): el
 * formato es un contrato entre las dos puntas y por eso va versionado.
 */
export interface VaultRecord {
  format: 'voxchain-identity';
  v: 1;
  /** SPKI en base64. Va también como dato autenticado (AAD) del cifrado. */
  pubkey: string;
  username?: string;
  kdf: { name: 'PBKDF2'; hash: 'SHA-256'; iterations: number; salt: string };
  cipher: { name: 'AES-GCM'; iv: string };
  /** PKCS#8 de la privada, cifrado con AES-GCM-256. Base64. */
  wrapped: string;
}

/** Lo que devuelve crear una identidad: la identidad y su respaldo cifrado. */
export interface NewIdentity {
  identity: Identity;
  /** JSON del `VaultRecord`, listo para descargar como archivo. */
  backup: string;
}

/** Largo mínimo de la contraseña. Es la única defensa del respaldo si se filtra. */
export const MIN_PASSPHRASE = 10;

/**
 * Iteraciones de PBKDF2-SHA256: lo que recomienda OWASP (2023). En un
 * navegador actual son ~0,5 s por desbloqueo, y es lo que le cuesta a quien
 * robe el respaldo probar cada contraseña.
 */
const PBKDF2_ITERATIONS = 600_000;

/**
 * Límites para aceptar un respaldo ajeno: por debajo sería trivial de
 * forzar, por encima alguien podría colgar la pestaña con un archivo armado.
 */
const MIN_ITERATIONS = 100_000;
const MAX_ITERATIONS = 10_000_000;

const DB_NAME = 'voxchain-identity';
const DB_STORE = 'keys';
const VAULT_KEY = 'vault';
const LEGACY_KEY = 'signing-key';

const ECDSA = { name: 'ECDSA', namedCurve: 'P-256' } as const;

@Injectable({
  providedIn: 'root'
})
export class IdentityService {
  identity = signal<Identity | null>(null);

  /** Cómo está guardada la clave; `null` si no hay (o todavía no se sabe). */
  protection = signal<Protection | null>(null);

  /**
   * Hay identidad cifrada y su clave no está en memoria: para firmar hay que
   * pedir la contraseña. Pasa al abrir la app y al tocar "Bloquear".
   */
  locked = signal(false);

  /** La UI de desbloqueo (UnlockPromptComponent) se muestra mientras sea true. */
  unlockRequested = signal(false);

  /**
   * Si el navegador se comprometió a no desalojar el almacenamiento del sitio.
   *
   * IndexedDB es *best-effort*: bajo presión de espacio el navegador puede
   * borrarlo, y Safari borra lo de un sitio tras 7 días sin visitarlo. `null`
   * mientras no se sabe (o si el navegador no expone la API).
   */
  storagePersisted = signal<boolean | null>(null);

  private storageKey = 'voxchain_identity';

  /**
   * La clave de firma utilizable, **sólo en memoria**. Con `vault`, existe
   * desde que se desbloquea hasta que se bloquea o se cierra la pestaña: un
   * XSS almacenado que corra en una visita futura no puede firmar sin que el
   * usuario escriba su contraseña. Con `legacy`, es la que está en IndexedDB.
   */
  private key: CryptoKey | null = null;
  private vault: VaultRecord | null = null;
  /** Id (base64url) de la credencial WebAuthn de una identidad `passkey`. */
  private credentialId: string | null = null;
  /** Una sola ceremonia WebAuthn a la vez: el navegador rechaza la segunda. */
  private passkeyQueue: Promise<unknown> = Promise.resolve();

  /** Lectura inicial de IndexedDB: todo lo que dependa de la clave la espera. */
  private ready: Promise<void>;

  private pendingUnlock: Promise<void> | null = null;
  private unlockWaiter: { resolve: () => void; reject: (e: Error) => void } | null = null;

  constructor() {
    this.ready = this.load();
  }

  private async load(): Promise<void> {
    const raw = localStorage.getItem(this.storageKey);
    if (!raw) return;
    let stored: any;
    try {
      stored = JSON.parse(raw);
    } catch {
      localStorage.removeItem(this.storageKey);
      return;
    }

    // La identidad se publica ANTES de tocar la clave: la migración de abajo
    // reescribe el localStorage a partir de este signal, así que si se hiciera
    // al revés escribiría un `null` y borraría la identidad que está migrando.
    this.identity.set({
      pubkey: stored.pubkey,
      ...(stored.username ? { username: stored.username } : {}),
    });

    // Passkey: no hay nada que leer de IndexedDB, la clave no está acá.
    if (stored.passkey?.credentialId) {
      this.credentialId = stored.passkey.credentialId;
      this.protection.set('passkey');
      return;
    }

    // Sólo consulta: pedir persistencia en el arranque, sin que el usuario haya
    // hecho nada, en Firefox abre un permiso que nadie entiende. Se pide al
    // crear o restaurar (ver requestPersistence).
    navigator.storage?.persisted?.()
      .then((ok) => this.storagePersisted.set(ok))
      .catch(() => {});

    // Migración de identidades muy viejas: la privada estaba exportada en
    // localStorage, en texto plano. Se reimporta como clave no extraíble y se
    // borra el original. Queda `legacy`: cifrarla pediría una contraseña en el
    // arranque, antes de que el usuario sepa de qué se trata.
    if (stored.exportedPrivkey) {
      try {
        this.key = await importSigningKey(base64ToArrayBuffer(stored.exportedPrivkey));
        await dbPut(LEGACY_KEY, this.key);
        // Recién acá se borra el original: si el guardado falla, la identidad
        // sigue siendo recuperable del localStorage en el próximo arranque.
        this.persistPublicPart();
        this.protection.set('legacy');
      } catch (err) {
        console.error('no se pudo migrar la clave a IndexedDB', err);
      }
      return;
    }

    const vault = await dbGet<VaultRecord>(VAULT_KEY);
    if (vault) {
      this.vault = vault;
      this.protection.set('vault');
      this.locked.set(true);
      return;
    }
    const legacy = await dbGet<CryptoKey>(LEGACY_KEY);
    if (legacy) {
      this.key = legacy;
      this.protection.set('legacy');
    }
  }

  /**
   * Pide que el navegador no desaloje IndexedDB. Chrome decide solo (sitio
   * instalado, en marcadores o con uso frecuente); Firefox le pregunta al
   * usuario, por eso se llama justo después de una acción suya.
   */
  private async requestPersistence(): Promise<void> {
    try {
      const ok = (await navigator.storage?.persist?.()) ?? null;
      this.storagePersisted.set(ok);
    } catch {
      this.storagePersisted.set(null);
    }
  }

  /**
   * Guarda en localStorage sólo la parte pública de la identidad (con passkey,
   * también el id de la credencial: tampoco es secreto).
   */
  private persistPublicPart() {
    const id = this.identity();
    if (!id) return;
    const passkey = this.credentialId ? { passkey: { credentialId: this.credentialId } } : {};
    localStorage.setItem(this.storageKey, JSON.stringify({ ...id, ...passkey }));
  }

  /**
   * Si este navegador puede usar passkeys acá. Exige contexto seguro (HTTPS o
   * localhost): en `http://<ip>` no hay WebAuthn.
   */
  passkeysSupported(): boolean {
    return window.isSecureContext && typeof PublicKeyCredential !== 'undefined'
      && !!navigator.credentials?.create;
  }

  /**
   * Crea una identidad cuya clave vive en una passkey.
   *
   * El autenticador genera el par y nunca entrega la privada; lo que vuelve es
   * la pública en SPKI (`getPublicKey()`), el mismo formato que las demás
   * identidades, así que para el resto del sistema es una identidad más. Se
   * pide ES256 (P-256) porque es lo único que verifica el backend.
   *
   * `residentKey: 'required'` hace la passkey "descubrible": en otro dispositivo
   * se puede encontrar sin saber su id, que es lo que permite restaurar con
   * sólo el código público.
   */
  async createPasskey(displayName: string | undefined,
                      opts: { replace?: boolean } = {}): Promise<Identity> {
    await this.ready;
    this.assertCanReplace(opts.replace);
    const name = displayName?.trim() || 'VoxChain';

    let cred: PublicKeyCredential | null;
    try {
      cred = await navigator.credentials.create({
        publicKey: {
          rp: { name: 'VoxChain' },
          user: { id: crypto.getRandomValues(new Uint8Array(16)), name, displayName: name },
          challenge: crypto.getRandomValues(new Uint8Array(32)),
          pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
          authenticatorSelection: { residentKey: 'required', userVerification: 'required' },
          attestation: 'none',
          timeout: 120_000,
        },
      }) as PublicKeyCredential | null;
    } catch (err) {
      throw passkeyError(err, 'No se creó la passkey');
    }
    const response = cred?.response as AuthenticatorAttestationResponse | undefined;
    const spki = response?.getPublicKey?.();
    if (!cred || !spki || response?.getPublicKeyAlgorithm?.() !== -7) {
      throw new Error('Este navegador no pudo crear una passkey compatible. Probá con la contraseña.');
    }
    // Que sea de verdad una pública P-256 antes de adoptarla como identidad.
    await crypto.subtle.importKey('spki', spki, ECDSA, false, ['verify']);
    return this.adoptPasskey(arrayBufferToBase64(spki), b64url(cred.rawId), displayName?.trim());
  }

  /**
   * Restaura una identidad con passkey a partir de su código público.
   *
   * WebAuthn no devuelve la pública al firmar, así que en un dispositivo nuevo
   * el navegador no tiene forma de saberla: la aporta el usuario (es pública).
   * Para no creerle a ciegas, se le pide una firma a la passkey y se verifica
   * contra ese código: si no coincide, no es su identidad.
   */
  async restorePasskey(pubkey: string, displayName?: string,
                       opts: { replace?: boolean } = {}): Promise<Identity> {
    await this.ready;
    this.assertCanReplace(opts.replace);
    let pub: CryptoKey;
    try {
      pub = await crypto.subtle.importKey(
        'spki', base64ToArrayBuffer(pubkey.trim()), ECDSA, false, ['verify']);
    } catch {
      throw new Error('Ese no es un código público de VoxChain.');
    }

    const challenge = crypto.getRandomValues(new Uint8Array(32));
    let cred: PublicKeyCredential | null;
    try {
      cred = await navigator.credentials.get({
        publicKey: { challenge, userVerification: 'required', timeout: 120_000 },
      }) as PublicKeyCredential | null;
    } catch (err) {
      throw passkeyError(err, 'No se usó ninguna passkey');
    }
    const r = cred?.response as AuthenticatorAssertionResponse | undefined;
    if (!cred || !r) throw new Error('No se usó ninguna passkey.');

    const client = JSON.parse(new TextDecoder().decode(r.clientDataJSON));
    const signed = concat(r.authenticatorData,
                          await crypto.subtle.digest('SHA-256', r.clientDataJSON));
    const ok = client.type === 'webauthn.get'
      && client.challenge === b64url(challenge.buffer)
      && await crypto.subtle.verify({ name: 'ECDSA', hash: 'SHA-256' }, pub,
                                    derToP1363(r.signature), signed);
    if (!ok) throw new Error('Esa passkey no corresponde a ese código público.');

    return this.adoptPasskey(pubkey.trim(), b64url(cred.rawId), displayName?.trim());
  }

  private async adoptPasskey(pubkey: string, credentialId: string,
                             name: string | undefined): Promise<Identity> {
    // Con passkey no queda ninguna clave en el navegador: se borra lo que
    // hubiera de una identidad anterior (cifrada o no).
    await Promise.all([dbDelete(VAULT_KEY), dbDelete(LEGACY_KEY)]);
    this.vault = null;
    this.key = null;
    this.credentialId = credentialId;
    this.protection.set('passkey');
    this.locked.set(false);
    this.settleUnlock();

    const identity: Identity = { pubkey, ...(name ? { username: name } : {}) };
    this.identity.set(identity);
    this.persistPublicPart();
    return identity;
  }

  /**
   * Firma con la passkey: el navegador muestra su diálogo, el usuario pone la
   * huella o el PIN, y el autenticador firma. El challenge es el SHA-256 del
   * mensaje, así que la firma queda atada a este mensaje y a ningún otro.
   * Devuelve el sobre `wa1.…` que entiende `common/identity/webauthn.py`.
   */
  private signWithPasskey(message: string): Promise<string> {
    const credentialId = this.credentialId!;
    const run = async () => {
      const challenge = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(message));
      let cred: PublicKeyCredential | null;
      try {
        cred = await navigator.credentials.get({
          publicKey: {
            challenge,
            allowCredentials: [{ type: 'public-key', id: b64urlToArrayBuffer(credentialId) }],
            userVerification: 'required',
            timeout: 120_000,
          },
        }) as PublicKeyCredential | null;
      } catch (err) {
        throw passkeyError(err, 'No se firmó nada');
      }
      const r = cred?.response as AuthenticatorAssertionResponse | undefined;
      if (!r) throw new Error('No se firmó nada: la passkey no respondió.');
      return 'wa1.' + [r.authenticatorData, r.clientDataJSON, r.signature].map(b64url).join('.');
    };
    const result = this.passkeyQueue.then(run, run);
    this.passkeyQueue = result.catch(() => {});
    return result;
  }

  /**
   * Genera una identidad propia, cifrada con `passphrase`.
   *
   * La privada nace extraíble, porque `wrapKey` sólo cifra claves extraíbles,
   * y lo único que se hace con ella es cifrarla: el PKCS#8 lo produce el
   * propio `wrapKey` dentro de Web Crypto y sale ya cifrado, así que sus bytes
   * en claro nunca existen en JavaScript. La clave con la que se firma después
   * es una copia **no extraíble** que se obtiene descifrando ese mismo blob.
   *
   * `displayName` es un nombre para mostrar y nada más: vive sólo en este
   * navegador (y en el respaldo) y NO viaja al backend. Quien identifica a una
   * cuenta ante el resto de la red es su clave pública.
   */
  async generateKeypair(displayName: string | undefined, passphrase: string,
                        opts: { replace?: boolean } = {}): Promise<NewIdentity> {
    await this.ready;
    this.assertCanReplace(opts.replace);
    assertPassphrase(passphrase);

    const keypair = await crypto.subtle.generateKey(ECDSA, true, ['sign', 'verify']);
    const pubkey = arrayBufferToBase64(await crypto.subtle.exportKey('spki', keypair.publicKey));
    const name = displayName?.trim() || undefined;

    const { record, kek } = await seal(keypair.privateKey, pubkey, passphrase, name);
    const key = await unwrapWith(record, kek);
    const identity = await this.adoptVault(record, key, name);

    return { identity, backup: JSON.stringify(record, null, 2) };
  }

  /**
   * Restaura una identidad desde un respaldo.
   *
   * Acepta los dos formatos que existieron:
   * - **Respaldo cifrado** (JSON, el actual): `passphrase` es la contraseña con
   *   que se cifró. Se verifica descifrándolo y se guarda tal cual.
   * - **PEM en claro** (el de antes): `passphrase` es una contraseña **nueva**
   *   con la que queda cifrada desde ahora. Es también la forma de proteger una
   *   identidad `legacy`.
   *
   * En el PEM la pública se deriva de la privada (vía JWK, que trae `x`/`y`):
   * no hay que pedirla aparte ni confiar en que el usuario pegue la correcta.
   */
  async restore(text: string, passphrase: string, displayName?: string,
                opts: { replace?: boolean } = {}): Promise<Identity> {
    await this.ready;
    this.assertCanReplace(opts.replace);
    const trimmed = text.trim();
    const name = displayName?.trim() || undefined;

    if (trimmed.startsWith('{')) {
      const record = parseVault(trimmed);
      let key: CryptoKey;
      try {
        key = await openVault(record, passphrase);
      } catch {
        throw new Error('La contraseña no corresponde a ese respaldo.');
      }
      return this.adoptVault(record, key, name ?? record.username);
    }

    assertPassphrase(passphrase);
    const pkcs8 = pemToArrayBuffer(trimmed);
    let extractable: CryptoKey;
    let jwk: JsonWebKey;
    try {
      extractable = await crypto.subtle.importKey('pkcs8', pkcs8, ECDSA, true, ['sign']);
      jwk = await crypto.subtle.exportKey('jwk', extractable);
    } catch {
      throw new Error('Eso no parece un respaldo de VoxChain. Revisá que lo hayas pegado completo.');
    }
    const pub = await crypto.subtle.importKey(
      'jwk', { kty: 'EC', crv: 'P-256', x: jwk.x, y: jwk.y, ext: true }, ECDSA, true, ['verify']);
    const pubkey = arrayBufferToBase64(await crypto.subtle.exportKey('spki', pub));

    const { record, kek } = await seal(extractable, pubkey, passphrase, name);
    return this.adoptVault(record, await unwrapWith(record, kek), name);
  }

  /** Guarda el registro cifrado y deja la identidad desbloqueada. */
  private async adoptVault(record: VaultRecord, key: CryptoKey,
                           name: string | undefined): Promise<Identity> {
    if (name) record.username = name; else delete record.username;
    await dbPut(VAULT_KEY, record);
    // Recién con el registro nuevo a salvo se borra la clave sin cifrar: si el
    // guardado fallara, la identidad anterior seguiría en pie.
    await dbDelete(LEGACY_KEY);

    this.vault = record;
    this.key = key;
    this.credentialId = null;
    this.protection.set('vault');
    this.locked.set(false);

    const identity: Identity = { pubkey: record.pubkey, ...(name ? { username: name } : {}) };
    this.identity.set(identity);
    this.persistPublicPart();
    this.settleUnlock();
    await this.requestPersistence();
    return identity;
  }

  /**
   * Crear o restaurar pisa la identidad actual, y sin su respaldo no vuelve.
   * La UI pregunta antes; esto es la red de seguridad para cualquier otro
   * llamador.
   */
  private assertCanReplace(replace?: boolean) {
    if (this.identity() && !replace) {
      throw new Error('Ya hay una identidad en este navegador: reemplazarla la borra.');
    }
  }

  /**
   * Descifra la clave con la contraseña y la deja en memoria.
   *
   * Una contraseña equivocada no descifra nada: AES-GCM detecta que la clave
   * derivada no es la correcta y `unwrapKey` falla. No hace falta guardar ningún
   * hash de la contraseña para comprobarla.
   */
  async unlock(passphrase: string): Promise<void> {
    await this.ready;
    if (this.protection() !== 'vault' || !this.vault) return;
    try {
      this.key = await openVault(this.vault, passphrase);
    } catch {
      throw new Error('La contraseña no es correcta.');
    }
    this.locked.set(false);
    this.settleUnlock();
  }

  /** Olvida la clave de memoria. Hasta el próximo desbloqueo nadie puede firmar. */
  lock() {
    if (this.protection() !== 'vault') return;
    this.key = null;
    this.locked.set(true);
  }

  /**
   * Muestra el pedido de contraseña y espera a que el usuario desbloquee.
   * Llamadas concurrentes comparten el mismo pedido.
   */
  requestUnlock(): Promise<void> {
    if (!this.pendingUnlock) {
      this.pendingUnlock = new Promise<void>((resolve, reject) => {
        this.unlockWaiter = { resolve, reject };
      });
      this.unlockRequested.set(true);
    }
    return this.pendingUnlock;
  }

  /** El usuario cerró el pedido de contraseña sin desbloquear. */
  cancelUnlock() {
    this.settleUnlock(new Error('Tu identidad sigue bloqueada, así que no se firmó nada.'));
  }

  private settleUnlock(err?: Error) {
    const waiter = this.unlockWaiter;
    this.pendingUnlock = null;
    this.unlockWaiter = null;
    this.unlockRequested.set(false);
    if (!waiter) return;
    if (err) waiter.reject(err); else waiter.resolve();
  }

  /** El respaldo cifrado de la identidad actual, o `null` si no está cifrada. */
  backupJson(): string | null {
    return this.vault ? JSON.stringify(this.vault, null, 2) : null;
  }

  /**
   * Firma un mensaje con la clave de la identidad (ECDSA P-256 / SHA-256).
   *
   * Si la identidad está bloqueada, pide la contraseña y firma cuando el
   * usuario la escribe: así ninguna pantalla que firma tiene que saber de
   * bloqueos. Devuelve la firma cruda (P1363, r||s) en base64, el formato que
   * verifica el backend.
   */
  async sign(message: string): Promise<string> {
    await this.ready;
    if (!this.identity()) throw new Error('no identity');

    if (this.protection() === 'passkey') return this.signWithPasskey(message);
    if (!this.key && this.protection() === 'vault') await this.requestUnlock();
    const key = this.key;
    if (!key) {
      throw new Error(
        'No hay clave de firma en este navegador. Si borraste los datos del ' +
        'sitio, la identidad se perdió con ellos: restaurala desde tu respaldo.'
      );
    }

    const sig = await crypto.subtle.sign(
      { name: 'ECDSA', hash: 'SHA-256' },
      key,
      new TextEncoder().encode(message)
    );
    return arrayBufferToBase64(sig);
  }

  /** SHA-256 del texto en hex (debe coincidir con hashlib.sha256 del backend). */
  async sha256Hex(text: string): Promise<string> {
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
    return Array.from(new Uint8Array(digest))
      .map((b) => b.toString(16).padStart(2, '0'))
      .join('');
  }

  /**
   * Borra la identidad de este navegador, **clave incluida**.
   *
   * No es "cerrar sesión" (para eso está `lock`). Es irreversible salvo que el
   * usuario tenga el respaldo, así que sólo se llama tras una confirmación.
   */
  clearIdentity() {
    localStorage.removeItem(this.storageKey);
    this.identity.set(null);
    this.key = null;
    this.vault = null;
    this.credentialId = null;
    this.protection.set(null);
    this.locked.set(false);
    this.settleUnlock(new Error('La identidad se borró.'));
    Promise.all([dbDelete(VAULT_KEY), dbDelete(LEGACY_KEY)])
      .catch((err) => console.error('no se pudo borrar la clave', err));
  }

  getPubkeyShort(): string {
    const id = this.identity();
    if (!id) return '';
    return id.pubkey.slice(0, 16) + '...';
  }

  getUsername(): string | undefined {
    const id = this.identity();
    return id?.username;
  }
}

// --- Cifrado de la clave -----------------------------------------------------
//
// PBKDF2-SHA256 deriva de la contraseña una clave AES-GCM-256 (la "KEK"), que
// cifra el PKCS#8 de la privada con wrapKey. La pubkey va como dato autenticado:
// un registro al que le cambien la pubkey —para hacer pasar una identidad por
// otra— no se descifra. La KEK tampoco es extraíble y no se guarda en ningún
// lado: se vuelve a derivar en cada desbloqueo.

function assertPassphrase(passphrase: string) {
  if ([...passphrase].length < MIN_PASSPHRASE) {
    throw new Error(`La contraseña tiene que tener al menos ${MIN_PASSPHRASE} caracteres.`);
  }
}

async function deriveKek(passphrase: string, salt: Uint8Array,
                         iterations: number): Promise<CryptoKey> {
  // NFC: la misma contraseña tipeada en otro sistema (o leída por el CLI en
  // Python) puede llegar con los acentos compuestos de otra forma.
  const base = await crypto.subtle.importKey(
    'raw', new TextEncoder().encode(passphrase.normalize('NFC')), 'PBKDF2', false, ['deriveKey']);
  return crypto.subtle.deriveKey(
    { name: 'PBKDF2', hash: 'SHA-256', salt, iterations },
    base, { name: 'AES-GCM', length: 256 }, false, ['wrapKey', 'unwrapKey']);
}

async function seal(privateKey: CryptoKey, pubkey: string, passphrase: string,
                    username?: string): Promise<{ record: VaultRecord; kek: CryptoKey }> {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const kek = await deriveKek(passphrase, salt, PBKDF2_ITERATIONS);
  const wrapped = await crypto.subtle.wrapKey(
    'pkcs8', privateKey, kek,
    { name: 'AES-GCM', iv, additionalData: new TextEncoder().encode(pubkey) });
  const record: VaultRecord = {
    format: 'voxchain-identity',
    v: 1,
    pubkey,
    ...(username ? { username } : {}),
    kdf: { name: 'PBKDF2', hash: 'SHA-256', iterations: PBKDF2_ITERATIONS,
           salt: arrayBufferToBase64(salt.buffer) },
    cipher: { name: 'AES-GCM', iv: arrayBufferToBase64(iv.buffer) },
    wrapped: arrayBufferToBase64(wrapped),
  };
  return { record, kek };
}

/** Descifra el registro con una KEK ya derivada, como clave **no extraíble**. */
function unwrapWith(record: VaultRecord, kek: CryptoKey): Promise<CryptoKey> {
  return crypto.subtle.unwrapKey(
    'pkcs8', base64ToArrayBuffer(record.wrapped), kek,
    { name: 'AES-GCM', iv: new Uint8Array(base64ToArrayBuffer(record.cipher.iv)),
      additionalData: new TextEncoder().encode(record.pubkey) },
    ECDSA, false, ['sign']);
}

async function openVault(record: VaultRecord, passphrase: string): Promise<CryptoKey> {
  const kek = await deriveKek(
    passphrase, new Uint8Array(base64ToArrayBuffer(record.kdf.salt)), record.kdf.iterations);
  return unwrapWith(record, kek);
}

/** Valida un respaldo pegado o subido antes de gastar medio segundo en PBKDF2. */
function parseVault(text: string): VaultRecord {
  const invalid = new Error('Ese archivo no es un respaldo de VoxChain.');
  let r: any;
  try {
    r = JSON.parse(text);
  } catch {
    throw invalid;
  }
  const b64 = (s: unknown) => typeof s === 'string' && /^[A-Za-z0-9+/]+=*$/.test(s);
  const ok = r && r.format === 'voxchain-identity' && r.v === 1
    && b64(r.pubkey) && b64(r.wrapped)
    && r.kdf?.name === 'PBKDF2' && r.kdf?.hash === 'SHA-256' && b64(r.kdf?.salt)
    && Number.isInteger(r.kdf?.iterations)
    && r.cipher?.name === 'AES-GCM' && b64(r.cipher?.iv)
    && (r.username === undefined || typeof r.username === 'string');
  if (!ok) throw invalid;
  if (r.kdf.iterations < MIN_ITERATIONS || r.kdf.iterations > MAX_ITERATIONS) throw invalid;
  return {
    format: r.format, v: r.v, pubkey: r.pubkey,
    ...(r.username ? { username: r.username.slice(0, 24) } : {}),
    kdf: { name: 'PBKDF2', hash: 'SHA-256', iterations: r.kdf.iterations, salt: r.kdf.salt },
    cipher: { name: 'AES-GCM', iv: r.cipher.iv },
    wrapped: r.wrapped,
  };
}

// --- Passkeys ----------------------------------------------------------------

/**
 * Traduce los errores de WebAuthn a algo que el usuario entienda. El caso
 * común es `NotAllowedError`: canceló el diálogo o se venció el tiempo.
 */
function passkeyError(err: unknown, prefix: string): Error {
  const name = (err as any)?.name;
  if (name === 'NotAllowedError' || name === 'AbortError') {
    return new Error(`${prefix}: cancelaste la passkey o se venció el tiempo.`);
  }
  if (name === 'InvalidStateError') {
    return new Error(`${prefix}: esa passkey ya está registrada en este dispositivo.`);
  }
  if (name === 'SecurityError') {
    return new Error(`${prefix}: las passkeys sólo funcionan en una conexión segura (HTTPS).`);
  }
  console.error(err);
  return new Error(`${prefix}: la passkey no respondió.`);
}

/**
 * Firma ECDSA de DER (como la entrega un autenticador) a P1363 (r||s, como la
 * espera `crypto.subtle.verify`). Sólo se usa para comprobar localmente, al
 * restaurar, que una passkey corresponde a un código público.
 */
function derToP1363(der: ArrayBuffer): ArrayBuffer {
  const b = new Uint8Array(der);
  let i = 2;                                   // 0x30 len
  if (b[1] & 0x80) i += b[1] & 0x7f;           // largo en forma larga
  const read = () => {
    const len = b[i + 1];
    let v = b.slice(i + 2, i + 2 + len);
    i += 2 + len;
    while (v.length > 32 && v[0] === 0) v = v.slice(1);
    const out = new Uint8Array(32);
    out.set(v, 32 - v.length);
    return out;
  };
  const r = read();
  const s = read();
  return concat(r.buffer, s.buffer);
}

function concat(a: ArrayBuffer, b: ArrayBuffer): ArrayBuffer {
  const out = new Uint8Array(a.byteLength + b.byteLength);
  out.set(new Uint8Array(a), 0);
  out.set(new Uint8Array(b), a.byteLength);
  return out.buffer;
}

function b64url(buffer: ArrayBuffer | ArrayBufferLike): string {
  return arrayBufferToBase64(buffer).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function b64urlToArrayBuffer(text: string): ArrayBuffer {
  const b64 = text.replace(/-/g, '+').replace(/_/g, '/');
  return base64ToArrayBuffer(b64 + '='.repeat((4 - (b64.length % 4)) % 4));
}

// --- IndexedDB ---------------------------------------------------------------
//
// Se guardan objetos, no strings: el clonado estructurado soporta tanto el
// registro cifrado como un `CryptoKey` (las identidades `legacy`), así que el
// material de una clave no extraíble nunca existe como dato en la página.

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(DB_STORE)) db.createObjectStore(DB_STORE);
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function withStore<T>(mode: IDBTransactionMode,
                      fn: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  return openDb().then((db) => new Promise<T>((resolve, reject) => {
    const tx = db.transaction(DB_STORE, mode);
    const req = fn(tx.objectStore(DB_STORE));
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
    tx.oncomplete = () => db.close();
  }));
}

function dbGet<T>(key: string): Promise<T | null> {
  return withStore<T | undefined>('readonly', (store) => store.get(key))
    .then((v) => v ?? null)
    .catch((err) => {
      console.error('no se pudo leer IndexedDB', err);
      return null;
    });
}

function dbPut(key: string, value: unknown): Promise<unknown> {
  return withStore('readwrite', (store) => store.put(value, key));
}

function dbDelete(key: string): Promise<unknown> {
  return withStore('readwrite', (store) => store.delete(key));
}

/** Importa una privada PKCS#8 como clave de firma **no extraíble**. */
function importSigningKey(pkcs8: ArrayBuffer): Promise<CryptoKey> {
  return crypto.subtle.importKey('pkcs8', pkcs8, ECDSA, false, ['sign']);
}

// --- helpers -----------------------------------------------------------------

function base64ToArrayBuffer(b64: string): ArrayBuffer {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

function arrayBufferToBase64(buffer: ArrayBuffer | ArrayBufferLike): string {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

/** PEM PKCS#8 → bytes. Tolera espacios, saltos de línea y CRLF al pegar. */
function pemToArrayBuffer(pem: string): ArrayBuffer {
  const b64 = pem
    .replace(/-----BEGIN PRIVATE KEY-----/, '')
    .replace(/-----END PRIVATE KEY-----/, '')
    .replace(/\s+/g, '');
  if (!b64 || !/^[A-Za-z0-9+/]+=*$/.test(b64)) {
    throw new Error('Pegá el respaldo completo: el archivo que descargaste, o la clave con las líneas BEGIN y END.');
  }
  return base64ToArrayBuffer(b64);
}
