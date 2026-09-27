import { Component, ElementRef, effect, inject, signal, viewChild } from '@angular/core';
import { CommonModule } from '@angular/common';
import { IdentityService } from '../services/identity.service';

/**
 * Pedido de contraseña para desbloquear la identidad.
 *
 * Vive una sola vez, en el caparazón de la app, y aparece cuando
 * `IdentityService.sign()` necesita la clave y la identidad está bloqueada (o
 * cuando el usuario toca "Desbloquear"). Así ninguna pantalla que firma tiene
 * que saber de contraseñas: llaman a `sign()` y, si hace falta, esto se
 * interpone hasta que el usuario desbloquea o cancela.
 */
@Component({
  selector: 'app-unlock-prompt',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="up" *ngIf="identityService.unlockRequested()" (keydown.escape)="cancel()">
      <form class="card elev-md up__card" role="dialog" aria-modal="true" aria-labelledby="up-title"
            (submit)="$event.preventDefault(); submit()">
        <span class="card-kicker">Tu identidad está bloqueada</span>
        <h4 id="up-title" class="up__title">Escribí tu contraseña para firmar</h4>
        <p class="vc-note-sm">
          Tu clave secreta está cifrada en este navegador. La contraseña la descifra
          sólo en memoria, hasta que cierres la pestaña o toques Bloquear.
        </p>
        <!-- El nombre de usuario oculto ayuda a los gestores de contraseñas a
             asociar lo que guarden con esta identidad. -->
        <input type="text" class="up__user" autocomplete="username" tabindex="-1" aria-hidden="true"
               [value]="identityService.getUsername() || ''" readonly>
        <input #pass class="input up__pass" type="password" autocomplete="current-password"
               aria-label="Contraseña" [value]="value()" [disabled]="busy()"
               (input)="value.set($any($event.target).value); error.set('')">
        <p class="vc-note vc-note--bad up__error" *ngIf="error()">{{ error() }}</p>
        <div class="vc-actions up__cta">
          <button type="button" class="btn btn-ghost" (click)="cancel()" [disabled]="busy()">Cancelar</button>
          <button type="submit" class="btn btn-primary" [disabled]="busy() || !value()">
            {{ busy() ? 'Desbloqueando…' : 'Desbloquear' }}
          </button>
        </div>
      </form>
    </div>
  `,
  styles: [`
    .up { position: fixed; inset: 0; z-index: 50; display: grid; place-items: center; padding: 16px;
          background: color-mix(in srgb, var(--color-bg) 70%, transparent); backdrop-filter: blur(6px); }
    .up__card { width: min(420px, 100%); padding: 24px; display: grid; gap: 12px; }
    .up__title { margin: 0; font-size: 18px; color: var(--color-neutral-100); }
    .up__user { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }
    .up__error { margin: 0; }
    .up__cta { justify-content: flex-end; gap: 10px; }
  `]
})
export class UnlockPromptComponent {
  identityService = inject(IdentityService);

  value = signal('');
  busy = signal(false);
  error = signal('');

  private passInput = viewChild<ElementRef<HTMLInputElement>>('pass');

  constructor() {
    // Foco en la contraseña apenas aparece: el usuario llegó acá tocando
    // "Proponer" o similar y lo único que tiene que hacer es tipear.
    effect(() => {
      if (this.identityService.unlockRequested()) {
        setTimeout(() => this.passInput()?.nativeElement.focus());
      }
    });
  }

  async submit() {
    if (!this.value() || this.busy()) return;
    this.busy.set(true);
    try {
      await this.identityService.unlock(this.value());
      this.value.set('');
    } catch (err: any) {
      this.error.set(err?.message || 'No se pudo desbloquear.');
    } finally {
      this.busy.set(false);
    }
  }

  cancel() {
    if (this.busy()) return;
    this.value.set('');
    this.error.set('');
    this.identityService.cancelUnlock();
  }
}
