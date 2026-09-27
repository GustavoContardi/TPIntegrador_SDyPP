import { Component, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { RouterModule } from '@angular/router';
import { IdentityService, MIN_PASSPHRASE } from '../../core/services/identity.service';
import { friendlyError } from '../../core/utils/format';

/**
 * Identidad: quién sos ante la red.
 *
 * Un par de claves propio generado acá; la privada no sale del navegador y se
 * guarda cifrada con una contraseña que sólo conoce el usuario. En pantalla se
 * habla de "código público" y "clave secreta": son la clave pública y la
 * privada, dichas sin jerga.
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
          Sin cuentas ni datos personales. Tu identidad se crea en este navegador, la
          protege una contraseña que sólo vos conocés —no viaja a ningún lado— y con
          ella firmás todo lo que hacés en la red.
        </p>
      </div>

      <!-- ── identidad activa ────────────────────────────────────────────── -->
      <div class="card elev-sm vc-soft--75 active" *ngIf="identityService.identity() as id">
        <div class="active__top">
          <span class="card-kicker">Tu identidad activa</span>
          <span class="vc-actions">
            <span class="tag tag-accent">{{ id.username || 'sin nombre' }}</span>
            <span class="tag tag-neutral" *ngIf="identityService.protection() === 'vault'">
              {{ identityService.locked() ? 'bloqueada' : 'desbloqueada' }}
            </span>
            <span class="tag tag-neutral" *ngIf="identityService.protection() === 'legacy'">sin contraseña</span>
          </span>
        </div>

        <p class="vc-label active__label">Tu código público</p>
        <code class="vc-code-block">{{ id.pubkey }}</code>
        <p class="vc-note-sm active__hint">
          Es lo que la red ve de vos. Podés compartirlo sin problema: sirve para
          reconocerte, no para hacerse pasar por vos.
        </p>

        <div class="vc-actions active__cta">
          <button class="btn btn-secondary active__btn" (click)="copy(id.pubkey)">Copiar código</button>
          <ng-container *ngIf="identityService.protection() === 'vault'">
            <button class="btn btn-secondary active__btn" *ngIf="identityService.locked()"
                    (click)="unlock()">Desbloquear</button>
            <button class="btn btn-secondary active__btn" *ngIf="!identityService.locked()"
                    (click)="identityService.lock()">Bloquear</button>
            <button class="btn btn-secondary active__btn" (click)="downloadBackup()">Descargar respaldo</button>
          </ng-container>
          <button class="btn btn-ghost active__btn" (click)="clear()">Borrar identidad de este navegador</button>
        </div>

        <!-- Recién creada: el respaldo es lo primero. Se puede descargar de
             nuevo cuando sea (está cifrado), pero nadie vuelve a esta pantalla
             a buscarlo si no se lo pide ahora. -->
        <div class="vc-note vc-note--bad active__note" *ngIf="justCreated()">
          <strong>Descargá tu respaldo ahora.</strong>&ngsp;
          <span>Si se borran los datos de este navegador, o querés usar tu identidad en
            otro, lo vas a necesitar. Está cifrado con tu contraseña: guardalo donde
            quieras, sin ella no sirve de nada. Y si olvidás la contraseña, nadie puede
            recuperarla.</span>
          <div class="vc-actions active__cta">
            <button class="btn btn-secondary active__btn" (click)="downloadBackup()">Descargar respaldo</button>
            <button class="btn btn-ghost active__btn" (click)="justCreated.set(false)">Ya lo guardé</button>
          </div>
        </div>

        <ng-container *ngIf="!justCreated()">
          <p class="vc-note active__note" *ngIf="identityService.protection() === 'vault'">
            <strong>Tu clave secreta está cifrada con tu contraseña.</strong>&ngsp;
            <span>En este navegador sólo se guarda cifrada. Mientras la identidad está
              desbloqueada, la clave vive en memoria: se olvida al cerrar la pestaña o
              al tocar Bloquear, y para volver a firmar hay que escribir la contraseña.</span>
          </p>

          <!-- Identidades de antes de la contraseña: no se pueden cifrar sin su
               respaldo, porque la clave guardada no es extraíble. -->
          <p class="vc-note vc-note--warn active__note" *ngIf="identityService.protection() === 'legacy'">
            <strong>Tu clave secreta no tiene contraseña.</strong>&ngsp;
            <span>Se creó antes de que existieran y está guardada sin cifrar: quien
              acceda a los datos de este navegador puede usarla. Para protegerla,
              restaurala abajo desde el respaldo que guardaste al crearla (el texto
              BEGIN PRIVATE KEY) y elegí una contraseña.</span>
          </p>

          <!-- IndexedDB es best-effort: sin persistencia concedida, el navegador
               puede borrarlo solo (falta de espacio, o Safari a los 7 días sin
               visitas). Mejor que el usuario lo sepa a que lo descubra. -->
          <p class="vc-note vc-note--warn active__note"
             *ngIf="identityService.storagePersisted() === false">
            <strong>Tu navegador podría borrar la clave por su cuenta.</strong>&ngsp;
            <span>No garantizó conservar los datos de este sitio (pasa si le falta
              espacio, o en Safari tras una semana sin entrar). Tené tu respaldo a mano.</span>
          </p>
        </ng-container>
      </div>

      <!-- ── crear identidad ─────────────────────────────────────────────── -->
      <section class="vc-section own">
        <div class="card elev-sm vc-plain own__card">
          <span class="card-kicker">{{ identityService.identity() ? 'Otra identidad' : 'Empezá acá' }}</span>
          <h4 class="own__title">Crear mi identidad</h4>
          <p class="own__body">
            Elegí un nombre y una contraseña. Tu clave secreta se genera en este
            navegador y se guarda cifrada con esa contraseña.
          </p>

          <div class="field own__field">
            <label for="vc-name">Nombre para mostrar</label>
            <input id="vc-name" class="input" placeholder="Ej: Gustavo" maxlength="24"
                   autocomplete="off" [value]="displayName()" [disabled]="generating()"
                   (input)="displayName.set($any($event.target).value)"
                   (blur)="touched.set(true)">
            <p class="vc-note-sm own__hint">Se guarda sólo en este navegador; nunca viaja a la red.</p>
          </div>
          <div class="field own__field">
            <label for="vc-pass">Contraseña</label>
            <input id="vc-pass" class="input" type="password" autocomplete="new-password"
                   [value]="pass()" [disabled]="generating()"
                   (input)="pass.set($any($event.target).value)">
          </div>
          <div class="field own__field">
            <label for="vc-pass2">Repetí la contraseña</label>
            <input id="vc-pass2" class="input" type="password" autocomplete="new-password"
                   [value]="pass2()" [disabled]="generating()"
                   (input)="pass2.set($any($event.target).value)" (blur)="touched.set(true)">
            <p class="vc-note-sm own__hint">
              Al menos {{ minPass }} caracteres. Una frase larga es más fácil de recordar
              y más difícil de adivinar que una palabra rara.
            </p>
          </div>

          <label class="radio own__ack">
            <input type="checkbox" [checked]="acknowledged()" [disabled]="generating()"
                   (change)="acknowledged.set($any($event.target).checked)">
            <span class="dot own__box"></span>
            <span class="own__ack-text">
              Entiendo que nadie puede recuperar mi contraseña, y que si la olvido, o si
              se borran los datos de este navegador sin que yo haya descargado el
              respaldo, pierdo mi identidad.
            </span>
          </label>

          <p class="vc-note vc-note--bad own__error" *ngIf="touched() && createError()">
            {{ createError() }}
          </p>

          <button class="btn btn-primary own__submit" (click)="register()"
                  [disabled]="generating() || !!createError() || !acknowledged()">
            {{ generating() ? 'Creando identidad…' : 'Crear identidad' }}
          </button>
        </div>

        <!-- Restaurar: acepta el respaldo cifrado actual y el PEM en claro de
             antes. El PEM queda cifrado con una contraseña nueva: es también la
             forma de proteger una identidad "sin contraseña". -->
        <div class="card elev-sm vc-plain own__card">
          <span class="card-kicker">¿Ya tenés una?</span>
          <h4 class="own__title">Restaurar desde respaldo</h4>
          <p class="own__body">
            Subí el archivo de respaldo o pegá su contenido. Tu código público, tus
            mineros y tus equipos siguen siendo los mismos.
          </p>

          <div class="field own__field">
            <label for="vc-backup">Respaldo</label>
            <textarea id="vc-backup" class="input own__pem" spellcheck="false" autocomplete="off"
                      placeholder="Pegá acá el respaldo" [value]="restoreText()"
                      [disabled]="restoring()"
                      (input)="restoreText.set($any($event.target).value)"></textarea>
            <input type="file" class="own__file" accept=".json,.pem,.txt,application/json"
                   [disabled]="restoring()" (change)="loadFile($event)">
          </div>
          <div class="field own__field">
            <label for="vc-rpass">{{ restoreIsPem() ? 'Contraseña nueva' : 'Contraseña del respaldo' }}</label>
            <input id="vc-rpass" class="input" type="password"
                   [attr.autocomplete]="restoreIsPem() ? 'new-password' : 'current-password'"
                   [value]="restorePass()" [disabled]="restoring()"
                   (input)="restorePass.set($any($event.target).value)">
          </div>
          <ng-container *ngIf="restoreIsPem()">
            <div class="field own__field">
              <label for="vc-rpass2">Repetí la contraseña nueva</label>
              <input id="vc-rpass2" class="input" type="password" autocomplete="new-password"
                     [value]="restorePass2()" [disabled]="restoring()"
                     (input)="restorePass2.set($any($event.target).value)">
              <p class="vc-note-sm own__hint">
                Ese respaldo es de antes y no tiene contraseña: elegí una de al menos
                {{ minPass }} caracteres y desde ahora queda cifrada con ella.
              </p>
            </div>
          </ng-container>
          <div class="field own__field">
            <label for="vc-rname">Nombre para mostrar (opcional)</label>
            <input id="vc-rname" class="input" maxlength="24" autocomplete="off"
                   [value]="restoreName()" [disabled]="restoring()"
                   (input)="restoreName.set($any($event.target).value)">
          </div>

          <button class="btn btn-secondary own__submit" (click)="restore()"
                  [disabled]="restoring() || !canRestore()">
            {{ restoring() ? 'Restaurando…' : 'Restaurar identidad' }}
          </button>
        </div>

        <div class="card elev-sm vc-plain own__card">
          <span class="card-kicker">Por qué es seguro</span>
          <h4 class="own__title">Nadie puede votar por vos</h4>
          <p class="own__body">
            Cada cosa que hacés —proponer una ley, sumar un minero, fundar un equipo—
            va firmada con tu clave secreta, y esa clave nunca sale de tu navegador.
            Ni siquiera tus mineros la necesitan: cada uno tiene la suya propia,
            vinculada a vos.
          </p>
        </div>
      </section>
    </main>
  `,
  styles: [`
    .active { margin-top: 40px; padding: 26px; }
    .active__top { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 14px; }
    .active__label { margin: 18px 0 8px; }
    .active__hint { margin-top: 8px; }
    .active__cta { margin-top: 18px; gap: 12px; }
    .active__btn { font-size: 13px; }
    .active__note { margin-top: 16px; }

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
    .own__pem { min-height: 110px; resize: vertical; font: 11.5px var(--font-mono); }
    .own__file { margin-top: 8px; font-size: 12px; color: var(--color-neutral-400); }
  `]
})
export class IdentityComponent {
  identityService = inject(IdentityService);
  private snackBar = inject(MatSnackBar);

  readonly minPass = MIN_PASSPHRASE;

  /** Recién creada: se insiste con el respaldo hasta que el usuario diga que lo guardó. */
  justCreated = signal(false);

  // ── crear ───────────────────────────────────────────────────────────────
  generating = signal(false);
  displayName = signal('');
  pass = signal('');
  pass2 = signal('');
  acknowledged = signal(false);
  touched = signal(false);

  /** El primer problema del formulario de alta, o '' si está completo. */
  createError = computed(() => {
    const n = this.displayName().trim();
    if (n.length < 3 || n.length > 24) return 'Elegí un nombre de entre 3 y 24 caracteres.';
    if ([...this.pass()].length < MIN_PASSPHRASE) {
      return `La contraseña tiene que tener al menos ${MIN_PASSPHRASE} caracteres.`;
    }
    if (this.pass() !== this.pass2()) return 'Las dos contraseñas no coinciden.';
    return '';
  });

  // ── restaurar ───────────────────────────────────────────────────────────
  /** El texto del respaldo se vacía apenas se usa: no queda ni en memoria. */
  restoreText = signal('');
  restorePass = signal('');
  restorePass2 = signal('');
  restoreName = signal('');
  restoring = signal(false);

  /** Un PEM en claro (respaldo viejo) se cifra con una contraseña nueva. */
  restoreIsPem = computed(() => {
    const t = this.restoreText().trim();
    return !!t && !t.startsWith('{');
  });

  canRestore = computed(() => {
    if (!this.restoreText().trim() || !this.restorePass()) return false;
    if (!this.restoreIsPem()) return true;
    return [...this.restorePass()].length >= MIN_PASSPHRASE
      && this.restorePass() === this.restorePass2();
  });

  async register() {
    this.touched.set(true);
    if (this.createError() || !this.acknowledged()) return;
    if (!this.confirmReplace()) return;

    this.generating.set(true);
    try {
      const name = this.displayName().trim();
      await this.identityService.generateKeypair(name, this.pass(), { replace: true });
      this.justCreated.set(true);
      this.snackBar.open(`¡Listo, ${name}! Tu identidad está creada. Descargá tu respaldo.`,
                         'Cerrar', { duration: 6000 });
      this.displayName.set('');
      this.pass.set('');
      this.pass2.set('');
      this.acknowledged.set(false);
      this.touched.set(false);
    } catch (err: any) {
      console.error(err);
      this.snackBar.open(friendlyError(err, 'No se pudo crear la identidad. Probá de nuevo.'), 'Cerrar', { duration: 4000 });
    } finally {
      this.generating.set(false);
    }
  }

  async restore() {
    if (!this.canRestore() || !this.confirmReplace()) return;

    this.restoring.set(true);
    try {
      const id = await this.identityService.restore(
        this.restoreText(), this.restorePass(), this.restoreName(), { replace: true });
      this.justCreated.set(false);
      this.restoreText.set('');
      this.restorePass.set('');
      this.restorePass2.set('');
      this.restoreName.set('');
      this.snackBar.open(`Identidad restaurada${id.username ? `, ${id.username}` : ''}.`,
                         'Cerrar', { duration: 4000 });
    } catch (err: any) {
      console.error(err);
      this.snackBar.open(friendlyError(err, 'No se pudo restaurar la identidad.'),
                         'Cerrar', { duration: 5000 });
    } finally {
      this.restoring.set(false);
    }
  }

  /** Lee el archivo elegido al textarea; restaurar sigue siendo un paso aparte. */
  async loadFile(event: Event) {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    // Un respaldo pesa ~1 kB: algo mucho más grande no lo es.
    if (file.size > 64 * 1024) {
      this.snackBar.open('Ese archivo no es un respaldo de VoxChain.', 'Cerrar', { duration: 4000 });
    } else {
      this.restoreText.set(await file.text());
    }
    input.value = '';
  }

  async unlock() {
    try {
      await this.identityService.requestUnlock();
    } catch {
      // Canceló: la identidad sigue bloqueada y la pantalla ya lo muestra.
    }
  }

  /**
   * Descarga el respaldo cifrado. Se puede repetir cuantas veces haga falta:
   * sin la contraseña, el archivo no permite firmar nada.
   */
  downloadBackup() {
    const json = this.identityService.backupJson();
    if (!json) return;
    const name = (this.identityService.getUsername() || 'identidad')
      .toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'identidad';
    const url = URL.createObjectURL(new Blob([json], { type: 'application/json' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = `voxchain-${name}-respaldo.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  /**
   * Crear o restaurar pisa la identidad actual, y sin su respaldo no vuelve.
   * Devuelve true si no hay nada que pisar o si el usuario confirmó.
   */
  private confirmReplace(): boolean {
    const actual = this.identityService.identity();
    if (!actual) return true;
    return confirm(
      `Ya tenés una identidad en este navegador (${actual.username || 'sin nombre'}).\n\n` +
      'Si seguís, se reemplaza y su clave secreta se borra. Sólo vas a poder ' +
      'recuperarla con su respaldo.');
  }

  clear() {
    if (!confirm(
      '¿Borrar tu identidad de este navegador?\n\n' +
      'Se borra tu clave secreta. Sin tu respaldo (y su contraseña) no hay forma ' +
      'de recuperarla: nadie puede devolvértela, y tus mineros y equipos quedan a ' +
      'nombre de una identidad que ya no vas a poder usar.')) {
      return;
    }
    this.identityService.clearIdentity();
    this.justCreated.set(false);
    this.snackBar.open('Identidad borrada.', 'Cerrar', { duration: 2000 });
  }

  copy(text: string) {
    navigator.clipboard.writeText(text);
    this.snackBar.open('Código público copiado.', 'Cerrar', { duration: 2000 });
  }
}
