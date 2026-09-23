import { Component, OnDestroy, OnInit, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ApiService } from '../../core/services/api.service';
import { IdentityService } from '../../core/services/identity.service';
import { Deliberation, DeliberationDecision, DeliberationVoter } from '../../core/models/deliberation.model';
import { actionLabel, categoryLabel } from '../../core/models/law.model';
import { friendlyDuration, friendlyError } from '../../core/utils/format';

/**
 * La ley anunciada y la pausa para decidir (AGENT.md 3.12).
 *
 * Antes de abrir la ventana de una ley, el NCT la anuncia y congela su
 * dificultad sobre el convocado más grande del área. Durante la pausa cada
 * convocado —el fundador por su equipo, el dueño por su standalone— decide si
 * aporta cómputo. Quien no responde, no mina. Si nadie acepta y alguien vetó,
 * la ley se descarta; si nadie respondió, vuelve a la cola.
 *
 * Se pide al backend cada pocos segundos mientras la pantalla está abierta: la
 * pausa dura minutos y las respuestas de los demás tienen que verse llegar.
 */
@Component({
  selector: 'app-deliberation-panel',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="card elev-sm vc-soft--75 vc-edge dp" *ngIf="deliberation() as d">
      <div class="vc-live__top">
        <span class="card-kicker">Se decide quién la respalda</span>
        <span class="tag tag-accent mono" title="Tiempo para decidir">{{ countdown() }}</span>
      </div>
      <h3 class="mono vc-live__id">{{ d.law_id }}</h3>
      <p class="vc-note-sm dp__lead">
        Antes de votar, cada equipo y cada minero independiente convocado decide si
        pone su poder de cómputo a favor de esta ley. Quien no responde, no participa.
        Si el más grande se baja, el resto tiene que alcanzar sin él.
      </p>

      <div class="vc-live__grid">
        <div><p class="vc-label">Área</p><p class="vc-kv">{{ label(d.category) }}</p></div>
        <div><p class="vc-label">Se vota</p><p class="vc-kv">{{ actionLabel(d.action) }}</p></div>
        <div><p class="vc-label">Dificultad</p><p class="vc-kv">nivel {{ d.n_zeros_required }}</p></div>
        <div><p class="vc-label">Duración de la votación</p><p class="vc-kv">{{ duration(d.window_seconds) }}</p></div>
      </div>

      <ul class="vc-roster" *ngIf="d.voters.length; else nadie">
        <li *ngFor="let v of d.voters" [class.coord]="v.biggest">
          <code>{{ v.name }}</code>
          <span class="vc-note-sm">{{ kindLabel(v) }} · {{ share(d, v) }}% del poder</span>
          <span class="tag tag-outline" *ngIf="v.biggest">el más grande</span>
          <span class="tag vc-push" [ngClass]="tagOf(v)">{{ decisionLabel(v) }}</span>
          <ng-container *ngIf="mine(v)">
            <button class="btn btn-primary dp__btn" [disabled]="busy()"
                    (click)="decide(d, v, 'accept')">La respaldo</button>
            <button class="btn btn-secondary dp__btn" [disabled]="busy()"
                    (click)="decide(d, v, 'reject')">No la respaldo</button>
          </ng-container>
        </li>
      </ul>
      <ng-template #nadie>
        <p class="vc-empty">Nadie fue convocado todavía. Si nadie responde, la ley vuelve a esperar su turno.</p>
      </ng-template>

      <p class="vc-note vc-note--bad dp__err" *ngIf="error() as e">{{ e }}</p>
    </div>
  `,
  styles: [`
    .dp { margin-top: 40px; padding: 26px; }
    .dp__lead { margin-top: 10px; }
    .dp__btn { padding: 4px 12px; font-size: 12.5px; }
    .dp__err { margin-top: 16px; }
  `]
})
export class DeliberationPanelComponent implements OnInit, OnDestroy {
  private api = inject(ApiService);
  private identityService = inject(IdentityService);

  deliberation = signal<Deliberation | null>(null);
  busy = signal(false);
  error = signal<string | null>(null);
  /** Momento local en que vence la pausa: el contador corre sin pedirle al backend. */
  private endsAt = signal<number>(0);
  private now = signal<number>(Date.now());
  private timers: ReturnType<typeof setInterval>[] = [];

  countdown = computed(() => {
    const left = Math.max(0, Math.round((this.endsAt() - this.now()) / 1000));
    return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`;
  });

  actionLabel = actionLabel;

  ngOnInit() {
    this.refresh();
    this.timers.push(setInterval(() => this.refresh(), 3000));
    this.timers.push(setInterval(() => this.now.set(Date.now()), 1000));
  }

  ngOnDestroy() {
    this.timers.forEach(clearInterval);
  }

  label(category: string): string {
    return categoryLabel(category);
  }

  /** ¿Respondo yo por este convocado? El mismo id con que el API autoriza. */
  mine(v: DeliberationVoter): boolean {
    const id = this.identityService.identity();
    if (!id || !v.owner) return false;
    return v.owner === id.pubkey;
  }

  decisionLabel(v: DeliberationVoter): string {
    if (v.decision === 'accept') return 'la respalda';
    if (v.decision === 'reject') return 'no la respalda';
    // Sin respuesta todavía: si tiene una por defecto, es la que va a contar.
    if (v.default_decision === 'accept') return 'sin responder · por defecto la respalda';
    if (v.default_decision === 'reject') return 'sin responder · por defecto no';
    return 'sin responder';
  }

  kindLabel(v: DeliberationVoter): string {
    return v.kind === 'equipo' ? 'equipo' : 'minero independiente';
  }

  /**
   * Su parte del poder de cómputo convocado, en porcentaje.
   *
   * Los hashes por segundo no le dicen nada a nadie; "tiene el 40 % del poder"
   * sí, y es lo que explica por qué importa que el más grande se baje.
   */
  share(d: Deliberation, v: DeliberationVoter): number {
    const total = d.voters.reduce((acc, x) => acc + (x.hashrate || 0), 0);
    return total ? Math.round(((v.hashrate || 0) / total) * 100) : 0;
  }

  duration = friendlyDuration;

  tagOf(v: DeliberationVoter): string {
    return (v.decision ?? v.default_decision) === 'accept' ? 'tag-accent' : 'tag-neutral';
  }

  decide(d: Deliberation, v: DeliberationVoter, decision: DeliberationDecision) {
    this.busy.set(true);
    this.error.set(null);
    this.api.decideDeliberation(d.law_id, v.voter_id, decision).subscribe({
      next: () => { this.busy.set(false); this.refresh(); },
      error: (err) => {
        this.busy.set(false);
        this.error.set(friendlyError(err, 'No se pudo registrar tu decisión. Probá de nuevo.'));
      },
    });
  }

  private refresh() {
    this.api.getDeliberation().subscribe({
      next: (d) => {
        this.deliberation.set(d);
        if (d) this.endsAt.set(Date.now() + d.seconds_left * 1000);
      },
      error: () => {},  // sin respuesta no se muestra nada: mejor que un panel viejo
    });
  }
}
