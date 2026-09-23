import { Component, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { ApiService } from '../../core/services/api.service';
import { IdentityService } from '../../core/services/identity.service';
import {
  DEFAULT_CATEGORY,
  LAW_CATEGORIES,
  Law,
  LawCategory,
  ProposerStanding,
  categoryLabel,
} from '../../core/models/law.model';
import { SystemAvailability } from '../../core/models/system.model';
import { friendlyDate, friendlyError } from '../../core/utils/format';

@Component({
  selector: 'app-propose-law',
  standalone: true,
  imports: [CommonModule, RouterModule],
  template: `
    <main class="vc-page">
      <div class="vc-head__text">
        <h6 class="vc-kicker">Gobierno</h6>
        <h1 class="vc-title">Proponer ley</h1>
        <p class="vc-lead">
          Escribí tu propuesta, firmala con tu identidad y mandala a la red. Cuando
          llegue su turno, los mineros van a decidir si la respaldan con esfuerzo real.
        </p>
      </div>

      <p class="vc-note vc-note--warn" *ngIf="!identityService.identity()">
        <strong>Necesitás una identidad para proponer.</strong>&ngsp;
        <span><a routerLink="/identity">Creá tu identidad</a>: toma unos segundos y no pide datos personales.</span>
      </p>

      <div class="layout" *ngIf="identityService.identity()">
        <form class="form" (submit)="$event.preventDefault()">

          <div>
            <p class="vc-label form__label">¿Qué querés hacer?</p>
            <div class="picks">
              <button type="button" class="card elev-sm pick" [class.on]="!isDerogar()"
                      (click)="setAction('promulgacion')">
                <span class="pick__name">
                  Proponer una ley nueva
                  <span class="tag tag-accent" *ngIf="!isDerogar()">elegida</span>
                </span>
                <span class="pick__note">dificultad {{ zerosPromulgar() }} · esfuerzo normal</span>
              </button>
              <button type="button" class="card elev-sm pick" [class.on]="isDerogar()"
                      (click)="setAction('derogacion')">
                <span class="pick__name">
                  Derogar una vigente
                  <span class="tag tag-accent" *ngIf="isDerogar()">elegida</span>
                </span>
                <span class="pick__note">dificultad {{ zerosDerogar() }} · 16 veces más esfuerzo</span>
              </button>
            </div>
          </div>

          <ng-container *ngIf="!isDerogar()">
            <div>
              <p class="vc-label form__label--tight">Área de gobierno</p>
              <p class="vc-note-sm form__help">
                Define quién puede respaldarla: sólo participan los equipos que eligieron
                esta área.
              </p>
              <div class="vc-actions">
                <button type="button" class="tag tag-btn"
                        *ngFor="let c of categories()"
                        [class.tag-accent]="category() === c.value"
                        [class.tag-outline]="category() !== c.value"
                        (click)="setCategory(c.value)">{{ c.label }}</button>
              </div>
            </div>

            <div class="field">
              <div class="field__head">
                <label for="vc-text" class="vc-label">Texto de la ley</label>
                <span class="mono field__count">{{ text().length }} caracteres</span>
              </div>
              <textarea id="vc-text" class="input area" rows="12" placeholder="Artículo 1º — Escribí acá el texto de tu ley…"
                        [value]="text()" (input)="onText($event)"></textarea>
              <div class="vc-actions field__foot">
                <!-- El input de archivo va oculto detrás de la etiqueta: el nativo
                     no se puede pintar y el sistema no tiene una clase para él. -->
                <label class="btn btn-secondary field__file">
                  Subir un archivo .txt
                  <input type="file" accept=".txt" (change)="onFile($event)" hidden>
                </label>
                <span class="vc-note-sm">También podés subir el texto desde un archivo.</span>
              </div>
            </div>
          </ng-container>

          <div *ngIf="isDerogar()">
            <p class="vc-label form__label--tight">Ley a derogar</p>
            <p class="vc-note-sm form__help">
              La votan los mismos equipos que pueden votar su área, así que el área no
              se elige: es la de la ley original.
            </p>
            <div class="vc-stack" *ngIf="promulgatedLaws().length; else sinLeyes">
              <button type="button" class="card elev-sm law" *ngFor="let law of promulgatedLaws()"
                      [class.on]="lawIdToRepeal() === law.law_id"
                      (click)="setRepeal(law.law_id)">
                <span class="law__text">
                  <span class="mono law__id">{{ law.law_id }}</span>
                  <span class="law__meta">propuesta el {{ date(law.created_at) }}</span>
                </span>
                <span class="tag law__area"
                      [class.tag-accent]="lawIdToRepeal() === law.law_id"
                      [class.tag-neutral]="lawIdToRepeal() !== law.law_id">{{ label(law.category) }}</span>
              </button>
            </div>
            <ng-template #sinLeyes>
              <p class="vc-empty">No hay leyes promulgadas para derogar.</p>
            </ng-template>
          </div>

          <div class="vc-rule"></div>

          <p class="vc-note vc-note--warn" *ngIf="notAllowed() as st">
            <strong>Tu identidad no puede proponer leyes.</strong>&ngsp;
            <span>{{ st.reason }}</span>&ngsp;
            <a routerLink="/workers">Ir a Minería</a>
          </p>
          <p class="vc-note vc-note--warn" *ngIf="unavailable() as av">
            <strong>Tu ley va a tener que esperar.</strong>&ngsp;
            <span>{{ av.message }}</span>
          </p>
          <p class="vc-note vc-note--bad" *ngIf="error()">
            <strong>No se pudo proponer.</strong>&ngsp;<span>{{ error() }}</span>
          </p>
          <p class="vc-note vc-note--ok" *ngIf="success()">
            <strong>{{ isDerogar() ? '¡Derogación enviada!' : '¡Ley enviada!' }}</strong>&ngsp;
            <span>Quedó registrada como <span class="mono">{{ success() }}</span>. Seguí su
              recorrido en <a routerLink="/queue">Votar</a>.</span>&ngsp;
            <span *ngIf="postponed() as av">{{ av.message }}</span>
          </p>

          <div class="form__foot">
            <p class="vc-note-sm form__cooldown">
              Después de enviar vas a tener que esperar unas cuantas votaciones antes de
              proponer otra ley: así nadie puede acaparar la agenda.
            </p>
            <button type="button" class="btn btn-primary form__submit"
                    (click)="submit()" [disabled]="!canSubmit()">
              {{ submitting() ? 'Enviando…' : (isDerogar() ? 'Proponer derogación' : 'Proponer ley') }}
            </button>
          </div>
        </form>

        <aside class="aside">
          <div class="card elev-sm vc-plain vc-edge box">
            <span class="card-kicker">Quién puede respaldarla</span>
            <p class="box__avail">{{ availabilityText() }}</p>
            <div class="vc-actions box__tags" *ngIf="availability() as av">
              <span class="tag tag-accent">{{ av.eligible_workers }} {{ av.eligible_workers === 1 ? 'minero disponible' : 'mineros disponibles' }} para esta área</span>
              <span class="tag tag-neutral">{{ av.live_workers }} {{ av.live_workers === 1 ? 'encendido' : 'encendidos' }} en la red</span>
            </div>
          </div>

          <div class="card elev-sm vc-plain box">
            <span class="card-kicker">Lo que va a costar</span>
            <div class="box__cost">
              <span class="box__zeros">{{ zeros() }}</span>
              <span class="box__unit">nivel de dificultad</span>
            </div>
            <p class="vc-note-sm box__note">{{ costNote() }}</p>
          </div>

          <div class="card elev-sm vc-soft box">
            <span class="card-kicker">Firmada por vos</span>
            <p class="box__sign">
              Tu propuesta sale firmada con tu identidad: nadie puede cambiarle una coma
              en el camino ni presentarla en tu nombre.
            </p>
          </div>
        </aside>
      </div>
    </main>
  `,
  styles: [`
    .layout {
      display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(0, .85fr);
      gap: 40px; margin-top: 48px; align-items: start;
    }
    .form { display: flex; flex-direction: column; gap: 30px; }
    .form__label { margin: 0 0 12px; font-size: 12px; }
    .form__label--tight { margin: 0 0 6px; font-size: 12px; }
    .form__help { margin: 0 0 14px; }

    .picks { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .pick {
      gap: 4px; align-items: flex-start; text-align: left; cursor: pointer;
      padding: 16px 18px; background: transparent; color: var(--color-text);
      font-family: var(--font-body); border: 1px solid var(--color-divider);
    }
    .pick:hover { border-color: var(--color-accent-600); }
    /* La elegida no se rellena: se le enciende el filo. */
    .pick.on { border-color: var(--color-accent); }
    .pick__name { display: flex; align-items: center; gap: 8px; font-family: var(--font-heading); font-size: 15px; }
    .pick__note { font-size: 12px; color: var(--color-neutral-500); }

    .field__head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 8px; }
    .field__count { font-size: 11.5px; color: var(--color-neutral-500); }
    .area {
      min-height: 240px; font-size: 14.5px; line-height: 1.7; padding: 16px 18px;
      background: color-mix(in srgb, var(--color-surface) 70%, transparent);
    }
    .field__foot { margin-top: 12px; }
    .field__file { font-size: 12.5px; }

    .law {
      flex-direction: row; align-items: center; justify-content: space-between; gap: 16px;
      cursor: pointer; text-align: left; padding: 14px 18px; background: transparent;
      color: var(--color-text); font-family: var(--font-body); border: 1px solid var(--color-divider);
    }
    .law:hover { border-color: var(--color-accent-600); }
    .law.on { border-color: var(--color-accent); }
    .law__text { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
    .law__id { font-size: 13px; color: var(--color-neutral-100); }
    .law__meta { font-size: 11.5px; color: var(--color-neutral-500); }
    .law__area { flex: none; }

    .form__foot { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 16px; }
    .form__cooldown { max-width: 42ch; }
    .form__submit { min-height: 46px; padding: 0 28px; font-size: 14.5px; }

    .aside { display: flex; flex-direction: column; gap: 14px; position: sticky; top: 92px; }
    .box { padding: 20px 22px; }
    .box__note { margin-top: 12px; }
    .box__cost { display: flex; align-items: baseline; gap: 10px; margin-top: 12px; }
    .box__zeros { font-family: var(--font-heading); font-size: 34px; line-height: 1; color: var(--color-neutral-100); }
    .box__unit { font-size: 13px; color: var(--color-neutral-500); }
    .box__avail { margin: 10px 0 0; font-size: 13.5px; line-height: 1.7; color: var(--color-neutral-300); }
    .box__tags { margin-top: 14px; }
    .box__sign { margin: 10px 0 0; font-size: 13px; line-height: 1.7; color: var(--color-neutral-400); }

    @media (max-width: 960px) {
      .layout { grid-template-columns: minmax(0, 1fr); }
      .aside { position: static; }
    }
  `]
})
export class ProposeLawComponent {
  private api = inject(ApiService);
  identityService = inject(IdentityService);

  text = signal('');
  action = signal('promulgacion');
  category = signal(DEFAULT_CATEGORY);
  lawIdToRepeal = signal('');
  submitting = signal(false);
  error = signal('');
  success = signal('');
  promulgatedLaws = signal<Law[]>([]);
  availability = signal<SystemAvailability | null>(null);
  /**
   * Si esta identidad puede proponer (fundador de equipo o dueño de un
   * standalone). null mientras no se sabe: no se bloquea por no poder
   * preguntar, el POST igual responde el motivo.
   */
  standing = signal<ProposerStanding | null>(null);
  /** El que devolvió el alta: es el que explica en qué quedó ESTA ley. */
  postponed = signal<SystemAvailability | null>(null);
  /** Arranca con la lista local para no pintar una fila vacía; el backend la pisa. */
  categories = signal<LawCategory[]>(LAW_CATEGORIES);

  /**
   * Los ceros que exige promulgar, si se pudieron deducir.
   *
   * La API no publica la dificultad base: se infiere de la cadena, donde un
   * bloque de promulgación lleva `n` y uno de derogación `n+1`. Si la cadena
   * está vacía queda en null y la pantalla escribe "n" y "n+1" — decir un
   * número inventado sería peor que admitir que todavía no se sabe.
   */
  baseZeros = signal<number | null>(null);

  isDerogar = computed(() => this.action() === 'derogacion');
  zerosPromulgar = computed(() => this.baseZeros()?.toString() ?? 'n');
  zerosDerogar = computed(() => {
    const n = this.baseZeros();
    return n === null ? 'n+1' : String(n + 1);
  });
  zeros = computed(() => (this.isDerogar() ? this.zerosDerogar() : this.zerosPromulgar()));

  costNote = computed(() =>
    this.isDerogar()
      ? 'Derogar exige un nivel más que promulgar: dieciséis veces más esfuerzo para la misma red. Deshacer tiene que costar más que hacer.'
      : 'Cuanto más alta la dificultad, más esfuerzo tiene que poner la red para aprobarla. Cada nivel multiplica ese esfuerzo por 16.');

  /** Lo que dice el backend sobre quién puede minar esto, o el estado de espera. */
  availabilityText = computed(() => {
    const av = this.availability();
    if (!av) return 'Consultando qué mineros pueden respaldar esta área…';
    return av.message;
  });

  constructor() {
    this.api.getLaws('promulgated').subscribe({
      next: (laws) => this.promulgatedLaws.set(laws),
      error: () => {},
    });
    this.api.getLawCategories().subscribe({
      next: (cats) => { if (cats?.length) this.categories.set(cats); },
      // Sin conexión nos quedamos con la lista local: peor sería no dejar
      // proponer porque no se pudo leer un catálogo que casi nunca cambia.
      error: () => {},
    });
    this.inferBaseZeros();
    this.refreshAvailability();
    this.refreshStanding();
  }

  private refreshStanding() {
    const id = this.identityService.identity();
    if (!id) return;
    this.api.getProposerStanding(id.pubkey).subscribe({
      next: (st) => this.standing.set(st),
      error: () => this.standing.set(null),
    });
  }

  /** El veredicto negativo, o null si puede proponer (o no se sabe). */
  notAllowed(): ProposerStanding | null {
    const st = this.standing();
    return st && !st.allowed ? st : null;
  }

  label(category: string): string {
    return categoryLabel(category, this.categories());
  }

  date = friendlyDate;

  /** Deduce `n` del último bloque sellado; ver `baseZeros`. */
  private inferBaseZeros() {
    this.api.getChain().subscribe({
      next: (blocks) => {
        const last = blocks[blocks.length - 1];
        if (!last) return;
        this.baseZeros.set(
          last.action === 'derogacion' ? last.n_zeros_required - 1 : last.n_zeros_required);
      },
      error: () => {},
    });
  }

  setAction(action: string) {
    this.action.set(action);
    this.refreshAvailability();
  }

  setCategory(value: string) {
    this.category.set(value);
    this.refreshAvailability();
  }

  setRepeal(lawId: string) {
    this.lawIdToRepeal.set(lawId);
    this.refreshAvailability();
  }

  onText(event: Event) {
    this.text.set((event.target as HTMLTextAreaElement).value);
  }

  onFile(event: Event) {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => this.text.set(reader.result as string);
    reader.readAsText(file);
    // Sin esto, volver a elegir el mismo archivo no dispara 'change'.
    input.value = '';
  }

  /** Área efectiva de lo que se está por proponer. */
  effectiveCategory(): string {
    return this.repealCategory() ?? this.category();
  }

  /**
   * Consulta si hay mineros dispuestos a minar lo que se está por proponer.
   *
   * Se pregunta por área **y por acción**, y no una sola vez: un equipo que sólo
   * vota 'economia' deja el sistema disponible para esa área e indisponible para
   * el resto, y uno que rechaza derogaciones lo deja disponible para promulgar
   * en su área e indisponible para derogar en la misma. La respuesta cambia con
   * cada elección, y por eso las tres la disparan.
   */
  refreshAvailability() {
    this.api.getSystemAvailability(
      this.effectiveCategory(), this.action(),
      this.isDerogar() ? this.lawIdToRepeal() : '').subscribe({
      next: (av) => this.availability.set(av),
      // Sin respuesta no se afirma nada: mostrar "no disponible" porque no se
      // pudo consultar sería peor que no mostrar el aviso — el sistema podría
      // estar perfectamente operativo.
      error: () => this.availability.set(null),
    });
  }

  /** El aviso a mostrar antes de proponer, o null si el sistema está operativo. */
  unavailable(): SystemAvailability | null {
    const av = this.availability();
    return av && !av.available ? av : null;
  }

  /**
   * Área de la ley que se está por derogar.
   *
   * El backend la impone (la de la ley original) y el cliente tiene que firmar
   * esa misma: si firmara otra, la verificación de la firma fallaría.
   */
  repealCategory(): string | null {
    if (!this.isDerogar() || !this.lawIdToRepeal()) return null;
    const law = this.promulgatedLaws().find((l) => l.law_id === this.lawIdToRepeal());
    return law?.category ?? DEFAULT_CATEGORY;
  }

  canSubmit(): boolean {
    if (this.submitting() || this.notAllowed()) return false;
    return this.isDerogar() ? !!this.lawIdToRepeal() : !!this.text();
  }

  async submit() {
    const id = this.identityService.identity();
    if (!id || !this.canSubmit()) return;

    this.submitting.set(true);
    this.error.set('');
    this.success.set('');
    this.postponed.set(null);

    try {
      const derogar = this.isDerogar();
      const text = derogar ? '' : this.text();
      const law_id = derogar ? this.lawIdToRepeal() : `ley-${crypto.randomUUID().slice(0, 8)}`;
      const text_hash = await this.identityService.sha256Hex(text);
      const created_at = new Date().toISOString();
      // En una derogación el área la manda la ley original; firmar cualquier otra
      // haría fallar la verificación del backend, que resuelve la categoría antes
      // de comprobar la firma.
      const category = this.repealCategory() ?? this.category();

      const message =
        `${id.pubkey}|${this.action()}|${text_hash}|${law_id}|${created_at}|${category}`;
      const signature = await this.identityService.sign(message);

      const payload: any = {
        author_pubkey: id.pubkey,
        action: this.action(),
        category,
        law_id,
        text,
        text_hash,
        created_at,
        signature,
      };

      const result = await firstValueFrom(this.api.proposeLaw(payload));
      this.success.set(result?.law_id ?? 'unknown');
      // La ley se aceptó igual: si no hay red, queda encolada y hay que decirlo
      // acá mismo, junto al "propuesta con éxito", o el autor se queda esperando
      // una ventana que no va a abrirse todavía.
      const av = result?.availability ?? null;
      this.postponed.set(av && !av.available ? av : null);
      this.availability.set(av);
      if (derogar) {
        this.lawIdToRepeal.set('');
      } else {
        this.text.set('');
      }
    } catch (e: any) {
      this.error.set(friendlyError(e, 'No se pudo enviar la propuesta. Probá de nuevo.'));
    } finally {
      this.submitting.set(false);
    }
  }
}
