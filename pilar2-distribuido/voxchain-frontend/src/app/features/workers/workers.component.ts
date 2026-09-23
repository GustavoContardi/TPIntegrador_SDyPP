import { Component, OnDestroy, OnInit, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { ApiService } from '../../core/services/api.service';
import { IdentityService } from '../../core/services/identity.service';
import {
  Team,
  WorkerStatus,
  isDynamicWorker,
  isOwnedBy,
} from '../../core/models/worker.model';
import { TeamsComponent } from '../teams/teams.component';
import { friendlyError, modeLabel } from '../../core/utils/format';

interface PoolPolicy {
  decision: string;
  action?: string;
  law_id?: string;
}

interface PoolHealth {
  pool: string;
  rabbitmq: string;
  miners: number;
  voting_policy: PoolPolicy;
}

/**
 * Minería: los mineros de la red y los equipos, en una sola pantalla.
 *
 * El orden importa. Arriba van los cuatro números que resumen la red, después
 * los equipos (la decisión política) y recién al final la tabla de mineros (el
 * detalle operativo). Quien llega a esta pantalla quiere saber primero con
 * quién se mina, no con qué.
 */
@Component({
  selector: 'app-workers',
  standalone: true,
  imports: [CommonModule, MatSnackBarModule, TeamsComponent],
  template: `
    <main class="vc-page">
      <div class="vc-head">
        <div class="vc-head__text">
          <h6 class="vc-kicker">Cómputo</h6>
          <h1 class="vc-title">Minería</h1>
          <p class="vc-lead">
            Poné tu computadora a trabajar por las leyes en las que creés. Minando solo o
            en equipo, cada minero suma poder a la hora de decidir.
          </p>
        </div>
        <div class="head__side">
          <button class="btn btn-primary head__btn" *ngIf="canRegisterWorker() && !showRegisterForm()"
                  (click)="showRegisterForm.set(true)">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" aria-hidden="true">
              <path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"></path>
            </svg>
            Sumar mi minero
          </button>
          <p class="vc-note-sm head__note" *ngIf="canRegisterWorker()">Cada identidad puede tener un minero.</p>
          <p class="vc-note-sm head__note" *ngIf="myWorker() as mine">
            Tu minero es <code class="vc-code">{{ mine.worker_id }}</code>.
          </p>
        </div>
      </div>

      <div class="vc-stats stats">
        <div class="card elev-sm vc-stat">
          <p class="vc-stat__value">{{ workers().length }}</p>
          <p class="vc-stat__label">mineros en la red</p>
        </div>
        <div class="card elev-sm vc-stat">
          <p class="vc-stat__value vc-stat__value--accent">{{ runningCount() }}</p>
          <p class="vc-stat__label">encendidos ahora</p>
        </div>
        <div class="card elev-sm vc-stat">
          <p class="vc-stat__value">{{ teams().length }}</p>
          <p class="vc-stat__label">equipos</p>
        </div>
        <div class="card elev-sm vc-stat">
          <p class="vc-stat__value">{{ soloCount() }}</p>
          <p class="vc-stat__label">por su cuenta</p>
        </div>
      </div>

      <!-- ── alta de minero ──────────────────────────────────────────────── -->
      <div class="card elev-sm vc-soft panel" *ngIf="showRegisterForm()">
        <span class="card-kicker">Sumar tu minero</span>
        <p class="panel__sub">
          Tu minero queda a tu nombre: sólo vos podés sumarlo a un equipo, sacarlo o
          darlo de baja. Arranca minando por su cuenta y en unos segundos ya está
          aportando a la red.
        </p>
        <div class="field panel__field">
          <label for="vc-worker-id">Nombre del minero</label>
          <input id="vc-worker-id" class="input" placeholder="Ej: minero-ciudadano-1"
                 [value]="newWorkerId()" (input)="newWorkerId.set($any($event.target).value)">
          <p class="vc-note-sm panel__hint">Elegí un nombre único: así lo van a ver los demás en la red.</p>
        </div>
        <div class="vc-actions panel__actions">
          <button class="btn btn-secondary" (click)="cancelRegister()">Cancelar</button>
          <button class="btn btn-primary" (click)="confirmRegister()"
                  [disabled]="!newWorkerId().trim() || registering()">
            {{ registering() ? 'Sumando…' : 'Sumar minero' }}
          </button>
        </div>
      </div>

      <!-- ── equipos ─────────────────────────────────────────────────────── -->
      <app-teams id="seccion-equipos" [teams]="teams()" [workers]="workers()" (changed)="loadAll()"></app-teams>

      <!-- ── mineros de la red ───────────────────────────────────────────── -->
      <section class="vc-section">
        <h3 class="vc-h3 net__title">Mineros de la red</h3>
        <div class="vc-table-wrap" *ngIf="workers().length; else sinMineros">
          <table class="table net__table">
            <thead>
              <tr>
                <th>Minero</th>
                <th>Cómo mina</th>
                <th>Equipo</th>
                <th>Qué respalda</th>
                <th>Estado</th>
                <th class="num">Acciones</th>
              </tr>
            </thead>
            <tbody>
              <tr *ngFor="let w of workers()">
                <td>
                  <code>{{ w.worker_id }}</code>
                  <span class="tag tag-accent net__mine" *ngIf="isWorkerOwned(w)">mío</span>
                </td>
                <td><span class="tag" [ngClass]="modeCls(w.mode)">{{ modeLabel(w.mode) }}</span></td>
                <td class="net__team">
                  <ng-container *ngIf="w.team_id; else solo">
                    <a href="#seccion-equipos" (click)="scrollToTeams($event)">{{ w.team_name }}</a>
                    <span class="net__role">{{ w.team_role === 'coordinator' ? 'líder' : 'miembro' }}</span>
                  </ng-container>
                  <ng-template #solo><span class="vc-muted">por su cuenta</span></ng-template>
                </td>
                <td class="net__policy" [class.empty]="getPolicyDisplay(w) === '—'">{{ getPolicyDisplay(w) }}</td>
                <td>
                  <span class="vc-state" [class.online]="w.running">
                    <span class="dot" [class.online]="w.running"></span>
                    {{ w.running ? 'encendido' : 'apagado' }}
                  </span>
                  <span class="vc-note-sm net__pending" *ngIf="!w.running && isWorkerOwned(w)">
                    todavía no se encendió
                  </span>
                </td>
                <td class="num">
                  <span class="net__actions">
                    <button class="btn btn-ghost net__btn" *ngIf="isWorkerOwned(w) && w.team_id"
                            (click)="backToSolo(w)" [disabled]="switching()"
                            title="Sacarlo del equipo para que vuelva a minar por su cuenta">Minar por su cuenta</button>
                    <button class="btn btn-ghost net__btn" *ngIf="isWorkerOwned(w) && !w.team_id"
                            (click)="scrollToTeams($event)">Sumar a un equipo</button>
                    <button class="btn btn-ghost net__btn"
                            *ngIf="w.mode === 'pool-coordinator' && isWorkerOwned(w)"
                            (click)="openPolicyDialog(w)">Qué respalda</button>
                    <button class="btn btn-ghost net__btn net__danger"
                            *ngIf="isDynamicWorker(w) && isWorkerOwned(w)"
                            (click)="confirmUnregister(w)">Dar de baja</button>
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <ng-template #sinMineros>
          <p class="vc-empty">Todavía no hay ningún minero registrado en la red.</p>
        </ng-template>
      </section>

      <!-- ── política del pool ───────────────────────────────────────────── -->
      <div class="card elev-sm vc-soft panel" *ngIf="selectedPoolCoordinator() as pool">
        <span class="card-kicker">Qué respalda {{ pool.worker_id }}</span>
        <p class="panel__sub" *ngIf="poolHealth() as h">
          {{ h.miners }} {{ h.miners === 1 ? 'minero conectado' : 'mineros conectados' }} ·
          hoy {{ policySummary(h.voting_policy) }}
        </p>

        <div class="field panel__field">
          <label for="vc-decision">Postura del equipo</label>
          <select id="vc-decision" class="input" [value]="policyDecision()"
                  (change)="policyDecision.set($any($event.target).value)">
            <option value="accept">Respaldar todas las leyes</option>
            <option value="reject">Rechazar algunas</option>
          </select>
        </div>

        <ng-container *ngIf="policyDecision() === 'reject'">
          <div class="field panel__field">
            <label for="vc-pol-action">¿Qué rechaza?</label>
            <select id="vc-pol-action" class="input" [value]="policyAction()"
                    (change)="policyAction.set($any($event.target).value)">
              <option value="">Todo</option>
              <option value="promulgacion">Leyes nuevas</option>
              <option value="derogacion">Derogaciones</option>
            </select>
          </div>
          <div class="field panel__field">
            <label for="vc-pol-law">Una ley puntual (opcional)</label>
            <input id="vc-pol-law" class="input" placeholder="Ej: ley-3f2a91bc"
                   [value]="policyLawId()" (input)="policyLawId.set($any($event.target).value)">
          </div>
        </ng-container>

        <div class="vc-actions panel__actions">
          <button class="btn btn-secondary" (click)="cancelPolicy()">Cancelar</button>
          <button class="btn btn-primary" (click)="confirmPolicy()">Guardar</button>
        </div>
      </div>

      <!-- ── modos ───────────────────────────────────────────────────────── -->
      <section class="vc-section--divided">
        <h3 class="vc-h3 vc-h3--sm modes__title">Tres formas de minar</h3>
        <div class="modes">
          <div>
            <p class="modes__code"><span class="tag tag-outline">por su cuenta</span></p>
            <h5 class="vc-h5">Solo contra todos</h5>
            <p class="modes__body">
              Tu minero compite contra toda la red y decidís vos qué leyes respalda. Total
              independencia, pero tu poder es sólo el de tu computadora.
            </p>
          </div>
          <div>
            <p class="modes__code"><span class="tag tag-accent">lidera un equipo</span></p>
            <h5 class="vc-h5">Líder de equipo</h5>
            <p class="modes__body">
              Reparte el trabajo entre los mineros del equipo y además mina. Quien funda
              el equipo decide qué áreas y qué leyes respalda.
            </p>
          </div>
          <div>
            <p class="modes__code"><span class="tag tag-neutral">en equipo</span></p>
            <h5 class="vc-h5">Parte de un equipo</h5>
            <p class="modes__body">
              Hace su parte del trabajo que le asigna el líder. Cada minero que se suma
              hace al equipo más rápido, y le da más peso en cada votación.
            </p>
          </div>
        </div>
      </section>
    </main>
  `,
  styles: [`
    .head__side { display: flex; flex-direction: column; align-items: flex-end; gap: 8px; }
    .head__btn { min-height: 42px; padding: 0 20px; font-size: 14px; }
    .head__note { text-align: right; }

    .stats { margin-top: 40px; }

    .panel { padding: 24px; margin-top: 24px; }
    .panel__sub { margin: 10px 0 0; font-size: 13px; line-height: 1.7; color: var(--color-neutral-400); }
    .panel__field { margin-top: 16px; }
    .panel__hint { margin-top: 6px; }
    .panel__actions { justify-content: flex-end; margin-top: 20px; gap: 12px; }

    .net__title { margin-bottom: 20px; }
    .net__table { min-width: 900px; white-space: nowrap; }
    .net__mine { margin-left: 8px; }
    .net__team { font-size: 13px; }
    .net__team a { color: var(--color-accent-300); }
    .net__role { margin-left: 6px; font-size: 11px; color: var(--color-neutral-500); }
    .net__policy { font-size: 13px; color: var(--color-neutral-300); }
    /* El guion de "sin política" no es un dato: se apaga para que no compita
       con las celdas que sí dicen algo. */
    .net__policy.empty { color: var(--color-neutral-700); }
    .net__pending { display: block; margin-top: 4px; font-size: 11px; }
    .net__actions { display: inline-flex; gap: 4px; }
    .net__btn { font-size: 12.5px; }
    .net__danger { color: var(--color-neutral-400); }

    .modes__title { margin-bottom: 24px; }
    .modes { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 32px; }
    .modes__code { margin: 0 0 8px; }
    .modes__body { margin: 0; font-size: 13px; line-height: 1.7; color: var(--color-neutral-400); }
  `]
})
export class WorkersComponent implements OnInit, OnDestroy {
  private apiService = inject(ApiService);
  identityService = inject(IdentityService);
  private snackBar = inject(MatSnackBar);

  workers = signal<WorkerStatus[]>([]);
  teams = signal<Team[]>([]);
  private timer?: ReturnType<typeof setInterval>;
  switching = signal(false);
  selectedPoolCoordinator = signal<WorkerStatus | null>(null);
  poolHealth = signal<PoolHealth | null>(null);
  policyDecision = signal<string>('accept');
  policyAction = signal<string>('');
  policyLawId = signal<string>('');
  poolPolicies = signal<Record<string, PoolPolicy>>({});

  showRegisterForm = signal(false);
  newWorkerId = signal('');
  registering = signal(false);

  runningCount = computed(() => this.workers().filter((w) => w.running).length);
  soloCount = computed(() => this.workers().filter((w) => !w.team_id).length);

  /**
   * El minero de esta identidad, si ya registró alguno.
   *
   * Cada identidad registra uno solo (el backend responde 409 al segundo), así
   * que mostrar cuál es explica por qué no aparece el botón de registrar mejor
   * que hacerlo desaparecer sin más.
   */
  myWorker = computed(() => {
    const id = this.identityService.identity();
    if (!id) return null;
    return this.workers().find((w) => isOwnedBy(w, id)) ?? null;
  });

  canRegisterWorker = computed(() => {
    const id = this.identityService.identity();
    return !!id && !this.myWorker();
  });

  ngOnInit() {
    this.loadAll();
    // El estado de los mineros vive con TTL de 15 s en Redis y un cambio de modo
    // tarda unos segundos en aplicarse: sin refrescar, la pantalla muestra un
    // coordinador "sin reportar" que en realidad ya arrancó.
    this.timer = setInterval(() => this.loadAll(), 5000);
  }

  ngOnDestroy() {
    if (this.timer) clearInterval(this.timer);
  }

  /** Lleva la vista a la sección de equipos, que está en esta misma página. */
  scrollToTeams(event?: Event) {
    event?.preventDefault();
    document.getElementById('seccion-equipos')
      ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  modeCls(mode: string): string {
    return mode === 'pool-coordinator' ? 'tag-accent'
      : mode === 'standalone' ? 'tag-outline' : 'tag-neutral';
  }

  /** Una sola carga para las dos secciones de la página. */
  loadAll() {
    this.loadWorkers();
    this.apiService.listTeams().subscribe({
      next: (teams) => this.teams.set(teams),
      error: (err) => console.error('No se pudieron leer los equipos:', err),
    });
  }

  loadWorkers() {
    this.apiService.getWorkersStatus().subscribe({
      next: (data) => {
        this.workers.set(data);
        data.forEach((worker) => {
          if (worker.mode === 'pool-coordinator') this.loadPoolPolicy(worker.worker_id);
        });
      },
      error: (err) => console.error('No se pudieron leer los mineros:', err),
    });
  }

  loadPoolPolicy(poolId: string) {
    this.apiService.getPoolHealth(poolId).subscribe({
      next: (data) => {
        if (data.voting_policy) {
          this.poolPolicies.update((policies) => ({ ...policies, [poolId]: data.voting_policy }));
        }
      },
      error: (err) => console.error(`No se pudo leer la política de ${poolId}:`, err),
    });
  }

  getPolicyDisplay(worker: WorkerStatus): string {
    if (worker.mode !== 'pool-coordinator') return '—';
    const policy = this.poolPolicies()[worker.worker_id];
    if (!policy) return 'consultando…';
    const summary = this.policySummary(policy);
    return summary.charAt(0).toUpperCase() + summary.slice(1);
  }

  /** La postura de un equipo en una frase ("respalda todas", "rechaza las derogaciones"). */
  policySummary(policy: PoolPolicy | null | undefined): string {
    if (!policy || policy.decision === 'accept') return 'respalda todas';
    let label: string;
    if (!policy.action) {
      label = policy.law_id ? 'rechaza' : 'rechaza todas';
    } else {
      const actions = policy.action.split(',').map((a) => a.trim()).sort();
      label = (actions.length === 2 && actions.includes('promulgacion') && actions.includes('derogacion'))
        ? 'rechaza todas'
        : actions.includes('derogacion') ? 'rechaza las derogaciones' : 'rechaza las leyes nuevas';
    }
    if (policy.law_id) label += ` la ${policy.law_id}`;
    return label;
  }

  modeLabel = modeLabel;

  /**
   * Devuelve un minero a modo competitivo.
   *
   * El camino inverso (entrar a cooperativo) no está acá a propósito: vive en
   * Equipos, para que el modo del minero y su pertenencia a un equipo no puedan
   * moverse por separado. Salir de un equipo por este botón **sí** desarma la
   * membresía; el backend lo hace en la misma operación.
   */
  backToSolo(worker: WorkerStatus) {
    if (worker.team_role === 'coordinator') {
      this.snackBar.open(
        `${worker.worker_id} lidera "${worker.team_name}". Para sacarlo, disolvé el equipo.`,
        'Cerrar', { duration: 5000 });
      return;
    }
    this.switching.set(true);
    this.apiService.switchWorkerMode(worker.worker_id, { target: 'standalone' }).subscribe({
      next: (res: any) => {
        this.switching.set(false);
        this.loadAll();
        this.snackBar.open(
          res?.left_team
            ? `${worker.worker_id} salió del equipo y vuelve a minar por su cuenta.`
            : `${worker.worker_id} vuelve a minar por su cuenta.`,
          'Cerrar', { duration: 4000 });
      },
      error: (err) => {
        this.switching.set(false);
        this.snackBar.open(friendlyError(err, 'No se pudo sacar del equipo. Probá de nuevo.'),
          'Cerrar', { duration: 5000 });
      },
    });
  }

  openPolicyDialog(worker: WorkerStatus) {
    if (worker.mode !== 'pool-coordinator') {
      this.snackBar.open('Sólo quien lidera un equipo elige qué respalda.', 'Cerrar', { duration: 3000 });
      return;
    }
    this.selectedPoolCoordinator.set(worker);
    this.loadPoolHealth(worker.worker_id);
  }

  loadPoolHealth(poolId: string) {
    this.apiService.getPoolHealth(poolId).subscribe({
      next: (data) => {
        this.poolHealth.set(data);
        if (data.voting_policy) {
          this.policyDecision.set(data.voting_policy.decision || 'accept');
          this.policyAction.set(data.voting_policy.action || '');
          this.policyLawId.set(data.voting_policy.law_id || '');
        }
      },
      error: (err) => console.error('No se pudo leer el estado del pool:', err),
    });
  }

  cancelPolicy() {
    this.selectedPoolCoordinator.set(null);
    this.poolHealth.set(null);
    this.policyDecision.set('accept');
    this.policyAction.set('');
    this.policyLawId.set('');
  }

  confirmPolicy() {
    const pool = this.selectedPoolCoordinator();
    if (!pool) return;

    const policy: PoolPolicy = { decision: this.policyDecision() };
    if (this.policyDecision() === 'reject' && this.policyAction()) {
      policy.action = this.policyAction();
    }
    if (this.policyDecision() === 'reject' && this.policyLawId().trim()) {
      policy.law_id = this.policyLawId().trim();
    }

    this.apiService.setPoolPolicy(pool.worker_id, policy).subscribe({
      next: () => {
        this.loadPoolPolicy(pool.worker_id);
        this.cancelPolicy();
        this.snackBar.open('Listo: postura del equipo actualizada.', 'Cerrar', { duration: 3000 });
      },
      error: (err) => {
        this.snackBar.open(friendlyError(err, 'No se pudo guardar. Probá de nuevo.'),
          'Cerrar', { duration: 4000 });
      },
    });
  }

  // La verificación autoritativa la hace el backend exigiendo la firma del
  // dueño sobre la acción; acá sólo se decide qué botones mostrar. Ambos
  // helpers son compartidos con la sección de Equipos para que coincidan.
  isWorkerOwned(worker: WorkerStatus): boolean {
    return isOwnedBy(worker, this.identityService.identity());
  }

  isDynamicWorker(worker: WorkerStatus): boolean {
    return isDynamicWorker(worker);
  }

  cancelRegister() {
    this.showRegisterForm.set(false);
    this.newWorkerId.set('');
    // Se resetea también acá porque el camino de éxito cierra el formulario por
    // este método: sin esto, `registering` quedaba en true para siempre y la
    // segunda alta encontraba el botón deshabilitado diciendo "Registrando…".
    this.registering.set(false);
  }

  async confirmRegister() {
    const id = this.identityService.identity();
    if (!id) {
      this.snackBar.open('Necesitás una identidad para sumar un minero.', 'Cerrar', { duration: 3000 });
      return;
    }

    const workerId = this.newWorkerId().trim();
    if (!workerId) return;

    this.registering.set(true);
    try {
      const timestamp = new Date().toISOString();
      const signature = await this.identityService.sign(`${workerId}|register|${timestamp}`);

      // Sin clave privada: el minero genera la suya al arrancar y se vincula a
      // esta identidad con el token de enrolamiento que le deja el backend.
      this.apiService.registerWorker(workerId, id.pubkey, timestamp, signature).subscribe({
        next: (res: any) => {
          // El backend dice si además de anotarlo lo encendió. Si no pudo, el
          // alta es sólo un registro, y prometer un minero que nadie va a
          // encender deja al usuario esperando. En ese caso se le da el código
          // de activación (el token de enrolamiento), que es lo único que
          // necesita para encenderlo por su cuenta; el detalle técnico del
          // fallo queda en la consola.
          if (!res?.deployed && res?.deploy_error) console.warn('Despliegue del minero:', res.deploy_error);
          this.snackBar.open(
            res?.deployed
              ? `¡Listo! "${workerId}" ya es tuyo y arranca en unos segundos.`
              : `"${workerId}" quedó registrado a tu nombre, pero todavía no está encendido.`
                + (res?.enrollment_token ? ` Tu código de activación es ${res.enrollment_token}.` : ''),
            'Cerrar', { duration: res?.deployed ? 4000 : 15000 });
          this.cancelRegister();
          this.loadAll();
        },
        error: (err) => {
          console.error(err);
          this.snackBar.open(friendlyError(err, 'No se pudo sumar el minero. Probá de nuevo.'), 'Cerrar', { duration: 5000 });
          this.registering.set(false);
        },
      });
    } catch (err: any) {
      console.error(err);
      this.snackBar.open(friendlyError(err, 'No pudimos firmar con tu identidad. Probá de nuevo.'), 'Cerrar', { duration: 4000 });
      this.registering.set(false);
    }
  }

  async confirmUnregister(worker: WorkerStatus) {
    if (!confirm(`¿Dar de baja a "${worker.worker_id}"? Deja de minar y se borra de la red.`)) return;

    const id = this.identityService.identity();
    if (!id) {
      this.snackBar.open('Necesitás una identidad para dar de baja un minero.', 'Cerrar', { duration: 3000 });
      return;
    }

    try {
      const timestamp = new Date().toISOString();
      const signature = await this.identityService.sign(`${worker.worker_id}|delete|${timestamp}`);

      this.apiService.unregisterWorker(worker.worker_id, timestamp, signature).subscribe({
        next: () => {
          this.snackBar.open(`"${worker.worker_id}" fue dado de baja.`, 'Cerrar', { duration: 3000 });
          this.loadAll();
        },
        error: (err) => {
          console.error(err);
          this.snackBar.open(friendlyError(err, 'No se pudo dar de baja. Probá de nuevo.'), 'Cerrar', { duration: 4000 });
        },
      });
    } catch (err: any) {
      console.error(err);
      this.snackBar.open(friendlyError(err, 'No pudimos firmar con tu identidad. Probá de nuevo.'), 'Cerrar', { duration: 4000 });
    }
  }
}
