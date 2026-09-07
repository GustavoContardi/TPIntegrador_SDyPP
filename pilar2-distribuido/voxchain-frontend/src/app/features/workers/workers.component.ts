import { Component, OnDestroy, OnInit, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatSelectModule } from '@angular/material/select';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatTableModule } from '@angular/material/table';
import { MatIconModule } from '@angular/material/icon';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { RouterModule } from '@angular/router';
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

@Component({
  selector: 'app-workers',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatCardModule,
    MatButtonModule,
    MatSelectModule,
    MatFormFieldModule,
    MatInputModule,
    MatTableModule,
    MatIconModule,
    MatSnackBarModule,
    RouterModule,
    TeamsComponent,
  ],
  template: `
    <div class="workers-container">
      <div class="header-container">
        <div>
          <h1>Minería</h1>
          <p class="page-subtitle">
            Tus mineros y los equipos de la red, en un solo lugar: con quién minás
            y con qué minás son la misma decisión vista de dos lados.
          </p>
        </div>
        <button
          mat-raised-button
          color="accent"
          (click)="showRegisterForm.set(true)"
          *ngIf="canRegisterWorker() && !showRegisterForm()">
          <svg class="btn-svg" viewBox="0 0 24 24" fill="currentColor">
            <path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/>
          </svg>
          Registrar minero
        </button>
        <p class="already-registered" *ngIf="myWorker() as mine">
          Tu minero es <code>{{ mine.worker_id }}</code>.<br>
          <span class="muted">Cada identidad registra uno solo.</span>
        </p>
      </div>

      <!-- Register New Worker Card -->
      <mat-card class="register-card" *ngIf="showRegisterForm()">
        <mat-card-header>
          <mat-card-title>Registrar un nodo propio (minero)</mat-card-title>
          <mat-card-subtitle>Vincula criptográficamente un minero a tu identidad</mat-card-subtitle>
        </mat-card-header>
        <mat-card-content>
          <p class="form-hint">
            Registrar un ID de minero lo asocia a tu clave pública. Cualquier acción de administración (cambiar el modo o configurar la política) va a pedir tu firma. 
            El contenedor del minero tiene que correr con <code>WORKER_PRIVKEY_PEM</code> apuntando a tu clave privada.
          </p>
          <div class="form-field">
            <mat-form-field appearance="outline" class="full-width">
              <mat-label>ID del minero</mat-label>
              <input matInput [(ngModel)]="newWorkerId" placeholder="Ej: minero-ciudadano-1">
              <mat-hint>Elegí un nombre único para identificar tu contenedor</mat-hint>
            </mat-form-field>
          </div>
        </mat-card-content>
        <mat-card-actions class="form-actions">
          <button mat-button (click)="cancelRegister()">Cancelar</button>
          <button mat-raised-button color="accent" (click)="confirmRegister()" [disabled]="!newWorkerId().trim() || registering()">
            {{ registering() ? 'Registrando…' : 'Registrar minero' }}
          </button>
        </mat-card-actions>
      </mat-card>
      
      <!-- Equipos (modo cooperativo) -->
      <app-teams
        id="seccion-equipos"
        [teams]="teams()"
        [workers]="workers()"
        (changed)="loadAll()">
      </app-teams>

      <!-- Worker Status Card -->
      <mat-card class="workers-card">
        <mat-card-header>
          <mat-card-title>Mineros activos y registrados</mat-card-title>
        </mat-card-header>
        <mat-card-content>
          <div class="table-container">
            <table mat-table [dataSource]="workers()">
              <ng-container matColumnDef="worker_id">
                <th mat-header-cell *matHeaderCellDef>ID del minero</th>
                <td mat-cell *matCellDef="let worker">
                  <div class="worker-id-wrapper">
                    {{ worker.worker_id }}
                    <span class="owner-badge" *ngIf="isWorkerOwned(worker)">Mío</span>
                  </div>
                </td>
              </ng-container>

              <ng-container matColumnDef="pubkey">
                <th mat-header-cell *matHeaderCellDef>Clave pública</th>
                <td mat-cell *matCellDef="let worker">
                  <span *ngIf="worker.pubkey" class="pubkey-text" [title]="worker.pubkey">
                    {{ worker.pubkey.slice(0, 16) }}...
                  </span>
                  <span *ngIf="!worker.pubkey" class="empty-text">-</span>
                </td>
              </ng-container>

              <ng-container matColumnDef="mode">
                <th mat-header-cell *matHeaderCellDef>Modo</th>
                <td mat-cell *matCellDef="let worker">
                  <span [class.mode-badge]="true" [class.mode-standalone]="worker.mode === 'standalone'" 
                        [class.mode-pool-coordinator]="worker.mode === 'pool-coordinator'"
                        [class.mode-pool-worker]="worker.mode === 'pool-worker'">
                    {{ worker.mode }}
                  </span>
                </td>
              </ng-container>

              <ng-container matColumnDef="team">
                <th mat-header-cell *matHeaderCellDef>Equipo</th>
                <td mat-cell *matCellDef="let worker">
                  <a *ngIf="worker.team_id" class="team-link" (click)="scrollToTeams()"
                     [title]="'Coordinador en ' + (worker.pool_url || 'dirección aún desconocida')">
                    {{ worker.team_name }}
                    <span class="role-badge" [class.coord]="worker.team_role === 'coordinator'">
                      {{ worker.team_role === 'coordinator' ? 'coordina' : 'mina' }}
                    </span>
                  </a>
                  <span *ngIf="!worker.team_id" class="empty-text">por su cuenta</span>
                </td>
              </ng-container>

              <ng-container matColumnDef="policy">
                <th mat-header-cell *matHeaderCellDef>Política</th>
                <td mat-cell *matCellDef="let worker">
                  <span [class.policy-badge]="true" [class.policy-accept]="getPolicyDisplay(worker) === 'Accept All'"
                        [class.policy-reject]="getPolicyDisplay(worker).startsWith('Reject')">
                    {{ getPolicyDisplay(worker) }}
                  </span>
                </td>
              </ng-container>

              <ng-container matColumnDef="running">
                <th mat-header-cell *matHeaderCellDef>Estado</th>
                <td mat-cell *matCellDef="let worker">
                  <span [class.running-text]="worker.running"
                        [class.stopped-text]="!worker.running">
                    {{ worker.running ? 'Corriendo' : 'Sin arrancar' }}
                  </span>
                  <span class="pending-note" *ngIf="!worker.running && isWorkerOwned(worker)"
                        title="Registrarlo lo anota en la red; para que mine hay que levantar su contenedor">
                    registrado, falta levantarlo
                  </span>
                </td>
              </ng-container>

              <ng-container matColumnDef="actions">
                <th mat-header-cell *matHeaderCellDef>Acciones</th>
                <td mat-cell *matCellDef="let worker">
                  <div class="actions-cell">
                    <button
                      mat-button
                      (click)="backToSolo(worker)"
                      *ngIf="isWorkerOwned(worker) && worker.team_id"
                      [disabled]="switching()"
                      title="Sacarlo del equipo y devolverlo a modo competitivo">
                      Volver a competitivo
                    </button>
                    <button
                      mat-button
                      (click)="scrollToTeams()"
                      *ngIf="isWorkerOwned(worker) && !worker.team_id"
                      title="Los equipos se administran en la sección de arriba">
                      Sumar a un equipo
                    </button>
                    <button 
                      mat-button 
                      (click)="openPolicyDialog(worker)" 
                      *ngIf="worker.mode === 'pool-coordinator' && isWorkerOwned(worker)"
                      title="Configure voting policies">
                      Configure Policy
                    </button>
                    <button 
                      mat-button 
                      color="warn" 
                      (click)="confirmUnregister(worker)" 
                      *ngIf="isDynamicWorker(worker) && isWorkerOwned(worker)"
                      title="Unregister this worker from the network">
                      Unregister
                    </button>
                  </div>
                </td>
              </ng-container>

              <tr mat-header-row *matHeaderRowDef="displayedColumns"></tr>
              <tr mat-row *matRowDef="let row; columns: displayedColumns;"></tr>
            </table>
          </div>
        </mat-card-content>
      </mat-card>

      <!-- Configure Policy Form -->
      <mat-card class="policy-card" *ngIf="selectedPoolCoordinator()">
        <mat-card-header>
          <mat-card-title>Configurar la política de voto del pool</mat-card-title>
        </mat-card-header>
        <mat-card-content>
          <p><strong>Coordinador del pool:</strong> {{ selectedPoolCoordinator()?.worker_id }}</p>
          <div *ngIf="poolHealth()">
            <p><strong>Mineros conectados:</strong> {{ poolHealth()?.miners }}</p>
            <p><strong>Política actual:</strong> {{ poolHealth()?.voting_policy?.decision }}</p>
          </div>

          <div class="form-field">
            <mat-form-field appearance="outline" class="full-width">
              <mat-label>Decisión</mat-label>
              <mat-select [(value)]="policyDecision">
                <mat-option value="accept">Aceptar todas</mat-option>
                <mat-option value="reject">Rechazar específica</mat-option>
              </mat-select>
            </mat-form-field>
          </div>

          <div class="form-field" *ngIf="policyDecision() === 'reject'">
            <mat-form-field appearance="outline" class="full-width">
              <mat-label>Acción a rechazar</mat-label>
              <mat-select [(value)]="policyAction">
                <mat-option value="promulgacion">Promulgación</mat-option>
                <mat-option value="derogacion">Derogación</mat-option>
                <mat-option value="">Rechazar todas</mat-option>
              </mat-select>
            </mat-form-field>
          </div>

          <div class="form-field" *ngIf="policyDecision() === 'reject'">
            <mat-form-field appearance="outline" class="full-width">
              <mat-label>ID de ley a rechazar (opcional)</mat-label>
              <input matInput [(ngModel)]="policyLawId" placeholder="Vacío para rechazar sólo por acción">
            </mat-form-field>
          </div>
        </mat-card-content>
        <mat-card-actions class="form-actions">
          <button mat-button (click)="cancelPolicy()">Cancelar</button>
          <button mat-raised-button color="primary" (click)="confirmPolicy()">
            Update Policy
          </button>
        </mat-card-actions>
      </mat-card>

      <div class="info-section">
        <h3>Modos de minero</h3>
        <ul>
          <li><strong>Competitivo</strong> (<code>standalone</code>): el minero trabaja por su cuenta,
            suscrito directamente a los desafíos del NCT. Barre el espacio de nonces completo y
            compite contra toda la red. Sumar mineros acá no acelera nada: todos hacen el mismo
            trabajo y encuentran el mismo nonce.</li>
          <li><strong>Coordinador del equipo</strong> (<code>pool-coordinator</code>): fragmenta el
            espacio de nonces, reparte los fragmentos entre los mineros del equipo y además mina.</li>
          <li><strong>Minero del equipo</strong> (<code>pool-worker</code>): le pide fragmentos al
            coordinador y mina sólo el rango que le toca. Acá sí, cada minero que se suma divide el
            trabajo.</li>
        </ul>
        <p class="info-note">
          El modo cooperativo se administra desde <a routerLink="/teams">Equipos</a>: creás uno
          (tu minero pasa a coordinarlo) o te unís al de otro. La dirección del coordinador la
          resuelve el sistema con la que el propio minero publica, así que no hay que escribir
          ninguna URL — y el modo del minero nunca queda desalineado de su equipo.
        </p>
      </div>
    </div>
  `,
  styles: [`
    .workers-container {
      padding: 40px 20px;
      max-width: 1400px;
      margin: 0 auto;
    }
    .header-container {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 24px;
      margin-bottom: 32px;
    }
    .page-subtitle {
      color: #888;
      margin: 8px 0 0;
      max-width: 74ch;
      line-height: 1.5;
    }
    .btn-svg {
      width: 14px;
      height: 14px;
      margin-right: 6px;
      vertical-align: middle;
      display: inline-block;
    }
    h1 {
      color: #e0e0e0;
      margin: 0;
      font-weight: 500;
    }
    .already-registered { color: #b0b0b0; font-size: 0.85rem; text-align: right; margin: 0; line-height: 1.6; }
    .already-registered .muted { color: #777; font-size: 0.8rem; }
    .register-card, .workers-card, .policy-card {
      background-color: #1e1e1e;
      color: #e0e0e0;
      border: 1px solid #333;
      border-radius: 8px;
      margin-bottom: 24px;
      padding: 16px;
    }
    mat-card-title {
      color: #e0e0e0;
      font-size: 1.25rem;
    }
    mat-card-subtitle {
      color: #888;
    }
    .form-hint {
      color: #aaa;
      font-size: 0.95rem;
      line-height: 1.5;
      margin-bottom: 20px;
    }
    .form-hint code {
      background-color: rgba(0, 0, 0, 0.3);
      padding: 2px 6px;
      border-radius: 4px;
      color: #fff;
    }
    .form-field {
      margin: 16px 0;
    }
    .full-width {
      width: 100%;
    }
    .form-actions {
      display: flex;
      justify-content: flex-end;
      gap: 12px;
      padding: 0;
    }
    .table-container {
      overflow-x: auto;
      margin-top: 16px;
    }
    table {
      width: 100%;
      background: transparent;
    }
    th.mat-mdc-header-cell {
      color: #e0e0e0;
      font-weight: 600;
      font-size: 0.95rem;
      border-bottom: 1px solid #333 !important;
      padding: 16px;
      vertical-align: middle !important;
    }
    td.mat-mdc-cell {
      color: #b0b0b0;
      font-size: 0.9rem;
      border-bottom: 1px solid #222 !important;
      padding: 16px;
      vertical-align: middle !important;
    }
    tr.mat-mdc-row:hover {
      background-color: rgba(255, 255, 255, 0.03);
    }
    .worker-id-wrapper {
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }
    .owner-badge {
      background-color: rgba(255, 152, 0, 0.15);
      color: #ffb74d;
      font-size: 0.75rem;
      padding: 2px 8px;
      border-radius: 12px;
      border: 1px solid rgba(255, 152, 0, 0.3);
      font-weight: 600;
    }
    .pubkey-text {
      font-family: 'Courier New', monospace;
      color: #90caf9;
      background-color: #0c0c0c;
      padding: 4px 8px;
      border-radius: 4px;
      border: 1px solid #222;
    }
    .empty-text {
      color: #555;
    }
    .pending-note {
      display: block;
      color: #777;
      font-size: 0.72rem;
      margin-top: 4px;
    }
    .mode-badge {
      padding: 4px 8px;
      border-radius: 4px;
      font-size: 0.85em;
      font-weight: bold;
    }
    .mode-standalone {
      background-color: rgba(33, 150, 243, 0.15);
      color: #64b5f6;
      border: 1px solid rgba(33, 150, 243, 0.3);
    }
    .mode-pool-coordinator {
      background-color: rgba(255, 152, 0, 0.15);
      color: #ffb74d;
      border: 1px solid rgba(255, 152, 0, 0.3);
    }
    .mode-pool-worker {
      background-color: rgba(76, 175, 80, 0.15);
      color: #81c784;
      border: 1px solid rgba(76, 175, 80, 0.3);
    }
    .policy-badge {
      padding: 4px 8px;
      border-radius: 4px;
      font-size: 0.85em;
      font-weight: bold;
    }
    .policy-accept {
      background-color: rgba(76, 175, 80, 0.15);
      color: #81c784;
    }
    .policy-reject {
      background-color: rgba(244, 67, 54, 0.15);
      color: #e57373;
    }
    .running-text {
      color: #81c784;
      font-weight: bold;
    }
    .stopped-text {
      color: #e57373;
      font-weight: bold;
    }
    .actions-cell {
      display: flex;
      gap: 8px;
    }
    .info-section {
      background-color: #1e1e1e;
      color: #e0e0e0;
      padding: 24px;
      border-radius: 8px;
      border: 1px solid #333;
    }
    .info-section h3 {
      color: #e0e0e0;
      margin-top: 0;
    }
    .info-section ul {
      margin: 0;
      padding-left: 20px;
      color: #b0b0b0;
    }
    .info-section li {
      margin: 12px 0;
      line-height: 1.6;
    }
    .info-section code {
      background-color: rgba(0, 0, 0, 0.3);
      padding: 2px 6px;
      border-radius: 4px;
      color: #fff;
      font-size: 0.9em;
    }
    .info-note {
      color: #b0b0b0;
      line-height: 1.6;
      margin: 16px 0 0;
      padding-top: 16px;
      border-top: 1px solid #2a2a2a;
    }
    .info-note a, .team-link {
      color: #90caf9;
      text-decoration: none;
    }
    .info-note a:hover, .team-link:hover {
      text-decoration: underline;
    }
    .team-link {
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }
    .role-badge {
      font-size: 0.7rem;
      padding: 2px 7px;
      border-radius: 4px;
      background-color: rgba(76, 175, 80, 0.15);
      color: #81c784;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .role-badge.coord {
      background-color: rgba(255, 152, 0, 0.15);
      color: #ffb74d;
    }
  `]
})
export class WorkersComponent implements OnInit, OnDestroy {
  private apiService = inject(ApiService);
  identityService = inject(IdentityService);
  private snackBar = inject(MatSnackBar);

