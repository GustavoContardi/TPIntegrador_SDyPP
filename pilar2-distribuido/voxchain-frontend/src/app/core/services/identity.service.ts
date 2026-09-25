import { Injectable, signal } from '@angular/core';

export interface Identity {
  pubkey: string;
  username?: string; // nombre para mostrar
}

/** Lo que devuelve crear una identidad: la identidad y su copia de respaldo. */
export interface NewIdentity {
  identity: Identity;
  /**
   * PEM de la clave privada, **la única vez** que existe en toda la vida de la
   * identidad. No se persiste en ningún lado: si el usuario no lo guarda ahora,
   * no hay forma de recuperarlo. Es el precio de que la clave no sea extraíble.
   */
  pemBackup: string;
}

const DB_NAME = 'voxchain-identity';
const DB_STORE = 'keys';
const DB_KEY = 'signing-key';

@Injectable({
  providedIn: 'root'
})
export class IdentityService {
  identity = signal<Identity | null>(null);

  private storageKey = 'voxchain_identity';

  /**
   * La clave de firma, que vive en IndexedDB y nunca en JavaScript.
   *
   * Es una promesa porque `sign()` puede llamarse antes de que termine la
   * lectura inicial; esperar acá evita que la primera firma de la sesión falle
   * por una carrera con el arranque.
   */
  private signingKey: Promise<CryptoKey | null> = Promise.resolve(null);

  /**
   * Si el navegador se comprometió a no desalojar el almacenamiento del sitio.
   *
   * IndexedDB es *best-effort*: bajo presión de espacio el navegador puede
   * borrarlo, y Safari borra lo de un sitio tras 7 días sin visitarlo. Para
   * una clave que no se puede volver a leer, eso es perder la identidad. `null`
   * mientras no se sabe (o si el navegador no expone la API).
   */
  storagePersisted = signal<boolean | null>(null);

  constructor() {
    this.loadFromStorage();
  }

