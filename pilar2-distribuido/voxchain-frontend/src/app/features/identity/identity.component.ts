import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { RouterModule } from '@angular/router';
import { IdentityService } from '../../core/services/identity.service';
import { AccountsService, DemoAccount } from '../../core/services/accounts.service';

/**
 * Identidad: quién sos ante la red.
 *
 * Las dos formas de entrar viven en la misma pantalla y no en dos, porque son
 * la misma decisión: una cuenta demo del despliegue (las firmas las resuelve el
 * backend) o un par de claves propio generado acá (la privada no sale del
 * navegador). Antes las cuentas demo estaban en `/select-account`, a un clic de
 * distancia; la ruta sigue existiendo, pero el camino normal es este.
 */
@Component({
  selector: 'app-identity',
  standalone: true,
  imports: [CommonModule, RouterModule, MatSnackBarModule],
  template: `
    <main class="vc-page vc-page--narrow">
      <div class="vc-head__text">
        <h6 class="vc-kicker">Identidad</h6>
        <h1 class="vc-title">Quién sos ante la red</h1>
        <p class="vc-lead">
          Ante la red te identifica tu clave pública. Podés entrar con una cuenta demo
          del despliegue o generar un par de claves propio en este navegador.
        </p>
      </div>

      <!-- ── identidad activa ────────────────────────────────────────────── -->
      <div class="card elev-sm vc-soft--75 active" *ngIf="identityService.identity() as id">
        <div class="active__top">
          <span class="card-kicker">
            Identidad activa · {{ identityService.isDemoMode() ? 'modo demo' : 'claves propias' }}
          </span>
          <span class="tag tag-accent">{{ id.username || 'sin nombre' }}</span>
        </div>

        <p class="vc-label active__label">Clave pública (SPKI en base64)</p>
        <code class="vc-code-block">{{ id.pubkey }}</code>

        <div class="vc-actions active__cta">
          <button class="btn btn-secondary active__btn" (click)="copy(id.pubkey, 'Clave pública copiada.')">
            Copiar clave
          </button>
          <button class="btn btn-ghost active__btn" *ngIf="identityService.isDemoMode()"
                  (click)="releaseCurrent()">Liberar la cuenta</button>
          <button class="btn btn-ghost active__btn" *ngIf="!identityService.isDemoMode()"
                  (click)="clear()">Borrar identidad</button>
        </div>

        <p class="vc-note" *ngIf="identityService.isDemoMode()">
          En modo demo las firmas las resuelve el worker en el backend: no hay clave
          privada en este navegador.
        </p>

        <!-- Respaldo de una sola vez: existe sólo mientras dure esta pantalla,
             recién creada la identidad, y no se guarda en ningún lado. Es la
             contracara de que la clave sea no exportable. -->
        <ng-container *ngIf="pemBackup() as pem">
          <div class="vc-note vc-note--bad backup">
            <strong>Guardá esto ahora.</strong>&ngsp;
            <span>Es la única vez que vas a ver tu clave privada: el navegador la
              almacena de forma no exportable, así que ni la app ni vos pueden volver a
              leerla. Copiala a un archivo <code class="vc-code">.pem</code> en un lugar
              seguro.</span>
          </div>
          <pre class="vc-code-block backup__pem">{{ pem }}</pre>
          <div class="vc-actions active__cta">
            <button class="btn btn-secondary active__btn" (click)="copy(pem, 'PEM copiado.')">Copiar PEM</button>
            <button class="btn btn-ghost active__btn" (click)="pemBackup.set(null)">Ya la guardé</button>
          </div>
        </ng-container>

        <p class="vc-note" *ngIf="!identityService.isDemoMode() && !pemBackup()">
          <strong>Tu clave privada no es exportable.</strong>&ngsp;
          <span>El navegador firma con ella pero no puede entregarla, ni a esta app ni a
            ningún script. Borrar los datos del sitio la elimina para siempre — y no
            hace falta dársela a ningún minero: cada uno genera la suya al arrancar y se
            vincula a la tuya con un token de un solo uso.</span>
        </p>
      </div>

      <!-- ── cuentas demo ────────────────────────────────────────────────── -->
      <section class="vc-section">
        <h3 class="vc-h3 vc-h3--sm">Cuentas demo</h3>
        <p class="vc-lead demo__lead">
          Las cuentas del despliegue de demo, una por worker. Se reservan por sesión.
        </p>

        <div class="vc-table-wrap" *ngIf="accounts().length; else sinCuentas">
          <table class="table demo__table">
            <thead>
              <tr>
                <th>Cuenta</th>
                <th>Minero</th>
                <th>Modo</th>
                <th>Estado</th>
                <th class="num"></th>
              </tr>
            </thead>
            <tbody>
              <tr *ngFor="let a of accounts()">
                <td class="demo__user">{{ a.username }}</td>
                <td class="mono demo__worker">{{ a.worker_id }}</td>
                <td>
                  <span class="tag" [ngClass]="modeCls(a.mode)">{{ a.mode }}</span>
                </td>
                <td class="demo__state" [class.mine]="isMine(a)">{{ stateLabel(a) }}</td>
                <td class="num">
                  <button class="btn btn-ghost demo__btn" *ngIf="isMine(a)"
                          (click)="release(a)">Liberar</button>
                  <button class="btn btn-ghost demo__btn" *ngIf="!isMine(a)"
                          [disabled]="a.status === 'occupied' || busy()"
                          (click)="use(a)">Usar</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <ng-template #sinCuentas>
          <p class="vc-empty">{{ loadingAccounts() ? 'Cargando cuentas…' : 'No se pudieron leer las cuentas demo.' }}</p>
        </ng-template>
      </section>

      <!-- ── identidad propia ────────────────────────────────────────────── -->
      <section class="vc-section--divided own">
        <div class="card elev-sm vc-plain own__card">
          <span class="card-kicker">Identidad propia</span>
          <h4 class="own__title">Generar un par de claves</h4>
          <p class="own__body">
            ECDSA P-256 generado en tu navegador. La privada queda guardada de forma no
            exportable: vas a poder respaldarla una sola vez, y borrar los datos del
            sitio la elimina para siempre.
          </p>

          <div class="field own__field">
            <label for="vc-name">Nombre para mostrar</label>
            <input id="vc-name" class="input" placeholder="Ej: Gustavo" maxlength="24"
                   autocomplete="off" [value]="displayName()" [disabled]="generating()"
                   (input)="displayName.set($any($event.target).value)"
                   (blur)="nameTouched.set(true)">
            <p class="vc-note-sm own__hint">Se guarda sólo en este navegador; nunca viaja a la red.</p>
          </div>

          <label class="radio own__ack">
            <input type="checkbox" [checked]="acknowledged()" [disabled]="generating()"
                   (change)="acknowledged.set($any($event.target).checked)">
            <span class="dot own__box"></span>
            <span class="own__ack-text">
              Entiendo que mi clave privada queda guardada en este navegador de forma no
              exportable, que sólo voy a poder verla una vez para respaldarla, y que
              borrar los datos del sitio la elimina para siempre.
            </span>
          </label>

          <p class="vc-note vc-note--bad own__error" *ngIf="nameTouched() && !nameValid()">
            Elegí un nombre de entre 3 y 24 caracteres.
          </p>

          <button class="btn btn-primary own__submit" (click)="register()"
                  [disabled]="generating() || !canRegister()">
            {{ generating() ? 'Creando identidad…' : 'Crear identidad' }}
          </button>
        </div>

        <div class="card elev-sm vc-plain own__card">
          <span class="card-kicker">Tu clave privada nunca viaja</span>
          <h4 class="own__title">Los mineros no la necesitan</h4>
          <p class="own__body">
            Cada minero genera su propia identidad al arrancar y se vincula a la tuya con
            un token de enrolamiento de un solo uso. Lo que firmás con tu clave son las
            acciones de administración: registrar, cambiar de modo, fijar política.
          </p>
        </div>
      </section>
    </main>
  `,
  styles: [`
    .active { margin-top: 40px; padding: 26px; }
    .active__top { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 14px; }
    .active__label { margin: 18px 0 8px; }
    .active__cta { margin-top: 18px; gap: 12px; }
    .active__btn { font-size: 13px; }

    .backup { margin-top: 20px; }
    .backup__pem { margin-top: 12px; white-space: pre; word-break: normal; overflow-x: auto; }

    .demo__lead { margin: 6px 0 22px; font-size: 13.5px; }
    .demo__table { min-width: 720px; }
    .demo__user { font-size: 13.5px; color: var(--color-neutral-100); }
    .demo__worker { font-size: 12.5px; color: var(--color-neutral-300); }
    .demo__state { font-size: 13px; color: var(--color-neutral-300); }
    .demo__state.mine { color: var(--color-accent-300); }
    .demo__btn { font-size: 12.5px; }

    .own { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }
    .own__card { padding: 24px; }
    .own__title { margin: 12px 0 10px; font-size: 18px; color: var(--color-neutral-100); }
    .own__body { margin: 0 0 18px; font-size: 13px; line-height: 1.7; color: var(--color-neutral-400); }
    .own__field { margin-bottom: 14px; }
    .own__hint { margin-top: 6px; }
    /* El acuerdo es un párrafo, no una línea: la casilla se alinea con la
       primera línea del texto en vez de centrarse sobre el bloque entero. */
    .own__ack { align-items: flex-start; gap: 10px; font-size: 12.5px; line-height: 1.6; color: var(--color-neutral-400); }
    .own__box { width: 16px; height: 16px; border-radius: var(--radius-sm); background: transparent;
                border: 1.5px solid var(--color-divider); margin-top: 2px; }
    .own__ack input:checked + .own__box {
      border-color: var(--color-accent); background: var(--color-accent);
      box-shadow: inset 0 0 0 3px var(--color-bg);
    }
    .own__ack:hover .own__box { border-color: var(--color-accent); }
    .own__ack-text { flex: 1; }
    .own__error { margin-top: 14px; }
    .own__submit { align-self: flex-start; margin-top: 18px; min-height: 40px; padding: 0 20px; }
  `]
})
export class IdentityComponent implements OnInit {
  identityService = inject(IdentityService);
  accountsService = inject(AccountsService);
  private snackBar = inject(MatSnackBar);

