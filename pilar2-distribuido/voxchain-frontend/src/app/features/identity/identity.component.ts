import { Component, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { RouterModule } from '@angular/router';
import { IdentityService } from '../../core/services/identity.service';
import { friendlyError } from '../../core/utils/format';

/**
 * Identidad: quién sos ante la red.
 *
 * Un par de claves propio generado acá; la privada no sale del navegador.
 * En pantalla se habla de "código público" y "clave secreta": son la
 * clave pública y la privada, dichas sin jerga.
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
          Sin cuentas, sin contraseñas, sin datos personales. Tu identidad se crea en
          este navegador en un par de segundos y con ella firmás todo lo que hacés en
          la red.
        </p>
      </div>

      <!-- ── identidad activa ────────────────────────────────────────────── -->
      <div class="card elev-sm vc-soft--75 active" *ngIf="identityService.identity() as id">
        <div class="active__top">
          <span class="card-kicker">Tu identidad activa</span>
          <span class="tag tag-accent">{{ id.username || 'sin nombre' }}</span>
        </div>

        <p class="vc-label active__label">Tu código público</p>
        <code class="vc-code-block">{{ id.pubkey }}</code>
        <p class="vc-note-sm active__hint">
          Es lo que la red ve de vos. Podés compartirlo sin problema: sirve para
          reconocerte, no para hacerse pasar por vos.
        </p>

        <div class="vc-actions active__cta">
          <button class="btn btn-secondary active__btn" (click)="copy(id.pubkey, 'Código público copiado.')">
            Copiar código
          </button>
          <button class="btn btn-ghost active__btn" (click)="clear()">Borrar identidad de este navegador</button>
        </div>

        <!-- Respaldo de una sola vez: existe sólo mientras dure esta pantalla,
             recién creada la identidad, y no se guarda en ningún lado. Es la
             contracara de que la clave sea no exportable. -->
        <ng-container *ngIf="pemBackup() as pem">
          <div class="vc-note vc-note--bad backup">
            <strong>Guardá tu clave secreta ahora.</strong>&ngsp;
            <span>Es la única vez que la vas a ver. Copiala y guardala en un lugar
              seguro: si borrás los datos de este navegador, o querés usar tu identidad
              en otro, la recuperás pegándola en "Restaurar desde respaldo".</span>
          </div>
          <pre class="vc-code-block backup__pem">{{ pem }}</pre>
          <div class="vc-actions active__cta">
            <button class="btn btn-secondary active__btn" (click)="copy(pem, 'Clave secreta copiada.')">Copiar clave secreta</button>
            <button class="btn btn-ghost active__btn" (click)="pemBackup.set(null)">Ya la guardé</button>
          </div>
        </ng-container>

        <p class="vc-note" *ngIf="!pemBackup()">
          <strong>Tu clave secreta está protegida.</strong>&ngsp;
          <span>Tu navegador la usa para firmar, pero nunca la entrega: ni a esta app,
            ni a tus mineros, ni a nadie. Ojo: si borrás los datos del sitio, se borra
            con ellos, y sólo la recuperás con tu respaldo.</span>
        </p>

        <!-- IndexedDB es best-effort: sin persistencia concedida, el navegador
             puede borrarlo solo (falta de espacio, o Safari a los 7 días sin
             visitas). Mejor que el usuario lo sepa a que lo descubra. -->
        <p class="vc-note vc-note--warn active__persist"
           *ngIf="!pemBackup() && identityService.storagePersisted() === false">
          <strong>Tu navegador podría borrar la clave por su cuenta.</strong>&ngsp;
          <span>No garantizó conservar los datos de este sitio (pasa si le falta
            espacio, o en Safari tras una semana sin entrar). Tené tu respaldo a mano.</span>
        </p>
      </div>

      <!-- ── crear identidad ─────────────────────────────────────────────── -->
      <section class="vc-section own">
        <div class="card elev-sm vc-plain own__card">
          <span class="card-kicker">{{ identityService.identity() ? 'Otra identidad' : 'Empezá acá' }}</span>
          <h4 class="own__title">Crear mi identidad</h4>
          <p class="own__body">
            Elegí un nombre y listo. Tu clave secreta se genera y se guarda sólo en este
            navegador: vas a poder respaldarla una única vez.
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
              Entiendo que mi clave secreta vive sólo en este navegador, que voy a poder
              verla una única vez para respaldarla y que, si borro los datos del sitio,
              la pierdo salvo que tenga ese respaldo.
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

        <!-- Restaurar: lo que hace útil al respaldo. Sin esto, el PEM sólo
             servía para scripts/propose_law.py. -->
        <div class="card elev-sm vc-plain own__card">
          <span class="card-kicker">¿Ya tenés una?</span>
          <h4 class="own__title">Restaurar desde respaldo</h4>
          <p class="own__body">
            Pegá la clave secreta que guardaste al crearla. Tu código público, tus
            mineros y tus equipos siguen siendo los mismos.
          </p>

          <div class="field own__field">
            <label for="vc-pem">Clave secreta</label>
            <textarea id="vc-pem" class="input own__pem" spellcheck="false" autocomplete="off"
                      placeholder="-----BEGIN PRIVATE KEY-----" [value]="restorePem()"
                      [disabled]="restoring()"
                      (input)="restorePem.set($any($event.target).value)"></textarea>
          </div>
          <div class="field own__field">
            <label for="vc-rname">Nombre para mostrar (opcional)</label>
            <input id="vc-rname" class="input" maxlength="24" autocomplete="off"
                   [value]="restoreName()" [disabled]="restoring()"
                   (input)="restoreName.set($any($event.target).value)">
          </div>

          <button class="btn btn-secondary own__submit" (click)="restore()"
                  [disabled]="restoring() || !restorePem().trim()">
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

    .backup { margin-top: 20px; }
    .backup__pem { margin-top: 12px; white-space: pre; word-break: normal; overflow-x: auto; }

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
    .active__persist { margin-top: 12px; }
  `]
})
export class IdentityComponent {
  identityService = inject(IdentityService);
  private snackBar = inject(MatSnackBar);

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

  /** Formulario de restauración. El PEM se vacía apenas se usa: no queda ni en memoria. */
  restorePem = signal('');
  restoreName = signal('');
  restoring = signal(false);

  displayName = signal('');
  acknowledged = signal(false);
  nameTouched = signal(false);

  /** Entre 3 y 24 caracteres una vez recortados los espacios. */
  nameValid = computed(() => {
    const n = this.displayName().trim();
    return n.length >= 3 && n.length <= 24;
  });

  canRegister = computed(() => this.nameValid() && this.acknowledged());

  async register() {
    this.nameTouched.set(true);
    if (!this.canRegister()) return;

    if (!this.confirmReplace()) return;

    this.generating.set(true);
    try {
      const name = this.displayName().trim();
      const { pemBackup } = await this.identityService.generateKeypair(name, { replace: true });
      this.pemBackup.set(pemBackup);
      this.snackBar.open(`¡Listo, ${name}! Tu identidad está creada. Guardá tu clave secreta.`,
                         'Cerrar', { duration: 6000 });
      this.displayName.set('');
      this.acknowledged.set(false);
      this.nameTouched.set(false);
    } catch (err: any) {
      console.error(err);
      this.snackBar.open(friendlyError(err, 'No se pudo crear la identidad. Probá de nuevo.'), 'Cerrar', { duration: 4000 });
    } finally {
      this.generating.set(false);
    }
  }

  async restore() {
    if (!this.confirmReplace()) return;

    this.restoring.set(true);
    try {
      const id = await this.identityService.restoreFromPem(
        this.restorePem(), this.restoreName(), { replace: true });
      this.pemBackup.set(null);
      this.restorePem.set('');
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
      'recuperarla con el respaldo que guardaste al crearla.');
  }

  clear() {
    if (!confirm(
      '¿Borrar tu identidad de este navegador?\n\n' +
      'Se borra tu clave secreta. Sin el respaldo que guardaste al crearla no hay ' +
      'forma de recuperarla: nadie puede devolvértela, y tus mineros y equipos ' +
      'quedan a nombre de una identidad que ya no vas a poder usar.')) {
      return;
    }
    this.identityService.clearIdentity();
    this.pemBackup.set(null);
    this.snackBar.open('Identidad borrada.', 'Cerrar', { duration: 2000 });
  }

  copy(text: string, message: string) {
    navigator.clipboard.writeText(text);
    this.snackBar.open(message, 'Cerrar', { duration: 2000 });
  }
}
