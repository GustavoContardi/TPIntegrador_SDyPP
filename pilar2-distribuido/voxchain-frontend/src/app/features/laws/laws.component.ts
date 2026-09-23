import { Component, computed, effect, inject, signal, untracked } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ApiService } from '../../core/services/api.service';
import { EventsService } from '../../core/services/events.service';
import {
  LAW_CATEGORIES, Law, LawCategory, actionLabel, categoryLabel, lawStatusLabel,
} from '../../core/models/law.model';
import { friendlyDate } from '../../core/utils/format';

/** Los estados por los que puede pasar una ley, en el orden de su vida. */
const FILTROS: { key: string; label: string }[] = [
  { key: 'all', label: 'Todas' },
  { key: 'pending_queue', label: 'Esperando turno' },
  { key: 'in_window', label: 'En votación' },
  { key: 'promulgated', label: 'Vigentes' },
  { key: 'repealed', label: 'Derogadas' },
  { key: 'discarded', label: 'Descartadas' },
];

/**
 * Todas las leyes de la red, filtrables por estado.
 *
 * El diseño no maquetó esta pantalla pero sí dejó decidido cómo se filtra: una
 * fila de etiquetas por estado, no pestañas. La diferencia no es cosmética —
 * las pestañas anteriores sólo cubrían tres de los seis estados, y una ley
 * derogada o descartada no aparecía en ninguna. El filtro es local y no una
 * consulta por estado al backend: son pocas leyes, ya vinieron todas, y así
 * cambiar de filtro es instantáneo.
 */
@Component({
  selector: 'app-laws',
  standalone: true,
  imports: [CommonModule],
  template: `
    <main class="vc-page">
      <div class="vc-head">
        <div class="vc-head__text">
          <h6 class="vc-kicker">Registro</h6>
          <h1 class="vc-title">Leyes</h1>
          <p class="vc-lead">
            Todo lo que se propuso en la red: lo que espera su turno, lo que se está
            votando ahora, lo que ya rige y lo que se descartó porque nadie lo
            respaldó.
          </p>
        </div>
        <button class="btn btn-secondary head__btn" (click)="loadLaws()">Actualizar</button>
      </div>

      <div class="vc-actions filters">
        <button type="button" class="tag tag-btn" *ngFor="let f of filters"
                [class.tag-accent]="filter() === f.key"
                [class.tag-outline]="filter() !== f.key"
                (click)="filter.set(f.key)">{{ f.label }}</button>
        <span class="mono vc-muted filters__count">{{ visible().length }} de {{ laws().length }}</span>
      </div>

      <div class="vc-table-wrap laws" *ngIf="visible().length; else vacio">
        <table class="table laws__table">
          <thead>
            <tr>
              <th>Ley</th>
              <th>Área</th>
              <th>Tipo</th>
              <th>Estado</th>
              <th>Propuesta el</th>
            </tr>
          </thead>
          <tbody>
            <tr *ngFor="let l of visible()">
              <td class="mono laws__id">{{ l.law_id }}</td>
              <td><span class="tag tag-outline">{{ label(l.category) }}</span></td>
              <td class="laws__action">{{ actionLabel(l.action) }}</td>
              <td>
                <span class="tag" [ngClass]="statusCls(l.status)">{{ statusLabel(l.status) }}</span>
              </td>
              <td class="laws__date">{{ date(l.created_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <ng-template #vacio>
        <p class="vc-empty laws__empty">
          {{ laws().length ? 'Ninguna ley en este estado.' : 'Todavía no se propuso ninguna ley.' }}
        </p>
      </ng-template>
    </main>
  `,
  styles: [`
    .head__btn { min-height: 38px; font-size: 13px; }
    .filters { margin-top: 36px; }
    .filters__count { margin-left: 6px; font-size: 11.5px; }

    .laws { margin-top: 24px; }
    .laws__table { min-width: 720px; white-space: nowrap; }
    .laws__id { font-size: 13px; color: var(--color-neutral-100); }
    .laws__action { font-size: 13px; color: var(--color-neutral-300); }
    .laws__date { font-size: 12.5px; color: var(--color-neutral-400); }
    .laws__empty { margin-top: 24px; }
  `]
})
export class LawsComponent {
  private api = inject(ApiService);
  private events = inject(EventsService);

  filters = FILTROS;
  filter = signal('all');
  laws = signal<Law[]>([]);
  /** Etiquetas de las áreas; el backend las pisa con la lista autoritativa. */
  categories = signal<LawCategory[]>(LAW_CATEGORIES);

  visible = computed(() => {
    const f = this.filter();
    return f === 'all' ? this.laws() : this.laws().filter((l) => l.status === f);
  });

  constructor() {
    this.api.getLawCategories().subscribe({
      next: (cats) => { if (cats?.length) this.categories.set(cats); },
      error: () => {},  // nos quedamos con las etiquetas locales
    });
    // Se sigue tanto el último bloque como el contador de cambios de ley: una
    // ley puede moverse de estado sin que se selle nada (entra en ventana, se
    // descarta al vencer el plazo), y sólo el segundo signal avisa de eso.
    effect(() => {
      this.events.latestBlock();
      this.events.lawsChanged();
      untracked(() => this.loadLaws());
    });
  }

  label(category: string): string {
    return categoryLabel(category, this.categories());
  }

  actionLabel = actionLabel;
  statusLabel = lawStatusLabel;
  date = friendlyDate;

  /** Vigente o en juego lleva el acento; lo que ya no está en pie, gris. */
  statusCls(status: string): string {
    return status === 'promulgated' || status === 'in_window' ? 'tag-accent' : 'tag-neutral';
  }

  loadLaws() {
    this.api.getLaws().subscribe({
      next: (laws) => this.laws.set(laws),
      error: (err) => console.error('No se pudieron leer las leyes:', err),
    });
  }
}
