import { Component, computed, inject, input, output, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { ApiService } from '../../core/services/api.service';
import {
  LAW_CATEGORIES,
  LawCategory,
  categoryLabel,
} from '../../core/models/law.model';
import { IdentityService } from '../../core/services/identity.service';
import { DeliberationDecision } from '../../core/models/deliberation.model';
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
 * Vive **dentro** de la página de Minería, no como pantalla aparte: equipos y
 * mineros son la misma decisión vista de dos lados (con quién mino / con qué
 * mino), y separarlos obligaba a saltar de pantalla para entender el estado de
 * un solo minero.
 *
 * Es un componente presentacional en cuanto a los datos —los recibe por input y
 * avisa con `changed` cuando hay que recargarlos— pero sí ejecuta las acciones
 * (crear, unirse, salir, disolver), porque son suyas y no del contenedor.
 *
 * Acá también se elige la **agenda** del equipo: las áreas de ley que vota. No es
 * una preferencia de visualización — con agenda declarada, el coordinador ignora
 * las ventanas de otras áreas y el equipo entero deja de aportar cómputo a esas
 * leyes (AGENT.md 3.10).
 */
@Component({
  selector: 'app-teams',
  standalone: true,
  imports: [CommonModule, MatSnackBarModule],
  template: `
    <section class="vc-section">
      <div class="vc-head">
        <div class="vc-head__text head__wide">
          <h3 class="vc-h3">
            Equipos <span class="mono head__count">{{ teams().length }}</span>
          </h3>
          <p class="head__body">
            Un equipo reparte el espacio de nonces entre todos sus mineros: cada uno
            barre un tramo distinto y el trabajo se divide de verdad. Además elige
            <strong class="head__strong">qué áreas de ley vota</strong>: sólo aporta
            cómputo a esas ventanas.
          </p>
        </div>
        <button class="btn btn-secondary head__btn" *ngIf="canFound() && !creating()"
                (click)="openCreate()">Fundar equipo</button>
        <p class="vc-note-sm head__already" *ngIf="myTeam() as mine">
          Fundaste <strong>{{ mine.name }}</strong>.<br>
          Cada identidad funda un solo equipo.
        </p>
      </div>

      <!-- ── fundar ──────────────────────────────────────────────────────── -->
      <div class="card elev-sm vc-soft panel" *ngIf="creating()">
        <span class="card-kicker">Fundar un equipo</span>
        <p class="panel__sub">
          Uno de tus mineros pasa a ser el coordinador: reparte el trabajo y además mina.
        </p>

        <div class="field panel__field">
          <label for="vc-team-name">Nombre del equipo</label>
          <input id="vc-team-name" class="input" placeholder="Ej: Los Pibes del Barrio"
                 [value]="newTeamName()" (input)="newTeamName.set($any($event.target).value)">
        </div>

        <div class="field panel__field" *ngIf="freeWorkers().length">
          <label for="vc-coord">Minero que va a coordinar</label>
          <select id="vc-coord" class="input" [value]="coordinatorWorkerId()"
                  (change)="coordinatorWorkerId.set($any($event.target).value)">
            <option *ngFor="let w of freeWorkers()" [value]="w.worker_id">
              {{ w.worker_id }}{{ w.running ? '' : ' (detenido)' }}
            </option>
            <option value="__new__" *ngIf="canRegisterAnother()">Registrar un minero nuevo…</option>
          </select>
        </div>

        <p class="vc-note-sm panel__hint" *ngIf="!freeWorkers().length && canRegisterAnother()">
          Todavía no tenés ningún minero, así que lo damos de alta ahora: elegí un
          identificador y queda registrado junto con el equipo.
        </p>
        <p class="vc-note-sm panel__hint" *ngIf="!freeWorkers().length && !canRegisterAnother()">
          Tu minero <code class="vc-code">{{ myWorkers()[0].worker_id }}</code> ya está en
          un equipo. Cada identidad registra un minero solo, así que sacalo de ahí antes
          de fundar el tuyo.
        </p>

        <div class="field panel__field" *ngIf="needsNewWorker()">
          <label for="vc-new-worker">ID del minero nuevo</label>
          <input id="vc-new-worker" class="input" placeholder="Ej: minero-gustavo-1"
                 [value]="newWorkerId()" (input)="newWorkerId.set($any($event.target).value)">
          <p class="vc-note-sm panel__hint">Se registra a tu nombre y se levanta solo.</p>
        </div>

        <div class="panel__agenda">
          <p class="vc-label">¿Qué áreas de ley vota el equipo?</p>
          <p class="vc-note-sm panel__hint">
            Sin elegir ninguna vota todas. Si elegís algunas, cuando entre una ley de
            otra área tu equipo no va a aportar un solo hash.
          </p>
          <div class="vc-actions">
            <button type="button" class="tag tag-btn" *ngFor="let c of categories()"
                    [class.tag-accent]="newCategories().includes(c.value)"
                    [class.tag-outline]="!newCategories().includes(c.value)"
                    (click)="toggleNew(c.value)">{{ c.label }}</button>
          </div>
          <p class="vc-note-sm panel__summary">{{ agendaSummary(newCategories()) }}</p>
        </div>

        <div class="vc-actions panel__actions">
          <button class="btn btn-secondary" (click)="creating.set(false)">Cancelar</button>
          <button class="btn btn-primary" (click)="confirmCreate()" [disabled]="!canCreate() || busy()">
            {{ busy() ? 'Fundando…' : 'Fundar equipo' }}
          </button>
        </div>
      </div>

      <!-- ── unirse ──────────────────────────────────────────────────────── -->
      <div class="card elev-sm vc-soft panel" *ngIf="joining() as team">
        <span class="card-kicker">Unirse a "{{ team.name }}"</span>
        <p class="panel__sub">
          Tu minero va a pedirle fragmentos al coordinador del equipo en vez de minar solo.
        </p>

        <p class="vc-note" *ngIf="!team.coordinator_online">
          El coordinador de este equipo (<code class="vc-code">{{ team.coordinator_worker_id }}</code>)
          todavía no está corriendo. Podés unirte igual: tu minero queda asignado y
          empieza a pedirle trabajo en cuanto los dos estén encendidos.
        </p>

        <p class="vc-note vc-note--warn">
          <ng-container *ngIf="team.categories?.length; else minaTodo">
            Ojo: este equipo sólo vota <strong>{{ labels(team.categories) }}</strong>. Tu
            minero va a quedarse quieto en las ventanas de otras áreas.
          </ng-container>
          <ng-template #minaTodo>
            Este equipo vota todas las áreas: tu minero va a trabajar en cada ventana.
          </ng-template>
        </p>

        <div class="field panel__field" *ngIf="freeWorkers().length; else sinLibres">
          <label for="vc-join">Minero a sumar</label>
          <select id="vc-join" class="input" [value]="joinWorkerId()"
                  (change)="joinWorkerId.set($any($event.target).value)">
            <option *ngFor="let w of freeWorkers()" [value]="w.worker_id">
              {{ w.worker_id }}{{ w.running ? '' : ' (detenido)' }}
            </option>
          </select>
        </div>
        <ng-template #sinLibres>
          <p class="vc-note-sm panel__hint">
            <ng-container *ngIf="canRegisterAnother(); else sacaloDeAhi">
              No tenés ningún minero registrado. Registrá uno con
              <strong>Registrar minero</strong> y volvé.
            </ng-container>
            <ng-template #sacaloDeAhi>
              Tu minero ya está en un equipo. Sacalo de ahí antes de sumarlo a éste.
            </ng-template>
          </p>
        </ng-template>

        <div class="vc-actions panel__actions">
          <button class="btn btn-secondary" (click)="joining.set(null)">Cancelar</button>
          <button class="btn btn-primary" (click)="confirmJoin(team)" [disabled]="!joinWorkerId() || busy()">
            {{ busy() ? 'Uniendo…' : 'Unirme' }}
          </button>
        </div>
      </div>

      <p class="vc-empty teams__empty" *ngIf="!teams().length">
        Todavía no hay ningún equipo. El primero que funde uno se lleva la ventaja de
        repartir el trabajo mientras el resto mina de a uno.
      </p>

      <!-- ── las tarjetas ────────────────────────────────────────────────── -->
      <div class="teams">
        <div class="card elev-sm team" *ngFor="let team of teams()"
             [class.vc-soft--75]="isMyTeam(team)" [class.vc-owned]="isMyTeam(team)"
             [class.vc-plain]="!isMyTeam(team)">
          <div class="vc-row team__head">
            <h4 class="team__name">{{ team.name }}</h4>
            <span class="tag tag-accent" *ngIf="isMyTeam(team)">lo fundaste vos</span>
            <span class="tag tag-neutral" *ngIf="!team.coordinator_online">sin arrancar</span>
          </div>
          <p class="vc-note-sm team__coord">
            Coordinado por <code class="mono team__code">{{ team.coordinator_worker_id }}</code>
          </p>

          <div class="vc-rule vc-rule--short"></div>

          <div class="team__stats">
            <span class="team__stat">
              <span class="mono team__num">{{ team.member_count }}</span>
              <span class="vc-label">{{ team.member_count === 1 ? 'minero' : 'mineros' }}</span>
            </span>
            <span class="team__stat" *ngIf="team.miners_connected !== null && team.miners_connected !== undefined">
              <span class="mono team__num" [class.team__num--live]="team.miners_connected">{{ team.miners_connected }}</span>
              <span class="vc-label">con keep-alive</span>
            </span>
          </div>

          <ul class="vc-roster">
            <li *ngFor="let m of team.members" [class.coord]="m.role === 'coordinator'">
              <span class="dot" [class.online]="m.running"></span>
              <code>{{ m.worker_id }}</code>
              <span class="tag team__role"
                    [class.tag-accent]="m.role === 'coordinator'"
                    [class.tag-neutral]="m.role !== 'coordinator'">
                {{ m.role === 'coordinator' ? 'Coordinador' : 'Minero' }}
              </span>
            </li>
          </ul>

          <div class="vc-actions team__agenda">
            <span class="vc-label">Vota</span>
            <ng-container *ngIf="team.categories?.length; else votaTodo">
              <span class="tag tag-outline" *ngFor="let c of team.categories">{{ label(c) }}</span>
            </ng-container>
            <ng-template #votaTodo><span class="tag tag-neutral">todas las áreas</span></ng-template>
          </div>

          <!-- Qué cuenta en una deliberación si el fundador no responde
               (AGENT.md 3.12). Sólo el fundador la cambia. -->
          <div class="vc-actions team__agenda">
            <span class="vc-label">Si no respondés</span>
            <ng-container *ngIf="isMyTeam(team); else soloVer">
              <button type="button" class="tag tag-btn" *ngFor="let o of defaultOptions"
                      [class.tag-accent]="(team.default_decision || '') === o.value"
                      [class.tag-outline]="(team.default_decision || '') !== o.value"
                      [disabled]="busy()" (click)="setDefault(team, o.value)">{{ o.label }}</button>
            </ng-container>
            <ng-template #soloVer>
              <span class="tag tag-neutral">{{ defaultLabel(team.default_decision) }}</span>
            </ng-template>
          </div>

          <div class="team__edit" *ngIf="editing() === team.team_id">
            <div class="vc-actions">
              <button type="button" class="tag tag-btn" *ngFor="let c of categories()"
                      [class.tag-accent]="editCategories().includes(c.value)"
                      [class.tag-outline]="!editCategories().includes(c.value)"
                      (click)="toggleEdit(c.value)">{{ c.label }}</button>
            </div>
            <p class="vc-note-sm panel__summary">{{ agendaSummary(editCategories()) }}</p>
            <div class="vc-actions team__edit-cta">
              <button class="btn btn-secondary team__btn" (click)="editing.set(null)">Cancelar</button>
              <button class="btn btn-primary team__btn" (click)="saveAgenda(team)" [disabled]="busy()">
                {{ busy() ? 'Guardando…' : 'Guardar agenda' }}
              </button>
            </div>
          </div>

          <p class="vc-note" *ngIf="!team.coordinator_online && !isMyTeam(team)">
            El coordinador todavía no está corriendo. Podés unirte igual: tu minero queda
            asignado y empieza a pedirle trabajo en cuanto los dos estén encendidos.
          </p>

          <ng-container *ngIf="hasIdentity()">
            <div class="vc-rule vc-rule--short"></div>
            <div class="vc-actions">
              <button class="btn btn-secondary team__btn" *ngIf="isMyTeam(team) && editing() !== team.team_id"
                      (click)="openAgenda(team)">Cambiar áreas</button>
              <button class="btn btn-secondary team__btn" *ngIf="myWorkerIn(team) as mine"
                      (click)="leave(team)" [disabled]="busy()">Sacar {{ mine }}</button>
              <button class="btn btn-primary team__btn" *ngIf="!hasWorkerIn(team)"
                      (click)="openJoin(team)" [disabled]="busy()">Unirme</button>
              <button class="btn btn-ghost vc-push team__btn team__danger" *ngIf="isMyTeam(team)"
                      (click)="dissolve(team)" [disabled]="busy()">Disolver</button>
            </div>
          </ng-container>
        </div>
      </div>
    </section>
  `,
  styles: [`
    .head__wide { max-width: 70ch; }
    .head__count { font-size: 14px; color: var(--color-neutral-500); }
    .head__body { margin: 10px 0 0; font-size: 13.5px; line-height: 1.7; color: var(--color-neutral-400); }
    .head__strong { color: var(--color-neutral-200); font-weight: 500; }
    .head__btn { min-height: 38px; font-size: 13px; }
    .head__already { text-align: right; }
    .head__already strong { color: var(--color-neutral-200); font-weight: 500; }

    .panel { padding: 24px; margin-top: 24px; }
    .panel__sub { margin: 10px 0 0; font-size: 13px; line-height: 1.7; color: var(--color-neutral-400); }
    .panel__field { margin-top: 16px; }
    .panel__hint { margin-top: 6px; }
    .panel__agenda { margin-top: 20px; }
    .panel__agenda .vc-label { margin-bottom: 6px; }
    .panel__agenda .vc-actions { margin-top: 12px; }
    .panel__summary { margin-top: 10px; }
    .panel__actions { justify-content: flex-end; margin-top: 20px; gap: 12px; }

    .teams__empty { margin-top: 24px; }
    .teams { display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 16px; margin-top: 24px; }
    .team { padding: 24px; }
    .team__head { gap: 10px; }
    .team__name { margin: 0; font-size: 19px; color: var(--color-neutral-100); }
    .team__coord { margin-top: 9px; }
    .team__code { font-size: 11.5px; color: var(--color-accent-300); }
    .team__stats { display: flex; flex-wrap: wrap; align-items: baseline; gap: 22px; }
    .team__stat { display: inline-flex; align-items: baseline; gap: 7px; }
    .team__num { font-size: 21px; line-height: 1; color: var(--color-neutral-100); }
    .team__num--live { color: var(--color-accent-300); }
    .team__role { flex: none; margin-left: auto; }
    .team__agenda { margin-top: 18px; }
    .team__edit { margin-top: 16px; }
    .team__edit-cta { justify-content: flex-end; margin-top: 12px; }
    .team__btn { font-size: 13px; }
    .team__danger { color: var(--color-neutral-400); }
  `]
})
export class TeamsComponent {
  private api = inject(ApiService);
  private snack = inject(MatSnackBar);
  identityService = inject(IdentityService);

  /** Los datos los trae la página de Minería: un solo sondeo para las dos secciones. */
  teams = input<Team[]>([]);
  workers = input<WorkerStatus[]>([]);
  /** Algo cambió del lado del servidor; el contenedor tiene que recargar. */
  changed = output<void>();

  busy = signal(false);

  creating = signal(false);
  newTeamName = signal('');
  coordinatorWorkerId = signal('');
  newWorkerId = signal('');
  /** Agenda que se está eligiendo al fundar. Vacía = vota todas. */
  newCategories = signal<string[]>([]);

  /** Equipo cuya agenda se está editando ahora, si hay alguno. */
  editing = signal<string | null>(null);
  editCategories = signal<string[]>([]);

  /** Áreas conocidas; la lista local se usa hasta que responde el backend. */
  categories = signal<LawCategory[]>(LAW_CATEGORIES);

  joining = signal<Team | null>(null);
  joinWorkerId = signal('');

  constructor() {
    this.api.getLawCategories().subscribe({
      next: (cats) => { if (cats?.length) this.categories.set(cats); },
      error: () => {},  // nos quedamos con las etiquetas locales
    });
  }

  label(category: string): string {
    return categoryLabel(category, this.categories());
  }

  labels(categories: string[]): string {
    return categories.map((c) => this.label(c)).join(', ');
  }

  /** Frase que dice, en criollo, qué implica la agenda elegida. */
  agendaSummary(selected: string[]): string {
    if (!selected.length) {
      return 'Sin áreas elegidas: el equipo mina toda ventana que se abra.';
    }
    return `El equipo sólo va a minar leyes de: ${this.labels(selected)}. Las demás las deja pasar.`;
  }

  private toggle(current: string[], value: string): string[] {
    return current.includes(value)
      ? current.filter((c) => c !== value)
      : [...current, value];
  }

  toggleNew(value: string) {
    this.newCategories.set(this.toggle(this.newCategories(), value));
  }

  toggleEdit(value: string) {
    this.editCategories.set(this.toggle(this.editCategories(), value));
  }

  openAgenda(team: Team) {
    this.editing.set(team.team_id);
    this.editCategories.set([...(team.categories ?? [])]);
  }

  saveAgenda(team: Team) {
    this.busy.set(true);
    this.api.setTeamCategories(team.team_id, this.editCategories()).subscribe({
      next: (updated) => {
        this.snack.open(
          updated.categories.length
            ? `"${team.name}" ahora vota ${this.labels(updated.categories)}.`
            : `"${team.name}" vuelve a votar todas las áreas.`,
          'Cerrar', { duration: 4000 });
        this.editing.set(null);
        this.busy.set(false);
        this.changed.emit();
      },
      error: (err) => {
        this.snack.open('No se pudo cambiar la agenda: ' + this.detail(err),
          'Cerrar', { duration: 5000 });
        this.busy.set(false);
      },
    });
  }

  /** Respuestas por defecto posibles en una deliberación. */
  readonly defaultOptions: { value: DeliberationDecision | ''; label: string }[] = [
    { value: '', label: 'no mina' },
    { value: 'accept', label: 'aporta' },
    { value: 'reject', label: 'no aporta (veto)' },
  ];

  defaultLabel(decision: string | undefined): string {
    return this.defaultOptions.find((o) => o.value === (decision || ''))?.label ?? 'no mina';
  }

  /**
   * Fija qué responde el equipo si el fundador no está durante una
   * deliberación. "no mina" y "no aporta" no son lo mismo: el primero no vota
   * (si nadie responde, la ley vuelve a la cola) y el segundo es un veto.
   */
  setDefault(team: Team, decision: DeliberationDecision | '') {
    if ((team.default_decision || '') === decision) return;
    this.busy.set(true);
    this.api.setTeamDefaultDecision(team.team_id, decision).subscribe({
      next: () => {
        this.snack.open(`"${team.name}": si no respondés, ${this.defaultLabel(decision)}.`,
          'Cerrar', { duration: 4000 });
        this.busy.set(false);
        this.changed.emit();
      },
      error: (err) => {
        this.snack.open('No se pudo guardar: ' + this.detail(err), 'Cerrar', { duration: 5000 });
        this.busy.set(false);
      },
    });
  }

  hasIdentity = computed(() => !!this.identityService.identity());

  /** Todos mis mineros, estén libres o en un equipo. */
  myWorkers = computed(() => this.workers().filter((w) => this.isMine(w)));

  /** Mineros propios que no están en ningún equipo: los candidatos a coordinar o sumarse. */
  freeWorkers = computed(() => this.myWorkers().filter((w) => !w.team_id));

  /**
   * Cada identidad registra un minero solo (lo impone el backend con un 409).
   * Sin esto la UI ofrecía "registrar uno nuevo" a quien ya tenía el suyo
   * ocupado en otro equipo, y el alta fallaba recién al enviarla.
   */
  canRegisterAnother = computed(() => this.myWorkers().length === 0);

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
    this.canRegisterAnother()
    && (this.freeWorkers().length === 0 || this.coordinatorWorkerId() === '__new__'));

  isMine(worker: WorkerStatus): boolean {
    return isOwnedBy(worker, this.identityService.identity());
  }

  isMyTeam(team: Team): boolean {
    const id = this.identityService.identity();
    if (!id) return false;
    const owner = id.isDemo ? id.username : id.pubkey;
    return team.owner === owner;
  }

  /** Ids de todos mis mineros, estén donde estén. */
  private myWorkerIds(): Set<string> {
    return new Set(this.myWorkers().map((w) => w.worker_id));
  }

  /**
   * Cuál de mis mineros está en este equipo **como miembro**, si hay alguno.
   *
   * Deliberadamente ignora el rol de coordinador: el botón que usa esto es
   * "Sacar", y al coordinador no se lo saca — se disuelve el equipo (el backend
   * responde 409 si se intenta).
   */
  myWorkerIn(team: Team): string | null {
    const mine = this.myWorkerIds();
    const found = team.members.find((m) => mine.has(m.worker_id) && m.role === 'member');
    return found ? found.worker_id : null;
  }

  /**
   * ¿Tengo algún minero en este equipo, en **cualquier** rol?
   *
   * Es lo que decide si ofrecer "Unirme", y no puede ser `myWorkerIn` porque
   * ésa filtra por rol `member`: al fundador, cuyo minero es el coordinador, le
   * daba null y el equipo propio aparecía con el botón de unirse activado.
   */
  hasWorkerIn(team: Team): boolean {
    if (this.isMyTeam(team)) return true;
    const mine = this.myWorkerIds();
    return team.members.some((m) => mine.has(m.worker_id));
  }

  // -- fundar --------------------------------------------------------------

  openCreate() {
    this.creating.set(true);
    this.newTeamName.set('');
    this.newWorkerId.set('');
    this.newCategories.set([]);
    const free = this.freeWorkers();
    this.coordinatorWorkerId.set(
      free.length ? free[0].worker_id : (this.canRegisterAnother() ? '__new__' : ''));
  }

  canCreate(): boolean {
    if (!this.newTeamName().trim()) return false;
    if (this.needsNewWorker()) return !!this.newWorkerId().trim();
    // Sin minero libre y sin cupo para registrar otro no hay con qué fundar: el
    // '__new__' que dejaba `openCreate` habilitaba el botón para un alta que el
    // backend iba a rechazar.
    const chosen = this.coordinatorWorkerId();
    return !!chosen && chosen !== '__new__';
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

      this.api.createTeam(name, workerId, this.newCategories(), registration).subscribe({
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
