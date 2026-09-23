import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { ApiService } from '../../core/services/api.service';
import { IdentityService } from '../../core/services/identity.service';
import { EventsService } from '../../core/services/events.service';
import {
  LAW_CATEGORIES, Law, LawCategory, actionLabel, categoryLabel, lawStatusLabel,
} from '../../core/models/law.model';
import { friendlyDate } from '../../core/utils/format';
import { SystemAvailability } from '../../core/models/system.model';
import { Window } from '../../core/models/window.model';
import { DeliberationPanelComponent } from '../deliberation/deliberation-panel.component';

@Component({
  selector: 'app-queue',
  standalone: true,
  imports: [CommonModule, RouterModule, DeliberationPanelComponent],
  template: `
    <main class="vc-page vc-page--narrow">
      <div class="vc-head__text">
        <h6 class="vc-kicker">Participación</h6>
        <h1 class="vc-title">Votar</h1>
        <p class="vc-lead">
          Las leyes se votan de a una. Antes de cada votación, quienes tienen mineros
          deciden si ponen su poder de cómputo a favor. Acá ves qué se vota ahora, qué
          viene después y cómo terminó lo anterior.
        </p>
      </div>

      <p class="vc-note vc-note--warn" *ngIf="!identityService.identity()">
        <strong>Necesitás una identidad para participar.</strong>&ngsp;
        <span>Podés mirar todo igual; para decidir, <a routerLink="/identity">creá tu identidad</a>
          en segundos.</span>
      </p>

      <p class="vc-note vc-note--warn" *ngIf="unavailable() as av">
        <strong>Las votaciones están en pausa.</strong>&ngsp;
        <span>{{ av.message }}</span>&ngsp;
        <span *ngIf="av.since">En pausa desde las {{ av.since | date:'HH:mm' }}.</span>
      </p>

      <!-- Antes de su ventana, cada ley pasa por una pausa en la que los
           convocados deciden si aportan cómputo (AGENT.md 3.12). -->
      <app-deliberation-panel></app-deliberation-panel>

      <!-- La ficha de arriba es siempre la misma pieza: si hay ventana abierta
           muestra la ley que se está votando; si no, la que está a la cabeza
           esperando turno. Son dos estados de lo mismo, no dos tarjetas. -->
      <div class="card elev-sm vc-soft--75 vc-edge hero" *ngIf="window() as w; else esperando">
        <div class="vc-live__top">
          <span class="card-kicker">Votación en curso</span>
          <span class="tag tag-accent">abierta</span>
        </div>
        <h3 class="mono vc-live__id">{{ w.law_id }}</h3>
        <p class="vc-live__text" *ngIf="texts()[w.law_id] as t">{{ t }}</p>

        <div class="vc-live__grid">
          <div>
            <p class="vc-label">Se vota</p>
            <p class="vc-kv">{{ actionLabel(w.action) }}</p>
          </div>
          <div>
            <p class="vc-label">Dificultad</p>
            <p class="vc-kv">nivel {{ w.n_zeros_required }}</p>
          </div>
          <div>
            <p class="vc-label">Cierra</p>
            <p class="vc-kv vc-kv--accent">{{ w.deadline | date:'HH:mm' }} h</p>
          </div>
        </div>

        <div class="vc-actions hero__area">
          <span class="tag tag-outline">{{ label(w.category) }}</span>
          <span class="vc-note-sm">la respaldan los equipos que eligieron esta área</span>
        </div>

        <div class="vc-actions vc-live__cta">
          <button class="btn btn-secondary vc-live__btn" (click)="toggleText(w.law_id)">
            {{ texts()[w.law_id] ? 'Ocultar la ley' : 'Leer la ley completa' }}
          </button>
        </div>
      </div>

      <ng-template #esperando>
        <div class="card elev-sm vc-soft--75 hero" *ngIf="nextLaw() as next; else reposo">
          <div class="vc-live__top">
            <span class="card-kicker">La próxima en votarse</span>
            <span class="tag tag-neutral">{{ statusLabel(next.status) }}</span>
          </div>
          <h3 class="mono vc-live__id">{{ next.law_id }}</h3>
          <p class="vc-live__text" *ngIf="texts()[next.law_id] as t">{{ t }}</p>
          <div class="vc-live__grid">
            <div>
              <p class="vc-label">Se vota</p>
              <p class="vc-kv">{{ actionLabel(next.action) }}</p>
            </div>
            <div>
              <p class="vc-label">Propuesta el</p>
              <p class="vc-kv">{{ date(next.created_at) }}</p>
            </div>
          </div>
          <div class="vc-actions hero__area">
            <span class="tag tag-outline">{{ label(next.category) }}</span>
          </div>
          <div class="vc-actions vc-live__cta">
            <button class="btn btn-secondary vc-live__btn" (click)="toggleText(next.law_id)">
              {{ texts()[next.law_id] ? 'Ocultar la ley' : 'Leer la ley completa' }}
            </button>
          </div>
        </div>
        <ng-template #reposo>
          <div class="card elev-sm vc-soft--75 hero">
            <span class="card-kicker">Sin votaciones en curso</span>
            <p class="vc-live__idle">
              No hay ninguna ley en juego. La próxima votación arranca en cuanto alguien
              proponga una ley y haya mineros dispuestos a respaldarla.
            </p>
            <div class="vc-actions vc-live__cta" *ngIf="identityService.identity()">
              <a class="btn btn-primary vc-live__btn" routerLink="/propose">Proponer una ley</a>
            </div>
          </div>
        </ng-template>
      </ng-template>

      <section class="vc-section">
        <h3 class="vc-h3 vc-h3--sm sec__title">Esperando su turno</h3>
        <div class="vc-stack" *ngIf="queue().length; else colaVacia">
          <div class="card elev-sm vc-plain item" *ngFor="let law of queue(); let i = index">
            <div class="vc-row">
              <span class="mono item__pos">#{{ i + 1 }}</span>
              <span class="mono item__id">{{ law.law_id }}</span>
              <span class="tag tag-outline">{{ label(law.category) }}</span>
              <span class="vc-note-sm">{{ actionLabel(law.action) }} · {{ date(law.created_at) }}</span>
              <button class="btn btn-ghost vc-push item__toggle" (click)="toggleText(law.law_id)">
                {{ texts()[law.law_id] ? 'Ocultar texto' : 'Ver texto' }}
              </button>
            </div>
            <p class="item__text" *ngIf="texts()[law.law_id] as t">{{ t }}</p>
          </div>
        </div>
        <ng-template #colaVacia>
          <p class="vc-empty">No hay leyes esperando turno.</p>
        </ng-template>
      </section>

      <section class="vc-section">
        <h3 class="vc-h3 vc-h3--sm sec__title">Ya resueltas</h3>
        <div class="hist" *ngIf="history().length; else sinHistoria">
          <div class="hist__row" *ngFor="let law of history()">
            <span class="mono hist__id">{{ law.law_id }}</span>
            <span class="tag tag-outline">{{ label(law.category) }}</span>
            <span class="vc-note-sm">{{ actionLabel(law.action) }} · {{ date(law.created_at) }}</span>
            <span class="tag vc-push"
                  [class.tag-accent]="law.status === 'promulgated'"
                  [class.tag-neutral]="law.status !== 'promulgated'">{{ statusLabel(law.status) }}</span>
          </div>
        </div>
        <ng-template #sinHistoria>
          <p class="vc-empty">Todavía no se resolvió ninguna ley.</p>
        </ng-template>
      </section>
    </main>
  `,
  styles: [`
    .hero { margin-top: 40px; padding: 26px; }
    .hero__area { margin-top: 22px; }

    .sec__title { margin-bottom: 20px; }

    .item { padding: 20px 22px; }
    .item__pos { font-size: 18px; color: var(--color-accent-300); }
    .item__id { font-size: 14px; color: var(--color-neutral-100); }
    .item__toggle { font-size: 12.5px; }
    .item__text {
      margin: 16px 0 0; padding: 14px 16px; border-radius: var(--radius-md);
      background: var(--color-neutral-900); font-size: 13.5px; line-height: 1.7;
      white-space: pre-wrap; color: var(--color-neutral-200);
    }

    /* El historial no son tarjetas: es una lista, y la regla superior de cada
       fila alcanza para separarlas. */
    .hist { display: flex; flex-direction: column; }
    .hist__row {
      display: flex; flex-wrap: wrap; align-items: center; gap: 14px;
      padding: 15px 0; border-top: 1px solid var(--color-divider);
    }
    .hist__id { font-size: 13.5px; color: var(--color-neutral-100); }
  `]
})
export class QueueComponent implements OnInit {
  private api = inject(ApiService);
  identityService = inject(IdentityService);
  eventsService = inject(EventsService);

