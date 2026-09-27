import { Component, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { IdentityService } from './core/services/identity.service';
import { UnlockPromptComponent } from './core/components/unlock-prompt.component';

/**
 * El caparazón: barra superior y salida del router.
 *
 * La barra es pegajosa y translúcida (`backdrop-filter`), así que el contenido
 * pasa por detrás desenfocado en vez de empujarla. La cierra abajo una regla
 * que se desvanece en los extremos — la firma de Nocturne — y no un borde
 * lleno, que cortaría en seco contra el degradado de la página.
 *
 * El bloque de sesión de la derecha va separado por una línea vertical: lo que
 * está a su izquierda es la app, lo que está a su derecha sos vos.
 *
 * En pantallas angostas los links y la sesión no entran en una fila, y
 * envueltos en tres ocupaban un cuarto del celular pegado arriba. Ahí la barra
 * queda en marca + botón de menú, y el menú se despliega debajo, dentro de la
 * misma barra: links en columna y la sesión al pie, separada por una regla en
 * vez de la línea vertical.
 */
@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule, RouterModule, UnlockPromptComponent],
  template: `
    <div class="vc-shell">
      <header class="hdr" [class.hdr--open]="menuOpen()">
        <div class="hdr__inner" (click)="closeMenuOnLink($event)">
          <a routerLink="/" class="brand" title="Inicio">
            <span class="brand__vox">VOXCHAIN</span><span class="brand__reborn">REBORN</span>
          </a>

          <button type="button" class="btn btn-secondary btn-icon hdr__menu"
                  [attr.aria-expanded]="menuOpen()" aria-controls="vc-menu"
                  [attr.aria-label]="menuOpen() ? 'Cerrar menú' : 'Abrir menú'"
                  (click)="menuOpen.set(!menuOpen())">
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"
                 stroke-width="1.8" stroke-linecap="round" aria-hidden="true">
              <path *ngIf="!menuOpen()" d="M4 7h16M4 12h16M4 17h16"></path>
              <path *ngIf="menuOpen()" d="M6 6l12 12M18 6L6 18"></path>
            </svg>
          </button>

          <nav class="nav-links" id="vc-menu">
            <a routerLink="/dashboard" routerLinkActive="on" [ariaCurrentWhenActive]="'page'">Panel</a>
            <a routerLink="/chain" routerLinkActive="on" [ariaCurrentWhenActive]="'page'">Historial</a>
            <a routerLink="/laws" routerLinkActive="on" [ariaCurrentWhenActive]="'page'">Leyes</a>
            <a routerLink="/queue" routerLinkActive="on" [ariaCurrentWhenActive]="'page'"
               *ngIf="identityService.identity()">Votar</a>
            <a routerLink="/workers" routerLinkActive="on" [ariaCurrentWhenActive]="'page'">Minería</a>
            <a routerLink="/health" routerLinkActive="on" [ariaCurrentWhenActive]="'page'">Estado</a>
            <a routerLink="/propose" routerLinkActive="on" [ariaCurrentWhenActive]="'page'"
               *ngIf="identityService.identity()">Proponer ley</a>
          </nav>

          <div class="session">
            <ng-container *ngIf="identityService.identity() as id; else anon">
              <!-- El punto late mientras haya sesión: es el mismo semáforo que
                   usan los mineros, acá aplicado a tu propia identidad. -->
              <span class="session__dot" [class.session__dot--off]="identityService.locked()"></span>
              <span class="session__label">{{ sessionLabel() }}</span>
              <!-- No hay botón de "cerrar sesión": no hay sesión que cerrar. La
                   identidad es la clave guardada en este navegador, y el único
                   "salir" posible es borrarla — irreversible sin el respaldo —,
                   así que vive en /identity, detrás de una confirmación. Antes
                   había acá una × que la borraba de un clic. -->
              <a class="btn btn-ghost session__btn" routerLink="/identity">Mi identidad</a>
              <!-- Bloquear sí es lo que uno espera de "salir": olvida la clave de
                   memoria sin borrar nada, y para volver alcanza la contraseña. -->
              <ng-container *ngIf="identityService.protection() === 'vault'">
                <button class="btn btn-ghost session__btn" *ngIf="!identityService.locked()"
                        (click)="identityService.lock()" title="Olvidar la clave hasta que vuelvas a escribir tu contraseña">Bloquear</button>
                <button class="btn btn-ghost session__btn" *ngIf="identityService.locked()"
                        (click)="unlock()">Desbloquear</button>
              </ng-container>
            </ng-container>
            <ng-template #anon>
              <a class="btn btn-primary session__in" routerLink="/identity">Crear mi identidad</a>
            </ng-template>
          </div>
        </div>
        <div class="vc-rule"></div>
      </header>

      <router-outlet></router-outlet>
      <app-unlock-prompt></app-unlock-prompt>
    </div>
  `,
  styles: [`
    :host { display: block; }

    .hdr {
      position: sticky; top: 0; z-index: 20;
      backdrop-filter: blur(14px);
      background: color-mix(in srgb, var(--color-bg) 82%, transparent);
    }
    .hdr__inner {
      max-width: 1240px; margin: 0 auto;
      display: flex; align-items: center; gap: 20px; padding: 14px 32px;
    }

    .brand {
      display: inline-flex; align-items: baseline; gap: .34em;
      font-family: var(--font-heading); font-size: 17px; letter-spacing: .04em;
      color: var(--color-text); margin-right: auto; white-space: nowrap;
    }
    .brand:hover { color: var(--color-text); }
    .brand__vox { font-weight: 600; }
    .brand__reborn { font-weight: 300; color: var(--color-accent); }

    .nav-links { display: flex; align-items: center; gap: 15px; flex-wrap: wrap; }
    .nav-links a {
      font-size: 13.5px; white-space: nowrap; color: var(--color-neutral-400);
    }
    .nav-links a:hover { color: var(--color-text); }
    /* El acento marca dónde estás: es la única forma en que este sistema
       señala el presente, sin subrayado ni pastilla de fondo. */
    .nav-links a.on { color: var(--color-accent); }

    .session {
      display: flex; align-items: center; gap: 10px;
      padding-left: 16px; border-left: 1px solid var(--color-divider);
    }
    .session__dot {
      flex: none; width: 7px; height: 7px; border-radius: 50%;
      background: var(--color-accent); box-shadow: 0 0 10px var(--color-accent);
      animation: vc-pulse 1.8s ease-in-out infinite;
    }
    .session__dot--off { animation: none; background: var(--color-neutral-500); box-shadow: none; }
    .session__label { font-size: 12.5px; white-space: nowrap; color: var(--color-neutral-300); }
    .session__btn { font-size: 12px; color: var(--color-neutral-500); }
    .session__btn:hover { color: var(--color-text); background: transparent; }
    .session__in { font-size: 12.5px; }

    .hdr__menu { display: none; color: var(--color-neutral-300); }

    @media (max-width: 1080px) {
      .hdr__inner { flex-wrap: wrap; padding: 12px 20px; row-gap: 12px; }
      .brand { margin-right: 0; }
      .session { margin-left: auto; }
    }

    @media (max-width: 960px) {
      .hdr__inner { gap: 0; }
      .brand { margin-right: auto; }
      .hdr__menu { display: inline-flex; }
      .nav-links, .session { display: none; flex-basis: 100%; }
      .hdr--open .hdr__inner { max-height: 100vh; overflow-y: auto; }
      .hdr--open .nav-links {
        display: flex; flex-direction: column; align-items: stretch; gap: 0; margin-top: 8px;
      }
      .nav-links a { padding: 12px 2px; font-size: 15px; border-top: 1px solid var(--color-divider); }
      .hdr--open .session {
        display: flex; flex-wrap: wrap; margin: 0; padding: 14px 2px 6px;
        border-left: 0; border-top: 1px solid var(--color-divider);
      }
      .session__label { margin-right: auto; }
      .session__in { flex: 1; min-height: 40px; }
    }
  `]
})
export class AppComponent {
  identityService = inject(IdentityService);

  /** Sólo cuenta en pantallas angostas: en las anchas el botón no se ve. */
  menuOpen = signal(false);

  /**
   * Elegir un destino cierra el menú: si no, la página nueva carga tapada. Va
   * por el clic y no por el router porque tocar el link de la página en la que
   * ya estás no navega, y el menú igual tiene que cerrarse.
   */
  closeMenuOnLink(event: MouseEvent) {
    if ((event.target as HTMLElement).closest('a')) this.menuOpen.set(false);
  }

  /**
   * Cómo se te nombra en la barra: tu nombre para mostrar. La clave pública
   * no se muestra — es un dato para la red, no para vos.
   */
  sessionLabel(): string {
    const id = this.identityService.identity();
    if (!id) return '';
    return id.username || 'Tu identidad';
  }

  unlock() {
    // Cancelar es una respuesta válida: la identidad sigue bloqueada y listo.
    this.identityService.requestUnlock().catch(() => {});
  }
}