  accounts = signal<DemoAccount[]>([]);
  loadingAccounts = signal(true);
  busy = signal(false);
  generating = signal(false);

  /**
   * El PEM de respaldo, sólo mientras dure esta pantalla.
   *
   * No se persiste a propósito: es la contracara de que la clave sea no
   * exportable. Si se guardara en algún lado para poder mostrarlo de nuevo,
   * volveríamos a tener una copia legible de la privada, que es justo lo que
   * este diseño elimina.
   */
  pemBackup = signal<string | null>(null);

  displayName = signal('');
  acknowledged = signal(false);
  nameTouched = signal(false);

  /** Entre 3 y 24 caracteres una vez recortados los espacios. */
  nameValid = computed(() => {
    const n = this.displayName().trim();
    return n.length >= 3 && n.length <= 24;
  });

  canRegister = computed(() => this.nameValid() && this.acknowledged());

  ngOnInit() {
    this.loadAccounts();
  }

  // -- cuentas demo ---------------------------------------------------------

  private loadAccounts() {
    this.accountsService.listAccounts().subscribe({
      next: (accounts) => {
        this.accounts.set(accounts);
        this.loadingAccounts.set(false);
      },
      // Sin reintento automático: la pantalla sigue siendo útil sin la tabla
      // (podés generar claves propias), y un bucle de reintentos contra un
      // backend caído sólo llena la consola.
      error: () => this.loadingAccounts.set(false),
    });
  }