  queue = signal<Law[]>([]);
  nextLaw = signal<Law | null>(null);
  private fetchedWindow = signal<Window | null>(null);
  history = signal<Law[]>([]);
  /** Textos de ley ya pedidos, por id. Su presencia es además el "está abierto". */
  texts = signal<Record<string, string>>({});
  /** Etiquetas de las áreas; el backend las pisa con la lista autoritativa. */
  categories = signal<LawCategory[]>(LAW_CATEGORIES);
  /** Estado del sistema: si no hay mineros, la cola no avanza y hay que decirlo. */
  availability = signal<SystemAvailability | null>(null);

  /** La ventana en curso: manda la que llegó por SSE, y si no la del arranque. */
  window = computed(() => this.eventsService.activeWindow() ?? this.fetchedWindow());

  ngOnInit() {
    this.loadData();
    this.api.getLawCategories().subscribe({
      next: (cats) => { if (cats?.length) this.categories.set(cats); },
      error: () => {},  // nos quedamos con las etiquetas locales
    });
  }

  label(category: string): string {
    return categoryLabel(category, this.categories());
  }

  actionLabel = actionLabel;

  statusLabel = lawStatusLabel;
  date = friendlyDate;

  /**
   * El aviso de sistema caído, o null si está operativo.
   *
   * Sin esto, una cola que no avanza porque no hay mineros se ve idéntica a un
   * sistema colgado: mismas leyes, misma pantalla, ninguna explicación.
   */
  unavailable(): SystemAvailability | null {
    const av = this.availability();
    return av && !av.available ? av : null;
  }

