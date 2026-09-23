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
 * Es la primera pantalla y para mucha gente la única, así que funciona como una
 * landing: promesa, prueba en vivo, cómo funciona, por qué confiar, a quién le
 * sirve y un cierre con llamado a la acción. Habla en el idioma del ciudadano
 * —leyes, votaciones, respaldo— y no en el del sistema: acá no aparecen
 * nonces, ventanas ni ceros.
 *
 * La tarjeta en vivo no es decorado: muestra la votación que la red está
 * resolviendo ahora mismo, con números reales. Por eso todas las consultas
 * fallan en silencio: la portada es pública y se tiene que ver bien aunque el
 * backend no conteste.
 *
 * Los estilos viven en `styles/_home.scss` (prefijo `hm-`): son más de los que
 * admite el presupuesto de 4 kB por componente.
 */
@Component({
  selector: 'app-home',
  standalone: true,
  imports: [CommonModule, RouterModule],
  template: `
    <main class="vc-page vc-page--home hm">

      <!-- ── hero ─────────────────────────────────────────────────────────── -->
      <section class="hm-hero">
        <div class="hm-hero__copy hm-rise">
          <p class="hm-pill">
            <span class="hm-pill__dot" [class.on]="window()"></span>
            {{ window() ? 'La red está votando en este momento' : 'Gobierno abierto · verificable · sin intermediarios' }}
          </p>
          <h1 class="hm-title">
            <span class="hm-title__vox">VOXCHAIN</span>
            <span class="hm-title__reborn">REBORN</span>
          </h1>
          <p class="hm-tagline">El consenso no se cuenta. Se calcula.</p>
          <p class="hm-lead">
            La plataforma donde las leyes se proponen, se deciden y quedan
            <strong>selladas para siempre</strong>. Sin urnas, sin escrutinios a
            puertas cerradas y sin nadie que pueda borrar lo que se decidió.
          </p>
          <div class="hm-cta">
            <a class="btn btn-primary hm-btn hm-btn--main" routerLink="/propose">
              Proponer una ley <span class="hm-arrow" aria-hidden="true">→</span>
            </a>
            <a class="btn btn-secondary hm-btn" routerLink="/workers">Sumar mi computadora</a>
          </div>
          <ul class="hm-trust">
            <li *ngFor="let t of trust">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>
              {{ t }}
            </li>
          </ul>
        </div>

        <div class="hm-hero__side hm-rise hm-rise--late">
          <div class="hm-glow" aria-hidden="true"></div>
          <div class="card elev-md hm-live">
            <div class="vc-live__top">
              <span class="vc-live__kicker">{{ window() ? 'En vivo' : 'Ahora mismo' }}</span>
              <span class="vc-live__state" *ngIf="window()">
                <span class="vc-live__pulse"></span>votando
              </span>
            </div>

            <ng-container *ngIf="window() as w; else noWindow">
              <p class="hm-live__label">Se está decidiendo</p>
              <p class="mono hm-live__law">{{ w.law_id }}</p>
              <div class="vc-actions hm-live__tags">
                <span class="tag tag-accent">{{ cap(actionLabel(w.action)) }}</span>
                <span class="tag tag-outline">{{ label(w.category) }}</span>
              </div>
              <div class="hm-live__grid">
                <div>
                  <p class="hm-live__num">{{ w.n_zeros_required }}</p>
                  <p class="vc-stat__label">dificultad</p>
                </div>
                <div>
                  <p class="hm-live__num">{{ liveWorkers() }}</p>
                  <p class="vc-stat__label">mineros</p>
                </div>
                <div>
                  <p class="hm-live__num hm-live__num--accent">{{ remaining() }}</p>
                  <p class="vc-stat__label">para el cierre</p>
                </div>
              </div>
              <div class="vc-live__bar"><div class="vc-live__sweep"></div></div>
              <a class="hm-link" [routerLink]="identityService.identity() ? '/queue' : '/dashboard'">
                Seguir la votación <span aria-hidden="true">→</span>
              </a>
            </ng-container>

            <ng-template #noWindow>
              <p class="hm-live__calm">La red está en calma</p>
              <p class="hm-live__idle">
                No hay ninguna ley en juego. La próxima votación arranca en cuanto
                alguien proponga una y haya mineros dispuestos a respaldarla.
              </p>
              <a class="hm-link" routerLink="/propose">
                Proponé la próxima <span aria-hidden="true">→</span>
              </a>
            </ng-template>
          </div>
        </div>
      </section>

      <!-- ── números ──────────────────────────────────────────────────────── -->
      <section class="hm-numbers" aria-label="La red en números">
        <div class="hm-num">
          <p class="hm-num__value">{{ promulgated() }}</p>
          <p class="hm-num__label">leyes vigentes</p>
        </div>
        <div class="hm-num">
          <p class="hm-num__value">{{ blocks() }}</p>
          <p class="hm-num__label">decisiones selladas</p>
        </div>
        <div class="hm-num">
          <p class="hm-num__value hm-num__value--accent">{{ liveWorkers() }}</p>
          <p class="hm-num__label">mineros encendidos</p>
        </div>
        <div class="hm-num">
          <p class="hm-num__value">{{ teams() }}</p>
          <p class="hm-num__label">equipos en juego</p>
        </div>
      </section>

      <!-- ── cómo funciona ────────────────────────────────────────────────── -->
      <section class="hm-section">
        <p class="hm-eyebrow">Cómo funciona</p>
        <h2 class="hm-h2">De una idea a una ley, en tres pasos</h2>
        <div class="hm-steps">
          <div class="hm-step" *ngFor="let s of steps; let i = index">
            <div class="hm-step__mark">
              <span class="mono hm-step__num">0{{ i + 1 }}</span><span class="hm-step__line"></span>
            </div>
            <h3 class="hm-step__title">{{ s.title }}</h3>
            <p class="hm-body">{{ s.body }}</p>
          </div>
        </div>
      </section>

      <!-- ── por qué ──────────────────────────────────────────────────────── -->
      <section class="hm-section">
        <p class="hm-eyebrow">Por qué VoxChain</p>
        <h2 class="hm-h2">Un sistema en el que no hace falta confiar a ciegas</h2>
        <div class="hm-features">
          <article class="card elev-sm hm-feature" *ngFor="let f of features">
            <span class="hm-icon">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path [attr.d]="f.icon"/></svg>
            </span>
            <h3 class="hm-feature__title">{{ f.title }}</h3>
            <p class="hm-body">{{ f.body }}</p>
          </article>
        </div>
      </section>

      <!-- ── para quién ───────────────────────────────────────────────────── -->
      <section class="hm-section">
        <p class="hm-eyebrow">Para quién</p>
        <h2 class="hm-h2">Dos formas de sumarte</h2>
        <div class="hm-paths">
          <article class="card elev-sm hm-path hm-path--main">
            <span class="card-kicker">Para ciudadanos</span>
            <h3 class="hm-path__title">¿Tenés una idea para cambiar las reglas?</h3>
            <p class="hm-body">
              Escribila, firmala con tu identidad y dejá que la red decida si vale el
              esfuerzo. Tu propuesta llega entera y a tu nombre.
            </p>
            <ul class="hm-list">
              <li>Proponé leyes nuevas o derogá las que ya no sirven</li>
              <li>Seguí cada votación en tiempo real</li>
              <li>Nadie puede hablar en tu nombre</li>
            </ul>
            <a class="btn btn-primary hm-btn" routerLink="/propose">Proponer una ley</a>
          </article>
          <article class="card elev-sm hm-path">
            <span class="card-kicker">Para mineros</span>
            <h3 class="hm-path__title">¿Tenés una computadora con ganas de trabajar?</h3>
            <p class="hm-body">
              Ponela a respaldar las leyes en las que creés. Minando solo o en equipo,
              tu poder de cómputo es tu voz.
            </p>
            <ul class="hm-list">
              <li>La sumás en minutos desde esta misma web</li>
              <li>Elegís qué áreas y qué leyes respaldás</li>
              <li>Formá un equipo y multiplicá tu peso</li>
            </ul>
            <a class="btn btn-secondary hm-btn" routerLink="/workers">Sumar mi computadora</a>
          </article>
        </div>
      </section>

      <!-- ── preguntas ────────────────────────────────────────────────────── -->
      <section class="hm-section hm-faq">
        <div>
          <p class="hm-eyebrow">Preguntas frecuentes</p>
          <h2 class="hm-h2">Lo que todos preguntan primero</h2>
        </div>
        <div class="hm-faq__list">
          <details class="hm-q" *ngFor="let q of faq">
            <summary>{{ q.q }}</summary>
            <p class="hm-body">{{ q.a }}</p>
          </details>
        </div>
      </section>

      <!-- ── cierre ───────────────────────────────────────────────────────── -->
      <section class="hm-closing">
        <div class="hm-closing__glow" aria-hidden="true"></div>
        <h2 class="hm-closing__title">Tu voz, sellada para siempre.</h2>
        <p class="hm-closing__body">
          Creá tu identidad en segundos, sin datos personales, y proponé tu primera ley hoy.
        </p>
        <div class="hm-cta hm-cta--center">
          <a class="btn btn-primary hm-btn hm-btn--main"
             [routerLink]="identityService.identity() ? '/propose' : '/identity'">
            {{ identityService.identity() ? 'Proponer una ley' : 'Crear mi identidad' }}
            <span class="hm-arrow" aria-hidden="true">→</span>
          </a>
          <a class="btn btn-secondary hm-btn" routerLink="/laws">Ver las leyes vigentes</a>
        </div>
      </section>

      <p class="hm-foot">
        Proyecto integrador · Sistemas Distribuidos y Programación Paralela · UNLu
      </p>
    </main>
  `,
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

  readonly trust = [
    'Sin cuentas ni contraseñas',
    'Registro público e inalterable',
    'Nadie controla el resultado',
  ];

  readonly steps = [
    {
      title: 'Escribí tu propuesta',
      body: 'Redactá una ley nueva o elegí una vigente para derogar. Tu firma digital '
        + 'garantiza que es tuya y que nadie la puede alterar en el camino.',
    },
    {
      title: 'La red decide',
      body: 'Mineros y equipos eligen si la respaldan poniendo a trabajar sus '
        + 'computadoras. Apoyar una ley cuesta esfuerzo real: por eso vale.',
    },
    {
      title: 'Queda sellada para siempre',
      body: 'Si la red la resuelve a tiempo, entra al registro público encadenada a '
        + 'todas las anteriores. Si nadie la respalda, se descarta: el silencio también decide.',
    },
  ];

  /** Los trazos de cada ícono van acá para no repetir seis bloques de SVG en la plantilla. */
  readonly features = [
    {
      title: 'Inalterable',
      body: 'Cada decisión se encadena a la anterior. Cambiar una sola obligaría a '
        + 'rehacer todo lo que vino después.',
      icon: 'M6 11h12v10H6zM8.5 11V7.5a3.5 3.5 0 0 1 7 0V11',
    },
    {
      title: 'Transparente',
      body: 'Cualquiera puede ver qué se propuso, cómo se votó y cómo terminó. '
        + 'Sin registrarse y sin pedirle permiso a nadie.',
      icon: 'M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12zM12 9.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z',
    },
    {
      title: 'Tu identidad, en tus manos',
      body: 'Se crea en tu navegador en segundos. No hay contraseñas que robar ni '
        + 'datos personales que entregar.',
      icon: 'M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6zM9 12l2 2 4-4',
    },
    {
      title: 'Deshacer cuesta más',
      body: 'Derogar una ley exige dieciséis veces más esfuerzo que aprobarla. La '
        + 'estabilidad es parte del diseño.',
      icon: 'M12 4v16M5 20h14M4 8h16M7 8l-3 6a3 3 0 0 0 6 0zM17 8l-3 6a3 3 0 0 0 6 0z',
    },
    {
      title: 'La unión hace la fuerza',
      body: 'Sumá tu computadora a un equipo, elijan juntos qué temas respaldan y '
        + 'multipliquen su peso en cada votación.',
      icon: 'M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM2.5 20a6.5 6.5 0 0 1 13 0M16 4.5a3.5 3.5 0 0 1 0 7M18 14.5a6.5 6.5 0 0 1 3.5 5.5',
    },
    {
      title: 'Siempre en pie',
      body: 'Si una parte de la red se cae, otra toma el relevo al instante. '
        + 'Ninguna pieza es imprescindible.',
      icon: 'M3 12h4l3-8 4 16 3-8h4',
    },
  ];

  readonly faq = [
    {
      q: '¿Necesito crear una cuenta?',
      a: 'No. Tu identidad se genera en tu navegador en un par de segundos y tu clave '
        + 'secreta nunca sale de ahí. No te pedimos mail, teléfono ni contraseña.',
    },
    {
      q: '¿Qué pasa si nadie apoya mi ley?',
      a: 'No avanza. No hace falta que nadie la rechace: si ningún minero la '
        + 'respalda antes del cierre, se descarta. El silencio también decide.',
    },
    {
      q: '¿Se puede borrar una ley?',
      a: 'Nunca se borra: se deroga, con una nueva votación que exige más esfuerzo '
        + 'que la original. Y las dos decisiones quedan en el registro para siempre.',
    },
    {
      q: '¿Por qué se vota con computadoras y no con personas?',
      a: 'Porque el esfuerzo no se puede falsificar. Mil cuentas falsas no suman '
        + 'nada si no hay trabajo real detrás: cada respaldo cuesta, y por eso cuenta.',
    },
    {
      q: '¿Quién controla la red?',
      a: 'Nadie en particular. La coordinación sólo abre y cierra votaciones; no '
        + 'opina sobre las leyes. Y si se cae, otro nodo toma su lugar al instante.',
    },
  ];

  /**
   * La votación en curso: la que llegó en vivo si hay, y si no la del arranque.
   *
   * El evento manda porque es más nuevo que la consulta inicial; pero con la
   * pestaña recién abierta todavía no llegó ninguno, y sin el respaldo la
   * portada arrancaría diciendo que la red está en calma aunque esté votando.
   */
  window = computed(() => this.events.activeWindow() ?? this.fetchedWindow());

  /**
   * Cuánto falta para el cierre. `—` si no hay fecha, `00:00` si ya venció.
   *
   * Las horas aparecen sólo cuando las hay: el plazo de una votación se fija
   * por configuración, así que puede ser de minutos o de horas, y sin este
   * corte una votación larga mostraba "563:22" — minutos que nadie lee como
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

  cap(text: string): string {
    return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
  }

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
   *
   * Las leyes vigentes se cuentan sobre las leyes y no sobre los bloques: los
   * bloques de promulgación incluyen también las que después se derogaron.
   */
  private load() {
    this.api.getChain().subscribe({
      next: (bs) => this.blocks.set(bs.length),
      error: () => {},
    });
    this.api.getLaws('promulgated').subscribe({
      next: (ls) => this.promulgated.set(ls.length),
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
