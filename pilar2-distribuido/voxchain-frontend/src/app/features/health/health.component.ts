import { Component, OnDestroy, OnInit, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ApiService } from '../../core/services/api.service';

/** Lo que el backend puede decir de un componente. */
type Estado = 'ok' | 'error' | 'none' | 'unknown';

interface Servicio {
  name: string;
  /** Valor grande: 'ok', 'sin respuesta', o el recuento de mineros vivos. */
  value: string;
  sub: string;
  badge: string;
  badgeCls: string;
  /** Si late o queda plana. */
  alive: boolean;
  stroke: string;
}

/**
 * Estado del sistema.
 *
 * El trazo de cada tarjeta es lo que hace legible la pantalla de un vistazo: si
 * el componente contesta el health check la línea late, y si dejó de contestar
 * se queda plana. Es la misma información que el texto, pero se lee sin leer —
 * y en una grilla de cuatro, una plana entre tres que laten salta a la vista
 * antes que cualquier palabra.
 */
@Component({
  selector: 'app-health',
  standalone: true,
  imports: [CommonModule],
  template: `
    <main class="vc-page">
      <div class="vc-head">
        <div class="vc-head__text">
          <h6 class="vc-kicker">Operación</h6>
          <h1 class="vc-title">Estado del sistema</h1>
          <p class="vc-lead">
            Los cuatro componentes que tienen que estar vivos para que una ley pueda
            sancionarse. Se consulta cada diez segundos.
          </p>
        </div>
        <span class="mono read-at" *ngIf="lastRead()">última lectura {{ lastRead() }}</span>
      </div>

      <div class="grid">
        <div class="card elev-sm vc-soft svc" *ngFor="let s of services()">
          <div class="svc__top">
            <h4 class="svc__name">{{ s.name }}</h4>
            <span class="tag" [ngClass]="s.badgeCls">{{ s.badge }}</span>
          </div>
          <p class="mono svc__value">{{ s.value }}</p>

          <div class="svc__trace">
            <svg viewBox="0 0 240 40" preserveAspectRatio="none" aria-hidden="true" class="svc__svg">
              <!-- El grupo se desplaza 120px, que es exactamente un ciclo del
                   trazo: al reiniciarse, el latido no da un salto. -->
              <g *ngIf="s.alive" class="svc__beat">
                <path d="M0 20 H26 L31 20 L35 7 L39 33 L43 20 L49 20 H70 L74 15 L78 20 H120 H146 L151 20 L155 7 L159 33 L163 20 L169 20 H190 L194 15 L198 20 H240"
                      fill="none" [attr.stroke]="s.stroke" stroke-width="1.6"
                      stroke-linejoin="round" stroke-linecap="round"></path>
              </g>
              <path *ngIf="!s.alive" d="M0 20 H240" fill="none" [attr.stroke]="s.stroke"
                    stroke-width="1.6" stroke-linecap="round"></path>
            </svg>
          </div>

          <p class="vc-note-sm">{{ s.sub }}</p>
        </div>
      </div>

      <p class="vc-note-sm grid__foot">
        La línea late mientras el componente responde el health check; si deja de
        contestar, se queda plana.
      </p>

      <section class="vc-section--divided qa">
        <div>
          <h5 class="vc-h5">Qué pasa si se cae el NCT</h5>
          <p class="qa__body">
            Otro nodo toma el lease atómico en Redis y sigue abriendo y cerrando
            ventanas. La ventana en curso conserva su deadline.
          </p>
        </div>
        <div>
          <h5 class="vc-h5">Qué pasa si no hay mineros</h5>
          <p class="qa__body">
            Las propuestas se aceptan igual, pero quedan encoladas: la ventana no se
            abre hasta que haya cómputo dispuesto a minar esa área.
          </p>
        </div>
        <div>
          <h5 class="vc-h5">Qué pasa si se cae Redis</h5>
          <p class="qa__body">
            El sistema queda de sólo lectura: sin estado compartido no se puede sellar
            un bloque ni renovar un lease.
          </p>
        </div>
      </section>
    </main>
  `,
  styles: [`
    .read-at { font-size: 11.5px; white-space: nowrap; color: var(--color-neutral-500); }

    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 14px; margin-top: 40px; }
    .grid__foot { margin-top: 18px; }

    .svc { padding: 22px; }
    .svc__top { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .svc__name { margin: 0; font-size: 17px; color: var(--color-neutral-100); }
    .svc__value { margin: 14px 0 0; font-size: 20px; color: var(--color-neutral-100); }
    .svc__trace { position: relative; height: 40px; margin: 14px 0 0; overflow: hidden; }
    .svc__svg { position: absolute; inset: 0; width: 100%; height: 100%; }
    .svc__beat { animation: vc-ecg 2.6s linear infinite; }

    .qa { display: grid; grid-template-columns: repeat(auto-fit, minmax(290px, 1fr)); gap: 32px; }
    .qa__body { margin: 0; font-size: 13px; line-height: 1.7; color: var(--color-neutral-400); }
  `]
})
export class HealthComponent implements OnInit, OnDestroy {
  private api = inject(ApiService);

