import { Component, computed, effect, inject, signal, untracked } from '@angular/core';
import { CommonModule } from '@angular/common';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router } from '@angular/router';
import { map } from 'rxjs';
import { ChainComponent } from '../chain/chain.component';
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
 * El registro de leyes, en dos vistas sobre lo mismo.
 *
 * Antes eran dos entradas de la barra — Leyes e Historial — que listaban las
 * mismas leyes con tablas distintas, y no quedaba claro cuál mirar. Ahora es
 * una sola pantalla: **Todas las leyes** cuenta en qué punto de su vida está
 * cada una, y **Historial sellado** muestra el comprobante de lo que ya se
 * decidió. La vista va en la URL (`?vista=historial`) para que se pueda
 * enlazar y para que `/chain`, la ruta vieja, siga llevando al mismo lugar.
 *
 * El diseño no maquetó la lista pero sí dejó decidido cómo se filtra: una
 * fila de etiquetas por estado, no pestañas. La diferencia no es cosmética —
 * las pestañas anteriores sólo cubrían tres de los seis estados, y una ley
 * derogada o descartada no aparecía en ninguna. El filtro es local y no una
 * consulta por estado al backend: son pocas leyes, ya vinieron todas, y así
 * cambiar de filtro es instantáneo.
 */
@Component({
  selector: 'app-laws',
  standalone: true,
  imports: [CommonModule, ChainComponent],
  template: `
    <main class="vc-page">
      <div class="vc-head">
        <div class="vc-head__text">
          <h6 class="vc-kicker">Registro público</h6>
          <h1 class="vc-title">Leyes</h1>
          <p class="vc-lead">
            Todo lo que se propuso en la red: lo que espera su turno, lo que se está
            votando ahora, lo que ya rige y lo que se descartó porque nadie lo
            respaldó. Y de lo que se decidió, el sello que lo vuelve imposible de borrar.
          </p>
        </div>
        <button class="btn btn-secondary head__btn" *ngIf="view() === 'leyes'"
                (click)="loadLaws()">Actualizar</button>
      </div>

      <!-- El selector segmentado de Nocturne: dos vistas hermanas, no destinos
           distintos, así que no son links de la barra sino una sola pieza. -->
      <div class="seg views" role="radiogroup" aria-label="Vista del registro">
        <label class="seg-opt">
          <input type="radio" name="vc-vista" [checked]="view() === 'leyes'" (change)="setView('leyes')">
          Todas las leyes
        </label>
        <label class="seg-opt">
          <input type="radio" name="vc-vista" [checked]="view() === 'historial'" (change)="setView('historial')">
          Historial sellado
        </label>
      </div>

      <app-chain class="views__panel" *ngIf="view() === 'historial'; else registro"></app-chain>

      <ng-template #registro>
        <div class="vc-actions filters">
          <button type="button" class="tag tag-btn" *ngFor="let f of filters"
                  [class.tag-accent]="filter() === f.key"
                  [class.tag-outline]="filter() !== f.key"
                  (click)="filter.set(f.key)">{{ f.label }}</button>
          <span class="mono vc-muted filters__count">{{ visible().length }} de {{ laws().length }}</span>
        </div>

        <div class="vc-table-wrap laws" *ngIf="visible().length; else vacio">
          <table class="table laws__table vc-table--stack">
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
                <td class="mono laws__id" data-label="Ley">{{ l.law_id }}</td>
                <td data-label="Área"><span class="tag tag-outline">{{ label(l.category) }}</span></td>
                <td class="laws__action" data-label="Tipo">{{ actionLabel(l.action) }}</td>
                <td data-label="Estado">
                  <span class="tag" [ngClass]="statusCls(l.status)">{{ statusLabel(l.status) }}</span>
                </td>
                <td class="laws__date" data-label="Propuesta el">{{ date(l.created_at) }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <ng-template #vacio>
          <p class="vc-empty laws__empty">
            {{ laws().length ? 'Ninguna ley en este estado.' : 'Todavía no se propuso ninguna ley.' }}
          </p>
        </ng-template>
      </ng-template>
    </main>
  `,
  styles: [`
    .head__btn { min-height: 38px; font-size: 13px; }
    .views { margin-top: 36px; }
    .views .seg-opt { padding: 8px 16px; }
    .views__panel { display: block; margin-top: 28px; }
    .filters { margin-top: 28px; }
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
  private router = inject(Router);
  private route = inject(ActivatedRoute);

  /** Qué vista se muestra; cualquier valor desconocido cae en la lista. */
  view = toSignal(
    this.route.queryParamMap.pipe(
      map((q) => (q.get('vista') === 'historial' ? 'historial' : 'leyes') as 'leyes' | 'historial'),
    ),
    { initialValue: 'leyes' as const },
  );

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

  /** Cambiar de vista reemplaza la entrada del historial: es la misma pantalla. */
  setView(view: 'leyes' | 'historial') {
    this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { vista: view === 'historial' ? 'historial' : null },
      replaceUrl: true,
    });
  }

  loadLaws() {
    this.api.getLaws().subscribe({
      next: (laws) => this.laws.set(laws),
      error: (err) => console.error('No se pudieron leer las leyes:', err),
    });
  }
}