  private loadFromStorage() {
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

    // Migración de identidades viejas: hasta ahora la privada se guardaba
    // exportada en localStorage, en texto plano. Cualquier XSS podía leerla y
    // quedarse con la identidad para siempre. Al abrir la app se reimporta como
    // clave NO extraíble, se pasa a IndexedDB y se borra el original.
    this.signingKey = stored.exportedPrivkey
      ? this.migrateLegacyKey(stored.exportedPrivkey)
      : loadKey();
    // Sólo consulta: pedir persistencia en el arranque, sin que el usuario haya
    // hecho nada, en Firefox abre un permiso que nadie entiende. Se pide al
    // crear o restaurar (ver requestPersistence).
    navigator.storage?.persisted?.()
      .then((ok) => this.storagePersisted.set(ok))
      .catch(() => {});
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

  private async migrateLegacyKey(exportedPrivkey: string): Promise<CryptoKey | null> {
    try {
      const key = await importSigningKey(base64ToArrayBuffer(exportedPrivkey));
      await saveKey(key);
      // Recién acá se borra el original: si el guardado falla, la identidad
      // sigue siendo recuperable del localStorage en el próximo arranque.
      this.persistPublicPart();
      return key;
    } catch (err) {
      console.error('no se pudo migrar la clave a IndexedDB', err);
      return null;
    }
  }

  /** Guarda en localStorage sólo la parte pública de la identidad. */
  private persistPublicPart() {
    const id = this.identity();
    if (id) localStorage.setItem(this.storageKey, JSON.stringify(id));
  }

  /**
   * Genera una identidad propia en el navegador.
   *
   * La clave privada se guarda en IndexedDB como `CryptoKey` **no extraíble**:
   * el navegador la usa para firmar pero su material nunca vuelve a ser
   * representable en JavaScript. Un XSS pasa de poder robar la identidad para
   * siempre a poder pedirle firmas mientras la pestaña esté abierta — sigue
   * siendo malo, pero es reversible cerrando la pestaña y no sobrevive a nada.
   *
   * `displayName` es un nombre para mostrar y nada más: vive sólo en este
   * navegador y NO viaja al backend. Lo único que el sistema puede verificar es
   * la posesión de la clave privada, así que un nombre enviado al servidor sería
   * un dato no verificable dentro del protocolo. Quien identifica a una cuenta
   * ante el resto de la red es su clave pública.
   */
  async generateKeypair(displayName?: string,
                        opts: { replace?: boolean } = {}): Promise<NewIdentity> {
    this.assertCanReplace(opts.replace);
    // Se genera extraíble por un instante y con un único propósito: sacar el
    // PEM de respaldo que el usuario tiene que guardar. Después se reimporta
    // como no extraíble y el handle extraíble se descarta.
    const keypair = await crypto.subtle.generateKey(
      { name: 'ECDSA', namedCurve: 'P-256' },
      true,
      ['sign', 'verify']
    );

    const pubkeyRaw = await crypto.subtle.exportKey('spki', keypair.publicKey);
    const privkeyRaw = await crypto.subtle.exportKey('pkcs8', keypair.privateKey);

    const sealed = await importSigningKey(privkeyRaw);
    await saveKey(sealed);
    this.signingKey = Promise.resolve(sealed);

    const name = displayName?.trim();
    const identity: Identity = {
      pubkey: arrayBufferToBase64(pubkeyRaw),
      ...(name ? { username: name } : {}),
    };
    this.identity.set(identity);
    this.persistPublicPart();
    await this.requestPersistence();

    return { identity, pemBackup: formatAsPem(arrayBufferToBase64(privkeyRaw)) };
  }

  /**
   * Restaura una identidad desde el PEM de respaldo que se mostró al crearla.
   *
   * Es lo que le da sentido a ese respaldo: sin esto sólo servía para
   * `scripts/propose_law.py`, y quien borraba los datos del sitio perdía la
   * identidad aunque la hubiera guardado.
   *
   * La pública se deriva de la privada (vía JWK, que trae `x`/`y`): así no hay
   * que pedirla aparte ni confiar en que el usuario pegue la que corresponde.
   * Para eso la privada se importa extraíble un instante; no expone nada nuevo,
   * porque el PEM ya está en JavaScript desde que el usuario lo pegó. Lo que se
   * guarda es, como siempre, una reimportación no extraíble.
   */
  async restoreFromPem(pem: string, displayName?: string,
                       opts: { replace?: boolean } = {}): Promise<Identity> {
    this.assertCanReplace(opts.replace);

    const pkcs8 = pemToArrayBuffer(pem);
    let jwk: JsonWebKey;
    try {
      const temp = await crypto.subtle.importKey(
        'pkcs8', pkcs8, { name: 'ECDSA', namedCurve: 'P-256' }, true, ['sign']);
      jwk = await crypto.subtle.exportKey('jwk', temp);
    } catch {
      throw new Error('Eso no parece una clave secreta de VoxChain. Revisá que la hayas pegado completa.');
    }
    const pub = await crypto.subtle.importKey(
      'jwk', { kty: 'EC', crv: 'P-256', x: jwk.x, y: jwk.y, ext: true },
      { name: 'ECDSA', namedCurve: 'P-256' }, true, ['verify']);
    const pubkeyRaw = await crypto.subtle.exportKey('spki', pub);

    const sealed = await importSigningKey(pkcs8);
    await saveKey(sealed);
    this.signingKey = Promise.resolve(sealed);

    const name = displayName?.trim();
    const identity: Identity = {
      pubkey: arrayBufferToBase64(pubkeyRaw),
      ...(name ? { username: name } : {}),
    };
    this.identity.set(identity);
    this.persistPublicPart();
    await this.requestPersistence();
    return identity;
  }

  /**
   * Crear o restaurar escribe en la misma entrada de IndexedDB que la
   * identidad actual: hacerlo sin querer la borra para siempre. La UI pregunta
   * antes; esto es la red de seguridad para cualquier otro llamador.
   */
  private assertCanReplace(replace?: boolean) {
    if (this.identity() && !replace) {
      throw new Error('Ya hay una identidad en este navegador: reemplazarla la borra.');
    }
  }

  /**
   * Firma un mensaje con la clave del navegador (ECDSA P-256 / SHA-256).
   *
   * La privada nunca sale de IndexedDB ni se materializa en JavaScript: el
   * `CryptoKey` es un handle opaco. Devuelve la firma cruda (P1363, r||s) en
   * base64, el formato que verifica el backend.
   */
  async sign(message: string): Promise<string> {
    const id = this.identity();
    if (!id) throw new Error('no identity');

    const key = await this.signingKey;
    if (!key) {
      throw new Error(
        'No hay clave de firma en este navegador. Si borraste los datos del ' +
        'sitio, la identidad se perdió con ellos.'
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
   * Borra la identidad de este navegador, **clave privada incluida**.
   *
   * No es "cerrar sesión": no hay sesión. Es irreversible salvo que el usuario
   * tenga el respaldo, así que sólo se llama tras una confirmación explícita.
   */
  clearIdentity() {
    localStorage.removeItem(this.storageKey);
    this.identity.set(null);
    this.signingKey = Promise.resolve(null);
    deleteKey().catch((err) => console.error('no se pudo borrar la clave', err));
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

// --- IndexedDB ---------------------------------------------------------------
//
// Se guarda el objeto `CryptoKey`, no sus bytes: el algoritmo de clonado
// estructurado lo soporta, y así el material de la clave nunca existe como dato
// en la página. localStorage no sirve para esto — sólo guarda strings, o sea que
// obliga a exportar la clave, que es exactamente lo que queremos evitar.

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

function saveKey(key: CryptoKey): Promise<unknown> {
  return withStore('readwrite', (store) => store.put(key, DB_KEY));
}

function loadKey(): Promise<CryptoKey | null> {
  return withStore<CryptoKey | undefined>('readonly', (store) => store.get(DB_KEY))
    .then((key) => key ?? null)
    .catch((err) => {
      console.error('no se pudo leer la clave de IndexedDB', err);
      return null;
    });
}

function deleteKey(): Promise<unknown> {
  return withStore('readwrite', (store) => store.delete(DB_KEY));
}

/** Importa una privada PKCS#8 como clave de firma **no extraíble**. */
function importSigningKey(pkcs8: ArrayBuffer): Promise<CryptoKey> {
  return crypto.subtle.importKey(
    'pkcs8', pkcs8, { name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign']);
}

// --- helpers -----------------------------------------------------------------

function base64ToArrayBuffer(b64: string): ArrayBuffer {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

function arrayBufferToBase64(buffer: ArrayBuffer): string {
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
    throw new Error('Pegá la clave secreta completa, con las líneas BEGIN y END.');
  }
  return base64ToArrayBuffer(b64);
}

function formatAsPem(b64: string): string {
  const lines = b64.match(/.{1,64}/g) || [b64];
  return `-----BEGIN PRIVATE KEY-----\n${lines.join('\n')}\n-----END PRIVATE KEY-----`;
}
