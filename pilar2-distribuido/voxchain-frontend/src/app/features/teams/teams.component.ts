import { Component, computed, inject, input, output, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { ApiService } from '../../core/services/api.service';
import { IdentityService } from '../../core/services/identity.service';
import {
  Team,
  WorkerRegistration,
  WorkerStatus,
  buildRegistration,
  isOwnedBy,
} from '../../core/models/worker.model';

/**
 * Equipos de minado: la cara de usuario del modo cooperativo.
 *
 * Vive **dentro** de la página de Mineros, no como pantalla aparte: equipos y
 * mineros son la misma decisión vista de dos lados (con quién mino / con qué
 * mino), y separarlos obligaba a saltar de pantalla para entender el estado de
 * un solo minero.
 *
 * Es un componente presentacional en cuanto a los datos —los recibe por input y
 * avisa con `changed` cuando hay que recargarlos— pero sí ejecuta las acciones
 * (crear, unirse, salir, disolver), porque son suyas y no del contenedor.
 */
@Component({
  selector: 'app-teams',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatButtonModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatSelectModule,
    MatSnackBarModule,
  ],
  template: `
    <section class="teams-section">
      <div class="section-head">
        <div>
          <h2>
            Equipos
            <span class="count">{{ teams().length }}</span>
          </h2>
          <p class="subtitle">
            Un equipo reparte el espacio de nonces entre todos sus mineros: cada uno
            barre un tramo distinto y el trabajo se divide de verdad. Minar por cuenta
            propia es competir contra toda la red haciendo el mismo cálculo que todos.
          </p>
        </div>
        <button mat-raised-button color="accent"
                (click)="openCreate()"
                *ngIf="canFound() && !creating()">
          Fundar equipo
        </button>
        <p class="already" *ngIf="myTeam() as mine">
          Fundaste <strong>{{ mine.name }}</strong>.<br>
          <span class="muted">Cada identidad funda un solo equipo.</span>
        </p>
      </div>

      <!-- Fundar -->
      <mat-card class="panel" *ngIf="creating()">
        <mat-card-header>
          <mat-card-title>Fundar un equipo</mat-card-title>
          <mat-card-subtitle>
            Uno de tus mineros pasa a ser el coordinador: reparte el trabajo y además mina.
          </mat-card-subtitle>
        </mat-card-header>
        <mat-card-content>
          <mat-form-field appearance="outline" class="full-width">
            <mat-label>Nombre del equipo</mat-label>
            <input matInput [(ngModel)]="newTeamName" placeholder="Ej: Los Pibes del Barrio">
          </mat-form-field>

          <ng-container *ngIf="freeWorkers().length > 0">
            <mat-form-field appearance="outline" class="full-width">
              <mat-label>Minero que va a coordinar</mat-label>
              <mat-select [(value)]="coordinatorWorkerId">
                <mat-option *ngFor="let w of freeWorkers()" [value]="w.worker_id">
                  {{ w.worker_id }}<span *ngIf="!w.running"> (detenido)</span>
                </mat-option>
                <mat-option value="__new__">Registrar un minero nuevo…</mat-option>
              </mat-select>
            </mat-form-field>
          </ng-container>

          <p class="hint" *ngIf="freeWorkers().length === 0">
            Todavía no tenés ningún minero libre, así que lo damos de alta ahora:
            elegí un identificador y se despliega junto con el equipo.
          </p>

          <mat-form-field appearance="outline" class="full-width" *ngIf="needsNewWorker()">
            <mat-label>ID del minero nuevo</mat-label>
            <input matInput [(ngModel)]="newWorkerId" placeholder="Ej: minero-gustavo-1">
            <mat-hint>Se registra a tu nombre y se despliega en el clúster</mat-hint>
          </mat-form-field>
        </mat-card-content>
        <mat-card-actions class="actions">
          <button mat-button (click)="creating.set(false)">Cancelar</button>
          <button mat-raised-button color="accent"
                  (click)="confirmCreate()"
                  [disabled]="!canCreate() || busy()">
            {{ busy() ? 'Fundando…' : 'Fundar equipo' }}
          </button>
        </mat-card-actions>
      </mat-card>

      <!-- Unirse -->
      <mat-card class="panel" *ngIf="joining() as team">
        <mat-card-header>
          <mat-card-title>Unirse a "{{ team.name }}"</mat-card-title>
          <mat-card-subtitle>
            Tu minero va a pedirle fragmentos al coordinador del equipo en vez de minar solo.
          </mat-card-subtitle>
        </mat-card-header>
        <mat-card-content>
          <mat-form-field appearance="outline" class="full-width"
                          *ngIf="freeWorkers().length > 0">
            <mat-label>Minero a sumar</mat-label>
            <mat-select [(value)]="joinWorkerId">
              <mat-option *ngFor="let w of freeWorkers()" [value]="w.worker_id">
                {{ w.worker_id }}<span *ngIf="!w.running"> (detenido)</span>
              </mat-option>
            </mat-select>
          </mat-form-field>
          <p class="hint" *ngIf="freeWorkers().length === 0">
            No tenés mineros libres. Sacá uno de su equipo actual, o registrá uno
            nuevo con <strong>Registrar minero</strong>.
          </p>
        </mat-card-content>
        <mat-card-actions class="actions">
          <button mat-button (click)="joining.set(null)">Cancelar</button>
          <button mat-raised-button color="primary"
                  (click)="confirmJoin(team)"
                  [disabled]="!joinWorkerId() || busy()">
            {{ busy() ? 'Uniendo…' : 'Unirme' }}
          </button>
        </mat-card-actions>
      </mat-card>

      <p class="empty" *ngIf="teams().length === 0">
        Todavía no hay ningún equipo. El primero que funde uno se lleva la ventaja
        de repartir el trabajo mientras el resto mina de a uno.
      </p>

      <div class="team-grid">
        <mat-card class="team-card" *ngFor="let team of teams()"
                  [class.mine]="isMyTeam(team)">
          <div class="team-head">
            <div>
              <h3>
                {{ team.name }}
                <span class="tag tag-mine" *ngIf="isMyTeam(team)">Lo fundaste vos</span>
              </h3>
              <p class="coordinator">
                Coordina <code>{{ team.coordinator_worker_id }}</code>
                <span class="dot" [class.online]="team.coordinator_online"></span>
                <span class="state">{{ team.coordinator_online ? 'en línea' : 'sin reportar' }}</span>
              </p>
            </div>
            <div class="stats">
              <div class="stat">
                <span class="stat-value">{{ team.member_count }}</span>
                <span class="stat-label">en el equipo</span>
              </div>
              <div class="stat" *ngIf="team.miners_connected !== null && team.miners_connected !== undefined">
                <span class="stat-value">{{ team.miners_connected }}</span>
                <span class="stat-label">con keep-alive</span>
              </div>
            </div>
          </div>

          <ul class="roster">
            <li *ngFor="let m of team.members">
              <span class="role" [class.coord]="m.role === 'coordinator'">
                {{ m.role === 'coordinator' ? 'Coordinador' : 'Minero' }}
              </span>
              <code>{{ m.worker_id }}</code>
              <span class="mode-tag">{{ m.mode }}</span>
              <span class="dot" [class.online]="m.running"></span>
            </li>
          </ul>

          <div class="team-actions" *ngIf="hasIdentity()">
            <button mat-button color="primary"
                    (click)="openJoin(team)"
                    *ngIf="!myWorkerIn(team)"
                    [disabled]="!team.coordinator_online"
                    [title]="team.coordinator_online
                      ? 'Sumar uno de mis mineros'
                      : 'El coordinador no está en línea todavía'">
              Unirme
            </button>
            <button mat-button
                    (click)="leave(team)"
                    *ngIf="myWorkerIn(team) as mine"
                    [disabled]="busy()">
              Sacar {{ mine }}
            </button>
            <button mat-button color="warn"
                    (click)="dissolve(team)"
                    *ngIf="isMyTeam(team)"
                    [disabled]="busy()">
              Disolver
            </button>
          </div>
        </mat-card>
      </div>
    </section>
  `,
  styles: [`
    .teams-section { margin-bottom: 40px; }
    .section-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 24px; margin-bottom: 20px; }
    h2 { color: #e0e0e0; font-weight: 500; margin: 0 0 8px; display: flex; align-items: center; gap: 10px; }
    .count { background: rgba(255,255,255,0.08); color: #b0b0b0; font-size: 0.85rem; padding: 2px 10px; border-radius: 12px; }
    .subtitle { color: #888; margin: 0; max-width: 74ch; line-height: 1.5; font-size: 0.9rem; }
    .already { color: #b0b0b0; font-size: 0.85rem; text-align: right; margin: 0; line-height: 1.6; white-space: nowrap; }
    .already .muted { color: #777; font-size: 0.8rem; }
    .panel { background: #1e1e1e; color: #e0e0e0; border: 1px solid #333; border-radius: 8px; padding: 16px; margin-bottom: 24px; }
    mat-card-title { color: #e0e0e0; font-size: 1.2rem; }
    mat-card-subtitle { color: #888; }
    .full-width { width: 100%; margin-top: 12px; }
    .actions { display: flex; justify-content: flex-end; gap: 12px; padding: 0; }
    .hint { color: #aaa; font-size: 0.9rem; line-height: 1.5; }
    .empty { color: #777; font-style: italic; margin: 8px 0 0; }

    .team-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 20px; }
    .team-card { background: #1e1e1e; border: 1px solid #333; border-radius: 8px; padding: 20px; color: #e0e0e0; }
    .team-card.mine { border-color: rgba(255,152,0,0.45); }
    .team-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
    .team-head h3 { margin: 0 0 6px; color: #e0e0e0; font-size: 1.1rem; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .coordinator { margin: 0; color: #888; font-size: 0.85rem; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
    .stats { display: flex; gap: 18px; }
    .stat { text-align: right; }
    .stat-value { display: block; font-size: 1.4rem; color: #90caf9; font-weight: 600; line-height: 1; }
    .stat-label { display: block; font-size: 0.7rem; color: #777; margin-top: 4px; }

    .roster { list-style: none; padding: 0; margin: 18px 0 0; border-top: 1px solid #2a2a2a; }
    .roster li { display: flex; align-items: center; gap: 8px; padding: 10px 0; border-bottom: 1px solid #222; font-size: 0.85rem; flex-wrap: wrap; }
    .role { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.04em; color: #81c784; background: rgba(76,175,80,0.12); padding: 2px 7px; border-radius: 4px; }
    .role.coord { color: #ffb74d; background: rgba(255,152,0,0.12); }

    code { font-family: 'Courier New', monospace; color: #90caf9; background: #0c0c0c; padding: 3px 7px; border-radius: 4px; border: 1px solid #222; }
    .mode-tag { font-size: 0.75rem; color: #b0b0b0; background: rgba(255,255,255,0.06); padding: 2px 8px; border-radius: 4px; }
    .tag { font-size: 0.7rem; padding: 2px 8px; border-radius: 12px; font-weight: 600; }
    .tag-mine { background: rgba(255,152,0,0.15); color: #ffb74d; border: 1px solid rgba(255,152,0,0.3); }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: #e57373; display: inline-block; }
    .dot.online { background: #81c784; }
    .state { color: #888; font-size: 0.8rem; }
    .team-actions { display: flex; gap: 8px; margin-top: 16px; flex-wrap: wrap; }

    ::ng-deep .mat-mdc-form-field { --mdc-outlined-text-field-outline-color: #444; --mdc-outlined-text-field-label-text-color: #888; }
    ::ng-deep .mat-mdc-select-value, ::ng-deep .mat-mdc-input-element { color: #e0e0e0; }
  `]
})
export class TeamsComponent {
  private api = inject(ApiService);
  private snack = inject(MatSnackBar);
  identityService = inject(IdentityService);