  private loadData() {
    this.api.getLawQueue().subscribe({
      next: (laws) => this.queue.set(laws),
      error: () => {},
    });
    this.api.getNextLaw().subscribe({
      next: (law: Law | null) => {
        this.nextLaw.set(law);
        // Se pregunta por el área y la acción de ESA ley: es la que está a la
        // cabeza, la que está trabada, y por lo tanto la que explica por qué la
        // cola no avanza. Encadenado y no en paralelo porque sin la ley no
        // sabemos qué mirar — y una derogación puede estar vetada donde la
        // promulgación de la misma área no lo está.
        this.api.getSystemAvailability(law?.category, law?.action,
                                       law?.law_id).subscribe({
          next: (av) => this.availability.set(av),
          // Sin respuesta no se afirma nada: anunciar "sistema caído" porque no se
          // pudo consultar sería peor que no mostrar el aviso.
          error: () => this.availability.set(null),
        });
      },
      error: () => {},
    });
    this.api.getActiveWindow().subscribe({
      next: (w) => this.fetchedWindow.set(w),
      error: () => this.fetchedWindow.set(null),
    });
    this.api.getLaws('promulgated').subscribe({
      next: (laws) => this.history.set(laws),
      error: () => {},
    });
  }

  toggleText(lawId: string) {
    const current = this.texts();
    if (current[lawId]) {
      const { [lawId]: _drop, ...rest } = current;
      this.texts.set(rest);
      return;
    }
    this.api.getLawText(lawId).subscribe({
      next: (text) => this.texts.set({ ...this.texts(), [lawId]: text }),
      error: () => console.error('No se pudo leer el texto de', lawId),
    });
  }
}