  /** La cuenta que tiene reservada esta sesión. */
  isMine(account: DemoAccount): boolean {
    return this.accountsService.selectedAccount()?.username === account.username;
  }

  stateLabel(account: DemoAccount): string {
    if (this.isMine(account)) return 'tu sesión';
    return account.status === 'occupied' ? 'ocupada' : 'libre';
  }

  modeCls(mode: string): string {
    return mode === 'pool-coordinator' ? 'tag-accent'
      : mode === 'standalone' ? 'tag-outline' : 'tag-neutral';
  }

  use(account: DemoAccount) {
    if (account.status === 'occupied') {
      this.snackBar.open('Esa cuenta ya está en uso por otra sesión.', 'Cerrar', { duration: 3000 });
      return;
    }
    this.busy.set(true);
    this.accountsService.reserveAccount(account.username).subscribe({
      next: (res) => {
        this.busy.set(false);
        if (res.status !== 'reserved' && res.status !== 'already_reserved') return;
        this.accountsService.setSelectedAccount(account);
        this.identityService.identity.set({
          pubkey: account.pubkey,
          username: account.username,
          isDemo: true,
        });
        this.snackBar.open(`Entraste como "${account.username}".`, 'Cerrar', { duration: 2500 });
        this.loadAccounts();
      },
      error: (err) => {
        this.busy.set(false);
        this.snackBar.open(
          err?.status === 409
            ? 'Esa cuenta ya está en uso por otra sesión.'
            : 'No se pudo tomar la cuenta. Probá de nuevo.',
          'Cerrar', { duration: 3000 });
        this.loadAccounts();
      },
    });
  }