  workers = signal<WorkerStatus[]>([]);
  teams = signal<Team[]>([]);
  private timer?: ReturnType<typeof setInterval>;
  displayedColumns: string[] = ['worker_id', 'pubkey', 'mode', 'team', 'policy', 'running', 'actions'];
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
  scrollToTeams() {
    document.getElementById('seccion-equipos')
      ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  /** Una sola carga para las dos secciones de la página. */
  loadAll() {
    this.loadWorkers();
    this.apiService.listTeams().subscribe({
      next: (teams) => this.teams.set(teams),
      error: (err) => console.error('Failed to load teams:', err),
    });
  }

  loadWorkers() {
    this.apiService.getWorkersStatus().subscribe({
      next: (data) => {
        this.workers.set(data);
        // Load policies for pool coordinators
        data.forEach(worker => {
          if (worker.mode === 'pool-coordinator') {
            this.loadPoolPolicy(worker.worker_id);
          }
        });
      },
      error: (err) => {
        console.error('Failed to load workers:', err);
      }
    });
  }

  loadPoolPolicy(poolId: string) {
    this.apiService.getPoolHealth(poolId).subscribe({
      next: (data) => {
        if (data.voting_policy) {
          this.poolPolicies.update(policies => ({
            ...policies,
            [poolId]: data.voting_policy
          }));
        }
      },
      error: (err) => {
        console.error(`Failed to load policy for ${poolId}:`, err);
      }
    });
  }

  getPolicyDisplay(worker: WorkerStatus): string {
    if (worker.mode !== 'pool-coordinator') {
      return '-';
    }
    const policy = this.poolPolicies()[worker.worker_id];
    if (!policy) {
      return 'Loading...';
    }
    if (policy.decision === 'accept') {
      return 'Accept All';
    }
    if (!policy.action && !policy.law_id) {
      return 'Reject All';
    }
    let label: string;
    if (!policy.action) {
      label = 'Reject All';
    } else {
      const actions = policy.action.split(',').map(a => a.trim()).sort();
      label = (actions.length === 2 && actions.includes('promulgacion') && actions.includes('derogacion'))
        ? 'Reject All'
        : `Reject: ${policy.action}`;
    }
    if (policy.law_id) {
      label += ` (law ${policy.law_id})`;
    }
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
      }
    });
  }

  openPolicyDialog(worker: WorkerStatus) {
    if (worker.mode !== 'pool-coordinator') {
      this.snackBar.open('Sólo los coordinadores de pool tienen política de voto', 'Cerrar', { duration: 3000 });
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
      error: (err) => {
        console.error('Failed to load pool health:', err);
      }
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

    const policy: PoolPolicy = {
      decision: this.policyDecision(),
    };

    if (this.policyDecision() === 'reject' && this.policyAction()) {
      policy.action = this.policyAction();
    }

    if (this.policyDecision() === 'reject' && this.policyLawId().trim()) {
      policy.law_id = this.policyLawId().trim();
    }

    this.apiService.setPoolPolicy(pool.worker_id, policy).subscribe({
      next: () => {
        this.loadPoolHealth(pool.worker_id);
        this.loadPoolPolicy(pool.worker_id);
        this.cancelPolicy();
        this.snackBar.open('Política del pool actualizada', 'Cerrar', { duration: 3000 });
      },
      error: (err) => {
        console.error('Failed to set pool policy:', err);
        this.snackBar.open('No se pudo fijar la política del pool: ' + (err.error?.detail || err.message), 'Cerrar', { duration: 4000 });
      }
    });
  }

  // La verificación autoritativa la hace el backend (X-Owner-Id contra Redis);
  // acá sólo se decide qué botones mostrar. Ambos helpers son compartidos con
  // la pantalla de Equipos para que las dos coincidan siempre.
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
      const message = `${workerId}|register|${timestamp}`;
      const signature = await this.identityService.sign(message);

      let pemKey: string | undefined = undefined;
      if (id.exportedPrivkey) {
        const exportedPrivkey = id.exportedPrivkey;
        const lines = [];
        for (let i = 0; i < exportedPrivkey.length; i += 64) {
          lines.push(exportedPrivkey.slice(i, i + 64));
        }
        pemKey = `-----BEGIN PRIVATE KEY-----\n${lines.join('\n')}\n-----END PRIVATE KEY-----`;
      }

      this.apiService.registerWorker(workerId, id.pubkey, timestamp, signature, pemKey).subscribe({
        next: (res: any) => {
          // El backend dice si además de anotarlo levantó un proceso. Sin
          // Kubernetes configurado (el compose local) el alta es sólo metadata,
          // y prometer un despliegue que no ocurrió deja al usuario esperando
          // un contenedor que nadie va a crear.
          this.snackBar.open(
            res?.deployed
              ? `Minero "${workerId}" registrado y desplegado en el clúster.`
              : `Minero "${workerId}" registrado. Todavía no está corriendo: `
                + `levantá su contenedor con  ./run.sh worker ${workerId}`,
            'Cerrar', { duration: res?.deployed ? 3000 : 9000 });
          this.cancelRegister();
          this.loadAll();
        },
        error: (err) => {
          console.error(err);
          this.snackBar.open('Falló el registro: ' + (err.error?.detail || err.message), 'Cerrar', { duration: 4000 });
          this.registering.set(false);
        }
      });
    } catch (err: any) {
      console.error(err);
      this.snackBar.open('Falló la firma: ' + err.message, 'Cerrar', { duration: 4000 });
      this.registering.set(false);
    }
  }

  async confirmUnregister(worker: WorkerStatus) {
    if (!confirm(`Are you sure you want to unregister worker "${worker.worker_id}"?`)) {
      return;
    }

    const id = this.identityService.identity();
    if (!id || id.isDemo) {
      this.snackBar.open('Sólo las identidades propias pueden dar de baja nodos.', 'Cerrar', { duration: 3000 });
      return;
    }

    try {
      const timestamp = new Date().toISOString();
      const message = `${worker.worker_id}|delete|${timestamp}`;
      const signature = await this.identityService.sign(message);

      this.apiService.unregisterWorker(worker.worker_id, timestamp, signature).subscribe({
        next: () => {
          this.snackBar.open(`Minero "${worker.worker_id}" dado de baja.`, 'Cerrar', { duration: 3000 });
          this.loadAll();
        },
        error: (err) => {
          console.error(err);
          this.snackBar.open('No se pudo dar de baja: ' + (err.error?.detail || err.message), 'Cerrar', { duration: 4000 });
        }
      });
    } catch (err: any) {
      console.error(err);
      this.snackBar.open('Falló la firma: ' + err.message, 'Cerrar', { duration: 4000 });
    }
  }
}
