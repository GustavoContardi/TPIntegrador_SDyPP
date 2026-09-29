import { Component, computed, inject, signal } from '@angular/core';
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
 * Cada destino es una pastilla con ícono, todas con el mismo filo suave del
 * acento: con algunas en pastilla y otras en texto llano la barra parecía
 * tener dos estilos. La única distinta es Proponer ley, encendida y con halo,
 * porque es la acción principal de la app; por eso además cierra la fila.
 *
 * El bloque de sesión va separado por una línea vertical: lo que está a su
 * izquierda es la app, lo que está a su derecha sos vos. Por eso tiene forma
 * de ficha de perfil —inicial, nombre y flecha— y no de link suelto.
 *
 * En pantallas angostas nada de eso entra en una fila, y envuelto en tres
 * ocupaba un cuarto del celular pegado arriba. Ahí la barra queda en marca +
 * botón de menú, y el menú se despliega debajo, dentro de la misma barra.
 *
 * Los estilos viven en `styles/_shell.scss` y no acá: con las pastillas y la
 * ficha, esta hoja pasaba el presupuesto de 4 kB por componente.
 */
@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule, RouterModule, UnlockPromptComponent],
  template: `
    <div class="vc-shell">
      <header class="hdr" [class.hdr--open]="menuOpen()">
        <div class="hdr__inner" (click)="closeMenuOnLink($event)">
          <a routerLink="/" class="hdr__brand" title="Inicio">
            <span class="hdr__vox">VOXCHAIN</span><span class="hdr__reborn">REBORN</span>
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

          <nav class="hdr__nav" id="vc-menu">
            <a class="hdr__pill hdr__pill--soft" routerLink="/dashboard" routerLinkActive="on"
               [ariaCurrentWhenActive]="'page'">
              <!-- Cuatro cuadros: el tablero de un vistazo. -->
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <rect x="4" y="4" width="7" height="7" rx="1.5"></rect><rect x="13" y="4" width="7" height="7" rx="1.5"></rect>
                <rect x="4" y="13" width="7" height="7" rx="1.5"></rect><rect x="13" y="13" width="7" height="7" rx="1.5"></rect>
              </svg>
              Panel
            </a>
            <a class="hdr__pill hdr__pill--soft" routerLink="/laws" routerLinkActive="on"
               [ariaCurrentWhenActive]="'page'">
              <!-- Una hoja con renglones: el texto de la ley. -->
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M7 3h7l4 4v14H7z"></path><path d="M14 3v4h4M10 12h5M10 16h5"></path>
              </svg>
              Leyes
            </a>
            <a class="hdr__pill hdr__pill--soft" routerLink="/health" routerLinkActive="on"
               [ariaCurrentWhenActive]="'page'">
              <!-- El mismo latido que dibuja la pantalla de Estado. -->
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h4l2-5 4 10 2-5h6"></path></svg>
              Estado
            </a>
            <a class="hdr__pill hdr__pill--soft" routerLink="/workers" routerLinkActive="on"
               [ariaCurrentWhenActive]="'page'" title="Poné tu computadora a minar">
              <!-- Un chip: lo que ponés es cómputo. -->
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <rect x="7" y="7" width="10" height="10" rx="1.5"></rect>
                <path d="M10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4"></path>
              </svg>
              Minería
            </a>
            <a class="hdr__pill hdr__pill--soft" routerLink="/queue" routerLinkActive="on"
               [ariaCurrentWhenActive]="'page'" *ngIf="identityService.identity()"
               title="Decidí si tu cómputo respalda la ley en juego">
              <!-- Una tilde en un recuadro: la boleta marcada. -->
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <rect x="4" y="4" width="16" height="16" rx="3"></rect>
                <path d="M8.5 12.5l2.5 2.5 4.5-5"></path>
              </svg>
              Votar
            </a>
            <a class="hdr__pill hdr__pill--propose" routerLink="/propose" routerLinkActive="on"
               [ariaCurrentWhenActive]="'page'" *ngIf="identityService.identity()">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"></path></svg>
              Proponer ley
            </a>
          </nav>

          <div class="hdr__session">
            <ng-container *ngIf="identityService.identity() as id; else anon">
              <!-- No hay botón de "cerrar sesión": no hay sesión que cerrar. La
                   identidad es la clave guardada en este navegador, y el único
                   "salir" posible es borrarla — irreversible sin el respaldo —,
                   así que vive en /identity, detrás de una confirmación. Antes
                   había acá una × que la borraba de un clic. -->
              <a class="hdr__me" routerLink="/identity" routerLinkActive="on"
                 [ariaCurrentWhenActive]="'page'" title="Ver y administrar tu identidad">
                <span class="hdr__avatar" aria-hidden="true">
                  {{ initial() }}
                  <!-- El punto late mientras la clave esté a mano: es el mismo
                       semáforo que usan los mineros, acá aplicado a vos. -->
                  <span class="hdr__dot" [class.hdr__dot--off]="identityService.locked()"></span>
                </span>
                <span class="hdr__who">
                  <span class="hdr__name">{{ sessionLabel() }}</span>
                  <span class="hdr__sub">{{ identityService.locked() ? 'Bloqueada' : 'Mi identidad' }}</span>
                </span>
                <svg class="hdr__chev" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6"></path></svg>
              </a>
              <!-- Bloquear sí es lo que uno espera de "salir": olvida la clave de
                   memoria sin borrar nada, y para volver alcanza la contraseña. -->
              <ng-container *ngIf="identityService.protection() === 'vault'">
                <button class="btn btn-ghost hdr__lock" *ngIf="!identityService.locked()"
                        (click)="identityService.lock()" title="Olvidar la clave hasta que vuelvas a escribir tu contraseña">Bloquear</button>
                <button class="btn btn-ghost hdr__lock" *ngIf="identityService.locked()"
                        (click)="unlock()">Desbloquear</button>
              </ng-container>
            </ng-container>
            <ng-template #anon>
              <a class="btn btn-primary hdr__signin" routerLink="/identity">Crear mi identidad</a>
            </ng-template>
          </div>
        </div>
        <div class="vc-rule"></div>
      </header>

      <router-outlet></router-outlet>
      <app-unlock-prompt></app-unlock-prompt>
    </div>
  `,
  styles: [`:host { display: block; }`]
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

  /** La inicial del nombre hace de avatar; sin nombre, una marca neutra. */
  initial = computed(() => {
    const name = this.identityService.identity()?.username?.trim();
    return name ? [...name][0].toLocaleUpperCase('es') : '·';
  });

  unlock() {
    // Cancelar es una respuesta válida: la identidad sigue bloqueada y listo.
    this.identityService.requestUnlock().catch(() => {});
  }
}
