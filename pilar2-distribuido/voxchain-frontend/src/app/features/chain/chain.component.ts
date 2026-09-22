import { Component, computed, effect, inject, signal, untracked } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ApiService } from '../../core/services/api.service';
import { EventsService } from '../../core/services/events.service';
import { Block } from '../../core/models/block.model';
import { actionLabel } from '../../core/models/law.model';

/** Un bloque con lo que la tabla necesita ya calculado. */
interface Fila {
  height: string;
  hash: string;
  hashShort: string;
  prev: string;
  law: string;
  action: string;
  zeros: number;
  nonce: string;
  winner: string;
  window: string;
  ts: string;
  derogacion: boolean;
}

/**
 * La cadena de bloques.
 *
 * El diseño no llegó a maquetar esta pantalla, pero sí dejó dicho qué se
 * muestra de un bloque: altura, hash, ley, acción, ceros, nonce, ganador,
 * ventana y fecha. Está armada con las mismas piezas del sistema que el resto
 * — tabla con la regla que se desvanece, monoespaciada para todo lo que
 * calculó la red, el acento sólo en la etiqueta de acción.
 *
 * La altura se cuenta desde el final: el bloque más nuevo va primero porque es
 * el que interesa, y su número es el largo de la cadena.
 */
@Component({
  selector: 'app-chain',
  standalone: true,
  imports: [CommonModule],
  template: `
    <main class="vc-page">
      <div class="vc-head">
        <div class="vc-head__text">
          <h6 class="vc-kicker">Registro</h6>
          <h1 class="vc-title">Cadena de bloques</h1>
          <p class="vc-lead">
            Cada bloque sella una ley con el nonce que alguien encontró y queda
            encadenado al anterior por su hash. Reescribir uno obliga a rehacer todo el
            trabajo de los que vinieron después.
          </p>
        </div>
        <span class="mono vc-muted head__len" *ngIf="rows().length">
          {{ rows().length }} bloque{{ rows().length === 1 ? '' : 's' }}
        </span>
      </div>

      <div class="vc-table-wrap chain" *ngIf="rows().length; else vacia">
        <table class="table chain__table">
          <thead>
            <tr>
              <th>Altura</th>
              <th>Hash del bloque</th>
              <th>Ley</th>
              <th>Acción</th>
              <th class="num">Ceros</th>
              <th class="num">Nonce</th>
              <th>Ganador</th>
              <th>Ventana</th>
              <th>Fecha</th>
            </tr>
          </thead>
          <tbody>
            <tr *ngFor="let b of rows()">
              <td class="mono chain__height">{{ b.height }}</td>
              <td>
                <code [title]="b.hash">{{ b.hashShort }}</code>
                <!-- El anterior va debajo y apagado: es el eslabón, no el dato
                     que se busca, pero sin él la cadena no se ve encadenada. -->
                <span class="mono chain__prev">← {{ b.prev }}</span>
              </td>
              <td class="mono chain__law">{{ b.law }}</td>
              <td>
                <span class="tag" [class.tag-outline]="b.derogacion" [class.tag-accent]="!b.derogacion">
                  {{ b.action }}
                </span>
              </td>
              <td class="mono num chain__zeros">{{ b.zeros }}</td>
              <td class="mono num">{{ b.nonce }}</td>
              <td class="chain__winner">{{ b.winner }}</td>
              <td class="mono chain__window">{{ b.window }}</td>
              <td class="mono chain__ts">{{ b.ts }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <ng-template #vacia>
        <p class="vc-empty chain__empty">
          La cadena está vacía: todavía no se selló ningún bloque.
        </p>
      </ng-template>
    </main>
  `,
  styles: [`
    .head__len { font-size: 11.5px; white-space: nowrap; }
    .chain { margin-top: 40px; }
    .chain__table { min-width: 1120px; white-space: nowrap; }
    .chain__height { font-size: 13px; color: var(--color-neutral-500); }
    .chain__prev { display: block; margin-top: 4px; font-size: 11px; color: var(--color-neutral-700); }
    .chain__law { font-size: 13px; color: var(--color-neutral-200); }
    /* Los ceros son la dificultad: el dato que explica cuánto costó el bloque. */
    .chain__zeros { font-size: 14px; color: var(--color-accent-300); }
    .chain__winner { font-size: 13px; color: var(--color-neutral-300); }
    .chain__window { font-size: 12.5px; color: var(--color-neutral-500); }
    .chain__ts { font-size: 12px; color: var(--color-neutral-500); }
    .chain__empty { margin-top: 40px; }
  `]
})
export class ChainComponent {
  private api = inject(ApiService);
  private events = inject(EventsService);

  private blocks = signal<Block[]>([]);

  /** Los bloques del más nuevo al más viejo, con lo derivado ya resuelto. */
  rows = computed<Fila[]>(() => {
    const bs = this.blocks();
    return bs
      .map((b, i) => ({
        // `i` es el índice en el orden original (viejo → nuevo), así que el
        // último bloque queda con la altura más alta.
        height: '#' + (i + 1),
        hash: b.block_hash,
        hashShort: b.block_hash.slice(0, 16) + '…',
        prev: b.previous_hash ? b.previous_hash.slice(0, 12) + '…' : 'génesis',
        law: b.law_id,
        action: actionLabel(b.action),
        zeros: b.n_zeros_required,
        nonce: b.nonce.toLocaleString('es-AR'),
        winner: b.winning_node_or_pool,
        window: b.voting_window_id,
        ts: b.timestamp,
        derogacion: b.action === 'derogacion',
      }))
      .reverse();
  });

  constructor() {
    // Sin polling: la cadena sólo crece cuando se sella un bloque, y eso llega
    // por SSE. El efecto se suscribe al signal del último bloque y recarga; el
    // `untracked` evita que las escrituras de `load` lo vuelvan a disparar.
    effect(() => {
      this.events.latestBlock();
      untracked(() => this.load());
    });
  }

  private load() {
    this.api.getChain().subscribe({
      next: (blocks) => this.blocks.set(blocks),
      error: (err) => console.error('No se pudo leer la cadena:', err),
    });
  }
}