  /** Los datos los trae la página de Mineros: un solo sondeo para las dos secciones. */
  teams = input<Team[]>([]);
  workers = input<WorkerStatus[]>([]);
  /** Algo cambió del lado del servidor; el contenedor tiene que recargar. */
  changed = output<void>();

  busy = signal(false);

  creating = signal(false);
  newTeamName = signal('');
  coordinatorWorkerId = signal('');
  newWorkerId = signal('');

  joining = signal<Team | null>(null);
  joinWorkerId = signal('');

  hasIdentity = computed(() => !!this.identityService.identity());

  /** Mineros propios que no están en ningún equipo: los candidatos a coordinar o sumarse. */
  freeWorkers = computed(() =>
    this.workers().filter((w) => this.isMine(w) && !w.team_id));

  /** El equipo que fundó esta identidad, si fundó alguno. */
  myTeam = computed(() => this.teams().find((t) => this.isMyTeam(t)) ?? null);

  /**
   * Cada identidad funda un solo equipo (lo impone el backend con un 409).
   * Ocultar el botón evita ofrecer una acción que se sabe que va a fallar.
   */
  canFound = computed(() => this.hasIdentity() && !this.myTeam());

  /**
   * Hace falta dar de alta un minero: o el usuario lo eligió expresamente, o no
   * tiene ninguno libre y el alta es el único camino para fundar el equipo.
   */
  needsNewWorker = computed(() =>
    this.freeWorkers().length === 0 || this.coordinatorWorkerId() === '__new__');