  private health = signal<Record<string, Estado>>({
    api: 'unknown', nct: 'unknown', workers: 'unknown', redis: 'unknown',
  });
  /** Mineros vivos / registrados, para darle un número a la tarjeta de Mineros. */
  private workerCount = signal<{ vivos: number; total: number } | null>(null);
  lastRead = signal('');

  private timer: ReturnType<typeof setInterval> | null = null;

  services = computed<Servicio[]>(() => {
    const h = this.health();
    const count = this.workerCount();
    return [
      this.build('API', h['api'], 'voxchain-api · :8000'),
      this.build('NCT', h['nct'], 'coordinator · lease vigente'),
      this.build('Mineros', h['workers'],
        count ? `${count.vivos} de ${count.total} reportando` : 'latido de los últimos 15 s',
        count ? `${count.vivos} / ${count.total}` : undefined),
      this.build('Redis', h['redis'], 'estado y leases'),
    ];
  });

  ngOnInit() {
    this.load();
    this.timer = setInterval(() => this.load(), 10000);
  }

  ngOnDestroy() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  /**
   * Traduce un estado crudo del backend a una tarjeta.
   *
   * `none` y `unknown` no son fallas: el primero dice que no hay ningún minero
   * conectado —un estado legítimo del sistema— y el segundo que no se pudo
   * averiguar. Ninguno de los dos merece el rojo que sí merece `error`, pero
   * tampoco el latido: la línea queda plana porque, de hecho, no hay pulso.
   */
  private build(name: string, estado: Estado | undefined, sub: string,
                override?: string): Servicio {
    const e: Estado = estado ?? 'unknown';
    const ok = e === 'ok';
    const caido = e === 'error';
    const value = override && ok ? override : ({
      ok: 'ok',
      error: 'sin respuesta',
      none: 'sin mineros',
      unknown: 'sin datos',
    } as Record<Estado, string>)[e];

    return {
      name,
      value,
      sub: caido ? 'no contesta el health check' : sub,
      badge: ok ? 'en línea' : caido ? 'caído' : 'degradado',
      badgeCls: ok ? 'tag-accent' : 'tag-neutral',
      alive: ok,
      stroke: ok ? 'var(--color-accent)'
        : caido ? 'var(--color-neutral-700)' : 'var(--color-accent-2-400)',
    };
  }

  private load() {
    this.api.getHealth().subscribe({
      next: (status: any) => {
        this.health.set(status);
        this.lastRead.set(new Date().toLocaleTimeString('es-AR'));
      },
      // Si la propia API no contesta, no hay nada que creerle a la lectura
      // anterior: se marcan los cuatro como sin respuesta en vez de dejar en
      // pantalla un "todo ok" de hace diez segundos.
      error: () => {
        this.health.set({ api: 'error', nct: 'error', workers: 'error', redis: 'error' });
        this.lastRead.set(new Date().toLocaleTimeString('es-AR'));
      },
    });
    this.api.getWorkersStatus().subscribe({
      next: (ws) => this.workerCount.set(
        { vivos: ws.filter((w) => w.running).length, total: ws.length }),
      error: () => this.workerCount.set(null),
    });
  }
}
