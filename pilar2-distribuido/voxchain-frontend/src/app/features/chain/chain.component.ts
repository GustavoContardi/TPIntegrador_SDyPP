import { Component, computed, effect, inject, signal, untracked } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ApiService } from '../../core/services/api.service';
import { EventsService } from '../../core/services/events.service';
import { Block } from '../../core/models/block.model';
import { actionLabel } from '../../core/models/law.model';
import { friendlyDate } from '../../core/utils/format';

/** Un bloque con lo que la tabla necesita ya calculado. */
interface Fila {
  height: string;
  hash: string;
  seal: string;
  law: string;
  action: string;
  zeros: number;
  winner: string;
  ts: string;
  derogacion: boolean;
}

/**
 * El historial: cada decisión que la red selló, de la más nueva a la más vieja.
 *
 * Es la cadena de bloques, pero contada para quien no sabe qué es un nonce ni
 * un hash previo. De cada bloque se muestra lo que alguien puede querer saber
 * —qué ley, qué se decidió, quién la selló y cuándo— más un sello corto que
 * funciona como comprobante. El detalle criptográfico sigue en la API.
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
          <h6 class="vc-kicker">Registro público</h6>
          <h1 class="vc-title">Historial</h1>
          <p class="vc-lead">
            Cada decisión de la red queda sellada y encadenada a la anterior. Nadie
            puede borrarla ni modificarla: cambiar una sola obligaría a rehacer todo el
            trabajo que vino después.
          </p>
        </div>
        <span class="vc-muted head__len" *ngIf="rows().length">
          {{ rows().length }} {{ rows().length === 1 ? 'decisión sellada' : 'decisiones selladas' }}
        </span>
      </div>

      <div class="vc-table-wrap chain" *ngIf="rows().length; else vacia">
        <table class="table chain__table">
          <thead>
            <tr>
              <th>N.º</th>
              <th>Ley</th>
              <th>Decisión</th>
              <th class="num">Dificultad</th>
              <th>Sellada por</th>
              <th>Fecha</th>
              <th>Sello</th>
            </tr>
          </thead>
          <tbody>
            <tr *ngFor="let b of rows()">
              <td class="mono chain__height">{{ b.height }}</td>
              <td class="mono chain__law">{{ b.law }}</td>
              <td>
                <span class="tag" [class.tag-outline]="b.derogacion" [class.tag-accent]="!b.derogacion">
                  {{ b.action }}
                </span>
              </td>
              <td class="num chain__zeros">nivel {{ b.zeros }}</td>
              <td class="chain__winner">{{ b.winner }}</td>
              <td class="chain__ts">{{ b.ts }}</td>
              <!-- El sello es el comprobante: corto a la vista, entero al pasar el mouse. -->
              <td><code class="chain__seal" [title]="b.hash">{{ b.seal }}</code></td>
            </tr>
          </tbody>
        </table>
      </div>

      <ng-template #vacia>
        <p class="vc-empty chain__empty">
          Todavía no se selló ninguna decisión. La primera ley que la red resuelva va a
          aparecer acá.
        </p>
      </ng-template>
    </main>
  `,
  styles: [`
    .head__len { font-size: 12.5px; white-space: nowrap; }
    .chain { margin-top: 40px; }
    .chain__table { min-width: 820px; white-space: nowrap; }
    .chain__height { font-size: 13px; color: var(--color-neutral-500); }
    .chain__law { font-size: 13px; color: var(--color-neutral-100); }
    .chain__zeros { font-size: 13px; color: var(--color-accent-300); }
    .chain__winner { font-size: 13px; color: var(--color-neutral-300); }
    .chain__ts { font-size: 12.5px; color: var(--color-neutral-400); }
    .table code.chain__seal { font-size: 12px; color: var(--color-neutral-500); }
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
        seal: b.block_hash.slice(0, 10),
        law: b.law_id,
        action: actionLabel(b.action),
        zeros: b.n_zeros_required,
        winner: b.winning_node_or_pool,
        ts: friendlyDate(b.timestamp),
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
