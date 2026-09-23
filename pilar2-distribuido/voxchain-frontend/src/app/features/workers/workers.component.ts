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
            Tus mineros y los equipos de la red, en un solo lugar: con quién minás y con
            qué minás son la misma decisión vista de dos lados.
          </p>
        </div>
        <div class="head__side">
          <button class="btn btn-primary head__btn" *ngIf="canRegisterWorker() && !showRegisterForm()"
                  (click)="showRegisterForm.set(true)">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" aria-hidden="true">
              <path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"></path>
            </svg>
            Registrar minero
          </button>
          <p class="vc-note-sm head__note" *ngIf="canRegisterWorker()">Cada identidad registra uno solo.</p>
          <p class="vc-note-sm head__note" *ngIf="myWorker() as mine">
            Tu minero es <code class="vc-code">{{ mine.worker_id }}</code>.
          </p>
        </div>
      </div>

      <div class="vc-stats stats">
        <div class="card elev-sm vc-stat">
          <p class="vc-stat__value">{{ workers().length }}</p>
          <p class="vc-stat__label">mineros registrados</p>
        </div>
        <div class="card elev-sm vc-stat">
          <p class="vc-stat__value vc-stat__value--accent">{{ runningCount() }}</p>
          <p class="vc-stat__label">corriendo</p>
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
        <span class="card-kicker">Registrar un nodo propio</span>
        <p class="panel__sub">
          Registrar un ID de minero lo asocia a tu clave pública, y cada acción de
          administración sobre él (cambiar el modo, configurar la política, moverlo de
          equipo) se firma con tu clave. El minero genera su propia identidad al
          arrancar: tu clave privada no se comparte con él ni con nadie.
        </p>
        <div class="field panel__field">
          <label for="vc-worker-id">ID del minero</label>
          <input id="vc-worker-id" class="input" placeholder="Ej: minero-ciudadano-1"
                 [value]="newWorkerId()" (input)="newWorkerId.set($any($event.target).value)">
          <p class="vc-note-sm panel__hint">Elegí un nombre único para identificar tu contenedor.</p>
        </div>
        <div class="vc-actions panel__actions">
          <button class="btn btn-secondary" (click)="cancelRegister()">Cancelar</button>
          <button class="btn btn-primary" (click)="confirmRegister()"
                  [disabled]="!newWorkerId().trim() || registering()">
            {{ registering() ? 'Registrando…' : 'Registrar minero' }}
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
                <th>Clave pública</th>
                <th>Modo</th>
                <th>Equipo</th>
                <th>Política</th>
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
                <td class="mono net__key" [title]="w.pubkey || ''">
                  {{ w.pubkey ? w.pubkey.slice(0, 12) + '…' : '—' }}
                </td>
                <td><span class="tag" [ngClass]="modeCls(w.mode)">{{ w.mode }}</span></td>
                <td class="net__team">
                  <ng-container *ngIf="w.team_id; else solo">
                    <a href="#seccion-equipos" (click)="scrollToTeams($event)">{{ w.team_name }}</a>
                    <span class="net__role">{{ w.team_role === 'coordinator' ? 'coordina' : 'mina' }}</span>
                  </ng-container>
                  <ng-template #solo><span class="vc-muted">por su cuenta</span></ng-template>
                </td>
                <td class="net__policy" [class.empty]="getPolicyDisplay(w) === '—'">{{ getPolicyDisplay(w) }}</td>
                <td>
                  <span class="vc-state" [class.online]="w.running">
                    <span class="dot" [class.online]="w.running"></span>
                    {{ w.running ? 'corriendo' : 'sin arrancar' }}
                  </span>
                  <span class="vc-note-sm net__pending" *ngIf="!w.running && isWorkerOwned(w)">
                    registrado, falta levantarlo
                  </span>
                </td>
                <td class="num">
                  <span class="net__actions">
                    <button class="btn btn-ghost net__btn" *ngIf="isWorkerOwned(w) && w.team_id"
                            (click)="backToSolo(w)" [disabled]="switching()"
                            title="Sacarlo del equipo y devolverlo a modo competitivo">Volver a competitivo</button>
                    <button class="btn btn-ghost net__btn" *ngIf="isWorkerOwned(w) && !w.team_id"
                            (click)="scrollToTeams($event)">Sumar a un equipo</button>
                    <button class="btn btn-ghost net__btn"
                            *ngIf="w.mode === 'pool-coordinator' && isWorkerOwned(w)"
                            (click)="openPolicyDialog(w)">Política</button>
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
        <span class="card-kicker">Política de voto de {{ pool.worker_id }}</span>
        <p class="panel__sub" *ngIf="poolHealth() as h">
          {{ h.miners }} minero(s) conectados · política actual
          <code class="vc-code">{{ h.voting_policy.decision }}</code>
        </p>

        <div class="field panel__field">
          <label for="vc-decision">Decisión</label>
          <select id="vc-decision" class="input" [value]="policyDecision()"
                  (change)="policyDecision.set($any($event.target).value)">
            <option value="accept">Aceptar todas</option>
            <option value="reject">Rechazar específica</option>
          </select>
        </div>

        <ng-container *ngIf="policyDecision() === 'reject'">
          <div class="field panel__field">
            <label for="vc-pol-action">Acción a rechazar</label>
            <select id="vc-pol-action" class="input" [value]="policyAction()"
                    (change)="policyAction.set($any($event.target).value)">
              <option value="">Rechazar todas</option>
              <option value="promulgacion">Promulgación</option>
              <option value="derogacion">Derogación</option>
            </select>
          </div>
          <div class="field panel__field">
            <label for="vc-pol-law">ID de ley a rechazar (opcional)</label>
            <input id="vc-pol-law" class="input" placeholder="Vacío para rechazar sólo por acción"
                   [value]="policyLawId()" (input)="policyLawId.set($any($event.target).value)">
          </div>
        </ng-container>

        <div class="vc-actions panel__actions">
          <button class="btn btn-secondary" (click)="cancelPolicy()">Cancelar</button>
          <button class="btn btn-primary" (click)="confirmPolicy()">Guardar política</button>
        </div>
      </div>

      <!-- ── modos ───────────────────────────────────────────────────────── -->
      <section class="vc-section--divided">
        <h3 class="vc-h3 vc-h3--sm modes__title">Modos de minero</h3>
        <div class="modes">
          <div>
            <p class="modes__code"><code class="vc-code">standalone</code></p>
            <h5 class="vc-h5">Competitivo</h5>
            <p class="modes__body">
              Trabaja por su cuenta, suscrito a los desafíos del NCT. Barre el espacio de
              nonces completo y compite contra toda la red. Sumar mineros acá no acelera
              nada: todos hacen el mismo trabajo.
            </p>
          </div>
          <div>
            <p class="modes__code"><code class="vc-code">pool-coordinator</code></p>
            <h5 class="vc-h5">Coordinador del equipo</h5>
            <p class="modes__body">
              Fragmenta el espacio de nonces, reparte los fragmentos entre los mineros del
              equipo y además mina.
            </p>
          </div>
          <div>
            <p class="modes__code"><code class="vc-code">pool-worker</code></p>
            <h5 class="vc-h5">Minero del equipo</h5>
            <p class="modes__body">
              Le pide fragmentos al coordinador y mina sólo el rango que le toca. Acá sí,
              cada minero que se suma divide el trabajo.
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
    .net__table { min-width: 1040px; white-space: nowrap; }
    .net__mine { margin-left: 8px; }
    .net__key { font-size: 12px; color: var(--color-neutral-500); }
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
    if (!id || id.isDemo) return null;
    return this.workers().find((w) => isOwnedBy(w, id)) ?? null;
  });

  canRegisterWorker = computed(() => {
    const id = this.identityService.identity();
    return !!id && !id.isDemo && !this.myWorker();
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
    if (!policy) return 'leyendo…';
    if (policy.decision === 'accept') return 'Acepta todas';
    if (!policy.action && !policy.law_id) return 'Rechaza todas';

    let label: string;
    if (!policy.action) {
      label = 'Rechaza todas';
    } else {
      const actions = policy.action.split(',').map((a) => a.trim()).sort();
      label = (actions.length === 2 && actions.includes('promulgacion') && actions.includes('derogacion'))
        ? 'Rechaza todas'
        : `Rechaza: ${policy.action}`;
    }
    if (policy.law_id) label += ` (ley ${policy.law_id})`;
    return label;
  }

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
        `${worker.worker_id} coordina "${worker.team_name}". Disolvé el equipo desde Equipos.`,
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
            : `${worker.worker_id} vuelve a modo competitivo.`,
          'Cerrar', { duration: 4000 });
      },
      error: (err) => {
        this.switching.set(false);
        this.snackBar.open('No se pudo cambiar el modo: ' + (err.error?.detail || err.message),
          'Cerrar', { duration: 5000 });
      },
    });
  }

  openPolicyDialog(worker: WorkerStatus) {
    if (worker.mode !== 'pool-coordinator') {
      this.snackBar.open('Sólo los coordinadores de pool tienen política de voto.', 'Cerrar', { duration: 3000 });
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
        this.snackBar.open('Política del pool actualizada.', 'Cerrar', { duration: 3000 });
      },
      error: (err) => {
        this.snackBar.open('No se pudo fijar la política: ' + (err.error?.detail || err.message),
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
    if (!id || id.isDemo) {
      this.snackBar.open('Sólo las identidades propias pueden registrar nodos.', 'Cerrar', { duration: 3000 });
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
          // El backend dice si además de anotarlo levantó un proceso, y dónde.
          // Si no pudo (sin Kubernetes ni socket de Docker), el alta es sólo
          // metadata, y prometer un despliegue que no ocurrió deja al usuario
          // esperando un contenedor que nadie va a crear.
          const where = res?.deployed_on === 'docker' ? 'levantado en Docker' : 'desplegado en el clúster';
          this.snackBar.open(
            res?.deployed
              ? `Minero "${workerId}" registrado y ${where}. Arranca en unos segundos.`
              : `Minero "${workerId}" registrado. Todavía no está corriendo`
                + (res?.deploy_error ? ` (${res.deploy_error})` : '') + ': '
                + `levantalo con  WORKER_ENROLL_TOKEN=${res?.enrollment_token ?? ''} ./run.sh worker ${workerId}`,
            'Cerrar', { duration: res?.deployed ? 4000 : 15000 });
          this.cancelRegister();
          this.loadAll();
        },
        error: (err) => {
          console.error(err);
          this.snackBar.open('Falló el registro: ' + (err.error?.detail || err.message), 'Cerrar', { duration: 4000 });
          this.registering.set(false);
        },
      });
    } catch (err: any) {
      console.error(err);
      this.snackBar.open('Falló la firma: ' + err.message, 'Cerrar', { duration: 4000 });
      this.registering.set(false);
    }
  }

  async confirmUnregister(worker: WorkerStatus) {
    if (!confirm(`¿Dar de baja al minero "${worker.worker_id}"?`)) return;

    const id = this.identityService.identity();
    if (!id || id.isDemo) {
      this.snackBar.open('Sólo las identidades propias pueden dar de baja nodos.', 'Cerrar', { duration: 3000 });
      return;
    }

    try {
      const timestamp = new Date().toISOString();
      const signature = await this.identityService.sign(`${worker.worker_id}|delete|${timestamp}`);

      this.apiService.unregisterWorker(worker.worker_id, timestamp, signature).subscribe({
        next: () => {
          this.snackBar.open(`Minero "${worker.worker_id}" dado de baja.`, 'Cerrar', { duration: 3000 });
          this.loadAll();
        },
        error: (err) => {
          console.error(err);
          this.snackBar.open('No se pudo dar de baja: ' + (err.error?.detail || err.message), 'Cerrar', { duration: 4000 });
        },
      });
    } catch (err: any) {
      console.error(err);
      this.snackBar.open('Falló la firma: ' + err.message, 'Cerrar', { duration: 4000 });
    }
  }
}
