import { Component, computed, effect, inject, signal, untracked } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { ApiService } from '../../core/services/api.service';
import { EventsService } from '../../core/services/events.service';
import { Block } from '../../core/models/block.model';
import { Law, actionLabel, categoryLabel } from '../../core/models/law.model';
import { Window } from '../../core/models/window.model';

/**
 * Panel: el estado de la red de un vistazo.
 *
 * El diseño no maquetó esta pantalla, así que está armada con sus mismas
 * piezas: la grilla de métricas de Minería y la ficha de ventana activa de la
 * portada. No inventa nada — es un resumen, y su trabajo es mandarte rápido a
 * la pantalla donde sí se hace algo.
 */
@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [CommonModule, RouterModule],
  template: `
    <main class="vc-page">
      <div class="vc-head__text">
        <h6 class="vc-kicker">Resumen</h6>
        <h1 class="vc-title">Panel</h1>
        <p class="vc-lead">
          Dónde está parada la red ahora mismo: cuánto se selló, cuánto espera turno y
          qué se está minando en este momento.
        </p>
      </div>

      <div class="vc-stats stats">
        <div class="card elev-sm vc-stat">
          <p class="vc-stat__value">{{ blocks().length }}</p>
          <p class="vc-stat__label">bloques sellados</p>
        </div>
        <div class="card elev-sm vc-stat">
          <p class="vc-stat__value">{{ promulgated() }}</p>
          <p class="vc-stat__label">leyes vigentes</p>
        </div>
        <div class="card elev-sm vc-stat">
          <p class="vc-stat__value">{{ pending() }}</p>
          <p class="vc-stat__label">esperando turno</p>
        </div>
        <div class="card elev-sm vc-stat">
          <p class="vc-stat__value" [class.vc-stat__value--accent]="window()">
            {{ window() ? '1' : '0' }}
          </p>
          <p class="vc-stat__label">ventanas abiertas</p>
        </div>
      </div>

      <div class="card elev-sm vc-soft--75 vc-edge live" *ngIf="window() as w; else reposo">
        <div class="vc-live__top">
          <span class="card-kicker">Se está minando ahora</span>
          <span class="tag tag-accent">abierta</span>
        </div>
        <h3 class="mono vc-live__id">{{ w.law_id }}</h3>
        <div class="vc-live__grid">
          <div>
            <p class="vc-label">Ventana</p>
            <p class="vc-kv mono">{{ w.voting_window_id }}</p>
          </div>
          <div>
            <p class="vc-label">Acción</p>
            <p class="vc-kv">{{ actionLabel(w.action) }}</p>
          </div>
          <div>
            <p class="vc-label">Dificultad</p>
            <p class="vc-kv mono">{{ w.n_zeros_required }} ceros</p>
          </div>
          <div>
            <p class="vc-label">Área</p>
            <p class="vc-kv">{{ label(w.category) }}</p>
          </div>
        </div>
        <div class="vc-actions vc-live__cta">
          <a class="btn btn-primary vc-live__btn" routerLink="/queue">Ir a votar</a>
        </div>
      </div>

      <ng-template #reposo>
        <div class="card elev-sm vc-soft--75 live">
          <span class="card-kicker">Red en reposo</span>
          <p class="vc-live__idle">
            No hay ninguna ley en disputa. Se abre una ventana en cuanto haya una
            propuesta con mineros dispuestos a minar su área.
          </p>
          <div class="vc-actions vc-live__cta">
            <a class="btn btn-primary vc-live__btn" routerLink="/propose">Proponer una ley</a>
          </div>
        </div>
      </ng-template>

      <section class="vc-section">
        <h3 class="vc-h3 vc-h3--sm sec__title">Últimos bloques</h3>
        <div class="recent" *ngIf="recent().length; else sinBloques">
          <div class="recent__row" *ngFor="let b of recent()">
            <code class="mono recent__hash" [title]="b.block_hash">{{ b.block_hash.slice(0, 16) }}…</code>
            <span class="mono recent__law">{{ b.law_id }}</span>
            <span class="tag" [class.tag-outline]="b.action === 'derogacion'"
                  [class.tag-accent]="b.action !== 'derogacion'">
              {{ actionLabel(b.action) }}
            </span>
            <span class="vc-note-sm">selló {{ b.winning_node_or_pool }}</span>
            <span class="mono vc-push recent__ts">{{ b.timestamp }}</span>
          </div>
        </div>
        <ng-template #sinBloques>
          <p class="vc-empty">Todavía no se selló ningún bloque.</p>
        </ng-template>
        <div class="vc-actions recent__more" *ngIf="recent().length">
          <a class="btn btn-secondary recent__btn" routerLink="/chain">Ver la cadena completa</a>
        </div>
      </section>
    </main>
  `,
  styles: [`
    .stats { margin-top: 40px; }

    .live { margin-top: 16px; padding: 26px; }

    .sec__title { margin-bottom: 20px; }
    /* Lista y no tarjetas: son cinco filas de lo mismo, y la regla superior
       alcanza para separarlas. */
    .recent { display: flex; flex-direction: column; }
    .recent__row {
      display: flex; flex-wrap: wrap; align-items: center; gap: 14px;
      padding: 15px 0; border-top: 1px solid var(--color-divider);
    }
    .recent__hash { font-size: 12.5px; color: var(--color-neutral-100); }
    .recent__law { font-size: 13px; color: var(--color-neutral-300); }
    .recent__ts { font-size: 12px; color: var(--color-neutral-500); }
    .recent__more { margin-top: 24px; }
    .recent__btn { font-size: 13px; }
  `]
})
export class DashboardComponent {
  private api = inject(ApiService);
  private events = inject(EventsService);

  blocks = signal<Block[]>([]);
  private laws = signal<Law[]>([]);
  private fetchedWindow = signal<Window | null>(null);

  /** La ventana en curso: manda la que llegó por SSE, y si no la del arranque. */
  window = computed(() => this.events.activeWindow() ?? this.fetchedWindow());

  /**
   * Leyes vigentes: las promulgadas que nadie derogó.
   *
   * Se cuenta sobre las leyes y no sobre los bloques a propósito. Contar
   * bloques de promulgación —como hacía antes esta pantalla— suma también las
   * que después fueron derogadas, así que el número sólo podía crecer: decía
   * cuántas veces se promulgó algo, no cuántas leyes rigen.
   */
  promulgated = computed(() => this.laws().filter((l) => l.status === 'promulgated').length);
  pending = computed(() => this.laws().filter((l) => l.status === 'pending_queue').length);

  recent = computed(() => this.blocks().slice(-5).reverse());

  constructor() {
    effect(() => {
      this.events.latestBlock();
      this.events.lawsChanged();
      untracked(() => this.load());
    });
  }

  label(category: string): string {
    return categoryLabel(category);
  }

  actionLabel = actionLabel;

  private load() {
    this.api.getChain().subscribe({
      next: (bs) => this.blocks.set(bs),
      error: () => {},
    });
    this.api.getLaws().subscribe({
      next: (ls) => this.laws.set(ls),
      error: () => {},
    });
    this.api.getActiveWindow().subscribe({
      next: (w) => this.fetchedWindow.set(w),
      error: () => this.fetchedWindow.set(null),
    });
  }
}
