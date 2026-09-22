import { Component, OnDestroy, OnInit, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { ApiService } from '../../core/services/api.service';
import { EventsService } from '../../core/services/events.service';
import { IdentityService } from '../../core/services/identity.service';
import { actionLabel, categoryLabel } from '../../core/models/law.model';
import { Window } from '../../core/models/window.model';

/**
 * Portada del proyecto.
 *
 * Es la primera pantalla y para mucha gente la única: tiene que explicar en
 * treinta segundos qué es esto y dejar dos caminos claros (proponer una ley o
 * poner a minar una máquina).
 *
 * La columna derecha no es decorado: muestra la ventana que la red está minando
 * ahora mismo. Es lo que convierte la promesa del título en algo que se está
 * cumpliendo mientras mirás. Por eso los números son reales y no de ejemplo — y
 * por eso todas las consultas fallan en silencio: la portada es pública y se
 * tiene que ver bien aunque el backend no conteste.
 */
@Component({
  selector: 'app-home',
  standalone: true,
  imports: [CommonModule, RouterModule],
  template: `
    <main class="vc-page vc-page--home">

      <section class="hero">
        <div>
          <p class="hero__eyebrow">Blockchain de gobierno · UNLu · Sistemas Distribuidos y Programación Paralela</p>
          <h1 class="hero__title">
            <span class="hero__vox">VOXCHAIN</span>
            <span class="hero__reborn">REBORN</span>
          </h1>
          <p class="hero__tagline">El consenso no se cuenta. Se calcula.</p>
          <p class="hero__lead">
            Una blockchain que no mueve dinero: promulga y deroga <strong>leyes</strong>.
            Cualquiera con un par de claves propone; la voluntad colectiva se mide en
            hashes calculados, no en cabezas contadas.
          </p>
          <div class="hero__cta">
            <a class="btn btn-primary hero__btn" routerLink="/propose">Proponer una ley</a>
            <a class="btn btn-secondary hero__btn" routerLink="/workers">Poner una máquina a minar</a>
          </div>
          <p class="hero__hint" *ngIf="!identityService.identity()">
            No hace falta registrarse en ningún lado: tu identidad es un par de claves
            que se genera en tu navegador y la privada nunca sale de ahí.
          </p>
        </div>

        <div class="hero__side">
          <div class="card elev-sm vc-soft live">
            <div class="vc-live__top">
              <span class="vc-live__kicker">Ventana activa</span>
              <span class="vc-live__state" *ngIf="window()">
                <span class="vc-live__pulse"></span>minando
              </span>
            </div>

            <ng-container *ngIf="window() as w; else noWindow">
              <p class="live__law">{{ w.law_id }} · {{ actionLabel(w.action) }}</p>
              <p class="vc-note-sm live__area">Área: {{ label(w.category) }}</p>
              <div class="vc-rule vc-rule--flat live__rule"></div>
              <div class="live__grid">
                <div>
                  <p class="vc-stat__value live__num">{{ w.n_zeros_required }}</p>
                  <p class="vc-stat__label">ceros</p>
                </div>
                <div>
                  <p class="vc-stat__value live__num">{{ liveWorkers() }}</p>
                  <p class="vc-stat__label">mineros</p>
                </div>
                <div>
                  <p class="vc-stat__value live__num vc-stat__value--accent">{{ remaining() }}</p>
                  <p class="vc-stat__label">al cierre</p>
                </div>
              </div>
              <div class="vc-live__bar"><div class="vc-live__sweep"></div></div>
            </ng-container>

            <ng-template #noWindow>
              <p class="live__law">Ninguna ley en disputa</p>
              <p class="vc-note-sm live__area">
                La red está en reposo. Se abre una ventana en cuanto haya una propuesta
                con mineros dispuestos a minar su área.
              </p>
            </ng-template>
          </div>

          <div class="hero__stats">
            <div class="card elev-sm vc-stat">
              <p class="vc-stat__value">{{ blocks() }}</p>
              <p class="vc-stat__label">bloques</p>
            </div>
            <div class="card elev-sm vc-stat">
              <p class="vc-stat__value">{{ promulgated() }}</p>
              <p class="vc-stat__label">leyes vigentes</p>
            </div>
            <div class="card elev-sm vc-stat">
              <p class="vc-stat__value">{{ teams() }}</p>
              <p class="vc-stat__label">equipos</p>
            </div>
          </div>
        </div>
      </section>

      <section class="steps">
        <h6 class="steps__head">Cómo se sanciona una ley</h6>
        <div class="steps__grid">
          <div>
            <div class="step__mark"><span class="mono step__num">01</span><span class="step__line"></span></div>
            <h4 class="step__title">Alguien propone</h4>
            <p class="step__body">
              Proponer no cuesta trabajo de cómputo, cuesta <em class="step__em">turno</em>:
              después de proponer entrás en cooldown y no podés volver a hacerlo por unas
              cuantas ventanas.
            </p>
          </div>
          <div>
            <div class="step__mark"><span class="mono step__num">02</span><span class="step__line"></span></div>
            <h4 class="step__title">La red entera mina esa ley</h4>
            <p class="step__body">
              Se abre una sola ventana de votación a la vez y toda la red apunta su
              cómputo al mismo desafío. Apoyar una ley es gastar electricidad en ella.
            </p>
          </div>
          <div>
            <div class="step__mark"><span class="mono step__num">03</span><span class="step__line"></span></div>
            <h4 class="step__title">El primero que la resuelve la sella</h4>
            <p class="step__body">
              El nonce ganador queda en el bloque, encadenado al anterior. Si nadie lo
              encuentra antes del cierre, la ley se descarta: el silencio también decide.
            </p>
          </div>
        </div>
      </section>

      <section class="pillars">
        <article class="card elev-sm pillar">
          <span class="tag tag-outline pillar__tag">asimetría</span>
          <h4 class="pillar__title">Derogar cuesta más que promulgar</h4>
          <p class="pillar__body">
            Promulgar exige <code class="vc-code">n</code> ceros; derogar,
            <code class="vc-code">n+1</code>. Cada cero multiplica el trabajo por 16.
            Deshacer lo hecho tiene que costarle más a la red que hacerlo.
          </p>
        </article>
        <article class="card elev-sm pillar">
          <span class="tag tag-outline pillar__tag">poder</span>
          <h4 class="pillar__title">Los equipos son facciones políticas</h4>
          <p class="pillar__body">
            Podés minar solo o fundar un equipo que reparta el espacio de búsqueda entre
            sus mineros. Quien junta más cómputo decide más leyes — el sistema reproduce
            a propósito la concentración de poder de las blockchains reales.
          </p>
        </article>
        <article class="card elev-sm pillar">
          <span class="tag tag-outline pillar__tag">neutralidad</span>
          <h4 class="pillar__title">El coordinador no arbitra contenido</h4>
          <p class="pillar__body">
            El NCT abre y cierra ventanas, verifica nonces y sella bloques. No opina
            sobre las leyes. Y si se cae, otro nodo toma el relevo con un lease atómico:
            nadie es dueño del proceso.
          </p>
        </article>
      </section>

      <section class="closing">
        <div>
          <h2 class="closing__title">Empezá por donde quieras</h2>
          <p class="closing__body">
            Escribí una ley y dejá que la red decida si vale el esfuerzo, o poné una
            máquina a minar y sumate a un equipo.
          </p>
        </div>
        <div class="vc-actions">
          <a class="btn btn-primary hero__btn" routerLink="/propose">Proponer una ley</a>
          <a class="btn btn-secondary hero__btn" routerLink="/workers">Minería</a>
        </div>
      </section>
    </main>
  `,
  styles: [`
    .hero {
      display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, .85fr);
      gap: 64px; align-items: end; padding: 104px 0 88px;
    }
    .hero__eyebrow {
      margin: 0 0 30px; font-size: 11px; letter-spacing: .16em;
      text-transform: uppercase; color: var(--color-neutral-500);
    }
    .hero__title {
      margin: 0; font-size: clamp(46px, 6.4vw, 92px); line-height: .94;
      letter-spacing: -.035em; font-weight: 500;
    }
    .hero__vox, .hero__reborn { display: block; }
    .hero__vox { color: var(--color-neutral-100); }
    /* El resplandor es el único lugar donde el acento se derrama, y es una
       aureola, no un relleno: el sistema lo admite como luz. */
    .hero__reborn {
      color: var(--color-accent); font-weight: 300;
      text-shadow: 0 0 60px color-mix(in srgb, var(--color-accent) 45%, transparent);
    }
    .hero__tagline {
      margin: 34px 0 0; font-size: clamp(19px, 2.1vw, 27px); font-weight: 300;
      letter-spacing: -.01em; color: var(--color-accent-300);
    }
    .hero__lead {
      margin: 22px 0 0; max-width: 58ch; font-size: 15.5px; line-height: 1.75;
      color: var(--color-neutral-300);
    }
    .hero__lead strong { color: var(--color-neutral-100); font-weight: 500; }
    .hero__cta { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 40px; }
    .hero__btn { min-height: 44px; padding: 0 24px; font-size: 14.5px; }
    .hero__hint {
      margin: 22px 0 0; max-width: 54ch; font-size: 12.5px; line-height: 1.7;
      color: var(--color-neutral-500);
    }

    .hero__side { display: flex; flex-direction: column; gap: 14px; }
    .hero__stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
    .hero__stats .vc-stat { padding: 16px 18px; }
    .hero__stats .vc-stat__value { font-size: 26px; }

    .live { gap: 0; padding: 20px 22px; }
    .live__law { margin: 14px 0 0; font-size: 18px; line-height: 1.3; color: var(--color-neutral-100); }
    .live__area { margin-top: 6px; }
    .live__rule { margin: 18px 0; }
    .live__grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
    .live__num { font-size: 22px; }

    .steps { padding: 56px 0 64px; }
    .steps, .closing { border-top: 1px solid var(--color-divider); }
    .steps__head { margin: 0 0 34px; color: var(--color-neutral-500); }
    .steps__grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 40px; }
    .step__mark { display: flex; align-items: center; gap: 12px; }
    .step__num { font-size: 12px; color: var(--color-accent); }
    .step__line { flex: 1; height: 1px; background: var(--color-accent-800); }
    .step__title { margin: 16px 0 10px; font-size: 19px; color: var(--color-neutral-100); }
    .step__body, .pillar__body { margin: 0; font-size: 13.5px; line-height: 1.7; color: var(--color-neutral-400); }
    .step__em { font-style: normal; border-bottom: 1px dotted var(--color-neutral-600); }

    .pillars { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; padding: 8px 0 64px; }
    .pillar { padding: 26px 24px; background: color-mix(in srgb, var(--color-surface) 60%, transparent); }
    .pillar__tag { align-self: flex-start; }
    .pillar__title { margin: 14px 0 10px; font-size: 18px; line-height: 1.3; color: var(--color-neutral-100); }

    .closing {
      display: flex; flex-wrap: wrap; align-items: end; justify-content: space-between;
      gap: 32px; padding: 56px 0 0;
    }
    .closing__title { margin: 0 0 12px; font-size: 32px; font-weight: 400; letter-spacing: -.02em; color: var(--color-neutral-100); }
    .closing__body { margin: 0; max-width: 52ch; font-size: 14.5px; line-height: 1.7; color: var(--color-neutral-400); }

    @media (max-width: 960px) {
      .hero { grid-template-columns: minmax(0, 1fr); gap: 48px; padding: 64px 0 56px; }
    }
  `]
})
export class HomeComponent implements OnInit, OnDestroy {
  private api = inject(ApiService);
  private events = inject(EventsService);
  identityService = inject(IdentityService);

  blocks = signal(0);
  promulgated = signal(0);
  teams = signal(0);
  liveWorkers = signal(0);
  private fetchedWindow = signal<Window | null>(null);

  /** Ahora, en milisegundos. Lo mueve el reloj de abajo para recalcular el cierre. */
  private now = signal(Date.now());
  private tick?: ReturnType<typeof setInterval>;

  /**
   * La ventana en curso: la que llegó por SSE si hay, y si no la del arranque.
   *
   * El evento manda porque es más nuevo que la consulta inicial; pero con la
   * pestaña recién abierta todavía no llegó ninguno, y sin el respaldo la
   * portada arrancaría diciendo que la red está en reposo aunque esté minando.
   */
  window = computed(() => this.events.activeWindow() ?? this.fetchedWindow());

  /**
   * Cuánto falta para el cierre. `—` si no hay fecha, `00:00` si ya venció.
   *
   * Las horas aparecen sólo cuando las hay: el plazo de una ventana lo fija el
   * NCT por configuración, así que puede ser de minutos o de horas, y sin este
   * corte una ventana larga mostraba "563:22" — minutos que nadie lee como
   * nueve horas y media.
   */
  remaining = computed(() => {
    const w = this.window();
    if (!w?.deadline) return '—';
    const left = new Date(w.deadline).getTime() - this.now();
    if (!Number.isFinite(left) || left <= 0) return '00:00';

    const total = Math.floor(left / 1000);
    const pad = (n: number) => String(n).padStart(2, '0');
    const hh = Math.floor(total / 3600);
    const mm = Math.floor((total % 3600) / 60);
    const ss = total % 60;
    return hh ? `${hh}:${pad(mm)}:${pad(ss)}` : `${pad(mm)}:${pad(ss)}`;
  });

  label(category: string): string {
    return categoryLabel(category);
  }

  actionLabel = actionLabel;

  ngOnInit() {
    this.tick = setInterval(() => this.now.set(Date.now()), 1000);
    this.load();
  }

  ngOnDestroy() {
    if (this.tick) clearInterval(this.tick);
  }

  /**
   * Los números de la portada.
   *
   * Todo con `error: () => {}`: esta pantalla es pública y es la primera que ve
   * alguien que nunca escuchó del proyecto. Mejor un cero que un cartel de
   * error donde debería estar la explicación de qué es esto.
   */
  private load() {
    this.api.getChain().subscribe({
      next: (bs) => {
        this.blocks.set(bs.length);
        this.promulgated.set(bs.filter((b) => b.action === 'promulgacion').length);
      },
      error: () => {},
    });
    this.api.listTeams().subscribe({
      next: (ts) => this.teams.set(ts.length),
      error: () => {},
    });
    this.api.getWorkersStatus().subscribe({
      next: (ws) => this.liveWorkers.set(ws.filter((w) => w.running).length),
      error: () => {},
    });
    this.api.getActiveWindow().subscribe({
      next: (w) => this.fetchedWindow.set(w),
      error: () => this.fetchedWindow.set(null),
    });
  }
}
