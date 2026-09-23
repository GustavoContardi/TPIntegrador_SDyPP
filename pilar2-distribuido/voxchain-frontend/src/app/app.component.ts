import { Component, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { IdentityService } from './core/services/identity.service';

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
 */
@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule, RouterModule],
  template: `
    <div class="vc-shell">
      <header class="hdr">
        <div class="hdr__inner">
          <a routerLink="/" class="brand" title="Inicio">
            <span class="brand__vox">VOXCHAIN</span><span class="brand__reborn">REBORN</span>
          </a>

          <nav class="nav-links">
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
              <span class="session__dot"></span>
              <span class="session__label">{{ sessionLabel() }}</span>
              <a class="btn btn-ghost session__btn" routerLink="/identity">Mi identidad</a>
              <button class="btn btn-ghost session__out" (click)="logout()" title="Cerrar sesión">×</button>
            </ng-container>
            <ng-template #anon>
              <a class="btn btn-primary session__in" routerLink="/identity">Crear mi identidad</a>
            </ng-template>
          </div>
        </div>
        <div class="vc-rule"></div>
      </header>

      <router-outlet></router-outlet>
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
    .session__label { font-size: 12.5px; white-space: nowrap; color: var(--color-neutral-300); }
    .session__btn { font-size: 12px; color: var(--color-neutral-500); }
    .session__btn:hover { color: var(--color-text); background: transparent; }
    .session__out { font-size: 17px; line-height: 1; color: var(--color-neutral-500); padding: 0 6px; }
    .session__in { font-size: 12.5px; }

    @media (max-width: 1080px) {
      .hdr__inner { flex-wrap: wrap; padding: 12px 20px; row-gap: 12px; }
      .brand { margin-right: 0; }
      .session { margin-left: auto; }
    }
  `]
})
export class AppComponent {
  identityService = inject(IdentityService);

  /**
   * Cómo se te nombra en la barra: tu nombre para mostrar. La clave pública
   * no se muestra — es un dato para la red, no para vos.
   */
  sessionLabel(): string {
    const id = this.identityService.identity();
    if (!id) return '';
    return id.username || 'Tu identidad';
  }

  logout() {
    this.identityService.clearIdentity();
  }
}