  isMine(worker: WorkerStatus): boolean {
    return isOwnedBy(worker, this.identityService.identity());
  }

  isMyTeam(team: Team): boolean {
    const id = this.identityService.identity();
    if (!id) return false;
    const owner = id.isDemo ? id.username : id.pubkey;
    return team.owner === owner;
  }

  /** Cuál de mis mineros está en este equipo como miembro, si hay alguno. */
  myWorkerIn(team: Team): string | null {
    const mine = new Set(this.workers().filter((w) => this.isMine(w)).map((w) => w.worker_id));
    const found = team.members.find((m) => mine.has(m.worker_id) && m.role === 'member');
    return found ? found.worker_id : null;
  }

  // -- fundar --------------------------------------------------------------

  openCreate() {
    this.creating.set(true);
    this.newTeamName.set('');
    this.newWorkerId.set('');
    const free = this.freeWorkers();
    this.coordinatorWorkerId.set(free.length ? free[0].worker_id : '__new__');
  }

  canCreate(): boolean {
    if (!this.newTeamName().trim()) return false;
    return this.needsNewWorker()
      ? !!this.newWorkerId().trim()
      : !!this.coordinatorWorkerId();
  }

  async confirmCreate() {
    if (!this.canCreate()) return;
    this.busy.set(true);
    try {
      const name = this.newTeamName().trim();
      let workerId: string;
      let registration: WorkerRegistration | undefined;

      if (this.needsNewWorker()) {
        workerId = this.newWorkerId().trim();
        registration = await buildRegistration(this.identityService, workerId);
      } else {
        workerId = this.coordinatorWorkerId();
      }

      this.api.createTeam(name, workerId, registration).subscribe({
        next: (team) => {
          this.snack.open(
            `Equipo "${team.name}" fundado. ${workerId} pasa a coordinarlo.`,
            'Cerrar', { duration: 4000 });
          this.creating.set(false);
          this.busy.set(false);
          this.changed.emit();
        },
        error: (err) => {
          this.snack.open('No se pudo fundar el equipo: ' + this.detail(err),
            'Cerrar', { duration: 5000 });
          this.busy.set(false);
        },
      });
    } catch (err: any) {
      this.snack.open('Falló la firma: ' + err.message, 'Cerrar', { duration: 5000 });
      this.busy.set(false);
    }
  }