  release(account: DemoAccount) {
    this.busy.set(true);
    this.accountsService.releaseAccount(account.username).subscribe({
      next: () => {
        this.busy.set(false);
        this.accountsService.setSelectedAccount(null);
        this.identityService.clearIdentity();
        this.snackBar.open(`Cuenta "${account.username}" liberada.`, 'Cerrar', { duration: 2500 });
        this.loadAccounts();
      },
      error: () => {
        this.busy.set(false);
        this.snackBar.open('No se pudo liberar la cuenta.', 'Cerrar', { duration: 3000 });
      },
    });
  }

  /** Suelta la cuenta demo activa desde la ficha de arriba. */
  releaseCurrent() {
    const current = this.accountsService.selectedAccount();
    if (current) {
      this.release(current);
      return;
    }
    // Sin reserva en esta sesión no hay nada que devolver; se sale y listo.
    this.identityService.clearIdentity();
  }

  // -- identidad propia -----------------------------------------------------

  async register() {
    this.nameTouched.set(true);
    if (!this.canRegister()) return;

    this.generating.set(true);
    try {
      const name = this.displayName().trim();
      const { pemBackup } = await this.identityService.generateKeypair(name);
      this.pemBackup.set(pemBackup);
      this.snackBar.open(`Identidad creada para ${name}. Guardá tu clave privada.`,
                         'Cerrar', { duration: 6000 });
      this.displayName.set('');
      this.acknowledged.set(false);
      this.nameTouched.set(false);
    } catch (err: any) {
      console.error(err);
      this.snackBar.open('No se pudo generar: ' + err.message, 'Cerrar', { duration: 3000 });
    } finally {
      this.generating.set(false);
    }
  }

  clear() {
    this.identityService.clearIdentity();
    this.pemBackup.set(null);
    this.snackBar.open('Identidad borrada.', 'Cerrar', { duration: 2000 });
  }

  copy(text: string, message: string) {
    navigator.clipboard.writeText(text);
    this.snackBar.open(message, 'Cerrar', { duration: 2000 });
  }
}
