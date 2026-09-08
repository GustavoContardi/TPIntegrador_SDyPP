import { Injectable, signal, computed, inject } from '@angular/core';
import { AccountsService } from './accounts.service';

export interface Identity {
  pubkey: string;
  username?: string; // nombre para mostrar, o usuario de la cuenta demo
  isDemo?: boolean;
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
  isDemoMode = computed(() => {
    const id = this.identity();
    return id?.isDemo === true;
  });

  private storageKey = 'voxchain_identity';
  private accountsService = inject(AccountsService);

  /**
   * La clave de firma, que vive en IndexedDB y nunca en JavaScript.
   *
   * Es una promesa porque `sign()` puede llamarse antes de que termine la
   * lectura inicial; esperar acá evita que la primera firma de la sesión falle
   * por una carrera con el arranque.
   */
  private signingKey: Promise<CryptoKey | null> = Promise.resolve(null);

  constructor() {
    this.loadFromStorage();
    this.syncWithDemoAccount();
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
      ...(stored.isDemo ? { isDemo: true } : {}),
    });

    // Migración de identidades viejas: hasta ahora la privada se guardaba
    // exportada en localStorage, en texto plano. Cualquier XSS podía leerla y
    // quedarse con la identidad para siempre. Al abrir la app se reimporta como
    // clave NO extraíble, se pasa a IndexedDB y se borra el original.
    this.signingKey = stored.exportedPrivkey
      ? this.migrateLegacyKey(stored.exportedPrivkey)
      : loadKey();
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

  private syncWithDemoAccount() {
    // Si hay una cuenta demo seleccionada, su pubkey manda.
    const demoAccount = this.accountsService.selectedAccount();
    if (demoAccount) {
      this.identity.set({
        pubkey: demoAccount.pubkey,
        username: demoAccount.username,
        isDemo: true
      });
      this.signingKey = Promise.resolve(null);
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
  async generateKeypair(displayName?: string): Promise<NewIdentity> {
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
      isDemo: false,
      ...(name ? { username: name } : {}),
    };
    this.identity.set(identity);
    this.persistPublicPart();

    return { identity, pemBackup: formatAsPem(arrayBufferToBase64(privkeyRaw)) };
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

    // En modo demo la privada la tiene el backend, no el navegador.
    if (id.isDemo) {
      throw new Error('Demo mode: signing is handled by backend');
    }

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

function formatAsPem(b64: string): string {
  const lines = b64.match(/.{1,64}/g) || [b64];
  return `-----BEGIN PRIVATE KEY-----\n${lines.join('\n')}\n-----END PRIVATE KEY-----`;
}