  // -- unirse / salir / disolver -------------------------------------------

  openJoin(team: Team) {
    this.joining.set(team);
    const free = this.freeWorkers();
    this.joinWorkerId.set(free.length ? free[0].worker_id : '');
  }

  confirmJoin(team: Team) {
    const workerId = this.joinWorkerId();
    if (!workerId) return;
    this.busy.set(true);
    this.api.joinTeam(team.team_id, workerId).subscribe({
      next: () => {
        this.snack.open(`${workerId} se sumó a "${team.name}".`, 'Cerrar', { duration: 4000 });
        this.joining.set(null);
        this.busy.set(false);
        this.changed.emit();
      },
      error: (err) => {
        this.snack.open('No se pudo unir: ' + this.detail(err), 'Cerrar', { duration: 5000 });
        this.busy.set(false);
      },
    });
  }

  leave(team: Team) {
    const workerId = this.myWorkerIn(team);
    if (!workerId) return;
    this.busy.set(true);
    this.api.leaveTeam(team.team_id, workerId).subscribe({
      next: () => {
        this.snack.open(`${workerId} volvió a minar por su cuenta.`, 'Cerrar', { duration: 4000 });
        this.busy.set(false);
        this.changed.emit();
      },
      error: (err) => {
        this.snack.open('No se pudo salir: ' + this.detail(err), 'Cerrar', { duration: 5000 });
        this.busy.set(false);
      },
    });
  }

  dissolve(team: Team) {
    if (!confirm(
      `¿Disolver "${team.name}"?\n\n` +
      `Sus ${team.member_count} minero(s) vuelven a modo competitivo: siguen ` +
      `minando la ventana en curso, pero cada uno por su cuenta.`)) {
      return;
    }
    this.busy.set(true);
    this.api.dissolveTeam(team.team_id).subscribe({
      next: (res) => {
        this.snack.open(
          `Equipo disuelto. ${(res.released || []).length} minero(s) volvieron a competitivo.`,
          'Cerrar', { duration: 4000 });
        this.busy.set(false);
        this.changed.emit();
      },
      error: (err) => {
        this.snack.open('No se pudo disolver: ' + this.detail(err), 'Cerrar', { duration: 5000 });
        this.busy.set(false);
      },
    });
  }

  private detail(err: any): string {
    return err?.error?.detail || err?.message || 'error desconocido';
  }
}
