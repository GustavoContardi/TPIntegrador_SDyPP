import { Component, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { firstValueFrom } from 'rxjs';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatFormFieldModule } from '@angular/material/form-field';
import { RouterModule } from '@angular/router';
import { ApiService } from '../../core/services/api.service';
import { IdentityService } from '../../core/services/identity.service';
import {
  DEFAULT_CATEGORY,
  LAW_CATEGORIES,
  Law,
  LawCategory,
  categoryLabel,
} from '../../core/models/law.model';
import { SystemAvailability } from '../../core/models/system.model';

@Component({
  selector: 'app-propose-law',
  standalone: true,
  imports: [
    CommonModule, FormsModule, RouterModule,
    MatCardModule, MatButtonModule, MatInputModule,
    MatSelectModule, MatFormFieldModule,
  ],
  template: `
    <div class="propose-container">
      <h1>Proponer ley</h1>

      <div *ngIf="!identityService.identity()" class="no-identity">
        <mat-card>
          <mat-card-content>
            <p>Necesitás <a routerLink="/identity">crear una identidad</a> antes de proponer una ley.</p>
          </mat-card-content>
        </mat-card>
      </div>

      <mat-card *ngIf="identityService.identity()">
        <mat-card-content>
          <div class="form">
            <mat-form-field appearance="outline" class="full-width">
              <mat-label>Acción</mat-label>
              <mat-select [(ngModel)]="action" (ngModelChange)="refreshAvailability()">
                <mat-option value="promulgacion">Promulgar</mat-option>
                <mat-option value="derogacion">Derogar</mat-option>
              </mat-select>
            </mat-form-field>

            <div *ngIf="action === 'derogacion'">
              <mat-form-field appearance="outline" class="full-width">
                <mat-label>ID de la ley a derogar</mat-label>
                <mat-select [(ngModel)]="lawIdToRepeal" (ngModelChange)="refreshAvailability()"
                            placeholder="Elegí una ley promulgada">
                  <mat-option *ngFor="let law of promulgatedLaws()" [value]="law.law_id">
                    {{ law.law_id }}
                  </mat-option>
                </mat-select>
                <mat-hint>Elegí la ley promulgada que querés derogar</mat-hint>
              </mat-form-field>
              <p *ngIf="repealCategory() as cat" class="hint">
                Se deroga en el área <strong>{{ label(cat) }}</strong>: derogar convoca
                a los mismos equipos que promulgaron, así que el área no se elige.
              </p>
              <p *ngIf="promulgatedLaws().length === 0" class="hint">No hay leyes promulgadas para derogar.</p>
            </div>

            <div *ngIf="action !== 'derogacion'">
              <mat-form-field appearance="outline" class="full-width">
                <mat-label>Área de gobierno</mat-label>
                <mat-select [(ngModel)]="category" (ngModelChange)="refreshAvailability()">
                  <mat-option *ngFor="let c of categories()" [value]="c.value">
                    {{ c.label }}
                  </mat-option>
                </mat-select>
                <mat-hint>
                  Decide qué equipos van a aportar cómputo: los que no votan esta
                  área no minan tu ley.
                </mat-hint>
              </mat-form-field>

              <mat-form-field appearance="outline" class="full-width">
                <mat-label>Texto de la ley</mat-label>
                <textarea matInput [(ngModel)]="text" rows="10" placeholder="Escribí acá el texto de la ley..."></textarea>
              </mat-form-field>

              <div class="file-upload">
                <p class="hint">O subí un archivo de texto:</p>
                <input type="file" accept=".txt" (change)="onFileSelected($event)" />
              </div>
            </div>

            <div *ngIf="unavailable() as av" class="warning-msg">
              <!-- Marcador dibujado con CSS y no un <mat-icon>: este proyecto
                   nunca declara la fuente de Material Icons, así que un mat-icon
                   renderiza la ligadura como texto crudo ("sc" en vez del
                   reloj). Verificado en el navegador. -->
              <span class="warning-mark" aria-hidden="true">!</span>
              <div>
                <strong>Sistema no disponible en este momento.</strong>
                <p>{{ av.message }}</p>
              </div>
            </div>

            <div *ngIf="error()" class="error-msg">{{ error() }}</div>
            <div *ngIf="success()" class="success-msg">
              {{ action === 'derogacion' ? 'Derogación propuesta. ID de la ley:' : 'Ley propuesta. ID:' }} {{ success() }}
              <p *ngIf="postponed() as av" class="postponed">
                Queda <strong>pospuesta</strong>: {{ av.message }}
              </p>
            </div>

            <div class="actions">
              <button mat-raised-button color="primary" (click)="submit()" [disabled]="submitting() || !canSubmit()">
                {{ submitting() ? 'Submitting...' : (action === 'derogacion' ? 'Propose Repeal' : 'Propose Law') }}
              </button>
            </div>
          </div>
        </mat-card-content>
      </mat-card>
    </div>
  `,
  styles: [`
    .propose-container {
      padding: 20px;
      max-width: 800px;
      margin: 0 auto;
    }
    h1 { color: #e0e0e0; }
    mat-card {
      background-color: #1e1e1e;
      color: #e0e0e0;
    }
    .full-width { width: 100%; margin-bottom: 16px; }
    .hint { color: #888; font-size: 0.9rem; }
    .file-upload {
      margin-bottom: 16px;
      color: #b0b0b0;
    }
    .actions { margin-top: 16px; }
    .error-msg {
      color: #f44336;
      padding: 8px;
      margin: 8px 0;
      background-color: #2d1b1b;
      border-radius: 4px;
    }
    .success-msg {
      color: #4caf50;
      padding: 8px;
      margin: 8px 0;
      background-color: #1b2d1b;
      border-radius: 4px;
    }
    .success-msg .postponed {
      color: #ffb74d;
      margin: 8px 0 0;
      font-size: 0.9rem;
    }
    .warning-msg {
      display: flex;
      gap: 12px;
      align-items: flex-start;
      color: #ffb74d;
      padding: 12px;
      margin: 8px 0 16px;
      background-color: #2d2616;
      border-left: 3px solid #ffb74d;
      border-radius: 4px;
    }
    .warning-msg p {
      margin: 4px 0 0;
      color: #d7c9ae;
      font-size: 0.9rem;
    }
    .warning-mark {
      flex: 0 0 auto;
      width: 20px;
      height: 20px;
      border-radius: 50%;
      border: 1.5px solid #ffb74d;
      font-size: 0.85rem;
      font-weight: 700;
      line-height: 17px;
      text-align: center;
    }
    .no-identity p {
      color: #888;
    }
    .no-identity a { color: #64b5f6; }
  `]
})
export class ProposeLawComponent {
  private apiService = inject(ApiService);
  identityService = inject(IdentityService);

  text = signal('');
  action = 'promulgacion';
  category = DEFAULT_CATEGORY;
  lawIdToRepeal = '';
  submitting = signal(false);
  error = signal('');
  success = signal('');
  promulgatedLaws = signal<Law[]>([]);
  /** Estado del sistema para el área elegida; null mientras no se consultó. */
  availability = signal<SystemAvailability | null>(null);
  /** El que devolvió el alta: es el que explica en qué quedó ESTA ley. */
  postponed = signal<SystemAvailability | null>(null);
  /** Arranca con la lista local para no pintar un select vacío; el backend la pisa. */
  categories = signal<LawCategory[]>(LAW_CATEGORIES);

  constructor() {
    this.apiService.getLaws('promulgated').subscribe((laws: Law[]) => {
      this.promulgatedLaws.set(laws);
    });
    this.refreshAvailability();
    this.apiService.getLawCategories().subscribe({
      next: (cats) => { if (cats?.length) this.categories.set(cats); },
      // Sin conexión nos quedamos con la lista local: peor sería no dejar
      // proponer porque no se pudo leer un catálogo que casi nunca cambia.
      error: () => {},
    });
  }

  label(category: string): string {
    return categoryLabel(category, this.categories());
  }

  /** Área efectiva de lo que se está por proponer. */
  effectiveCategory(): string {
    return this.repealCategory() ?? this.category;
  }

  /**
   * Consulta si hay mineros dispuestos a minar lo que se está por proponer.
   *
   * Se pregunta por área **y por acción**, y no una sola vez: un equipo que sólo
   * vota 'economia' deja el sistema disponible para esa área e indisponible para
   * el resto, y uno que rechaza derogaciones lo deja disponible para promulgar
   * en su área e indisponible para derogar en la misma. La respuesta cambia con
   * cada select, y por eso los tres los disparan.
   */
  refreshAvailability() {
    this.apiService.getSystemAvailability(
      this.effectiveCategory(), this.action,
      this.action === 'derogacion' ? this.lawIdToRepeal : '').subscribe({
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
    if (this.action !== 'derogacion' || !this.lawIdToRepeal) return null;
    const law = this.promulgatedLaws().find((l) => l.law_id === this.lawIdToRepeal);
    return law?.category ?? DEFAULT_CATEGORY;
  }

  canSubmit(): boolean {
    if (this.submitting()) return false;
    if (this.action === 'derogacion') return !!this.lawIdToRepeal;
    return !!this.text();
  }

  onFileSelected(event: Event) {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      this.text.set(reader.result as string);
    };
    reader.readAsText(file);
  }

  async submit() {
    const id = this.identityService.identity();
    if (!id || !this.canSubmit()) return;

    this.submitting.set(true);
    this.error.set('');
    this.success.set('');
    this.postponed.set(null);

    try {
      const text = this.action === 'derogacion' ? '' : this.text();
      const law_id = this.action === 'derogacion'
        ? this.lawIdToRepeal
        : `ley-${crypto.randomUUID().slice(0, 8)}`;
      const text_hash = await this.identityService.sha256Hex(text);
      const created_at = new Date().toISOString();
      // En una derogación el área la manda la ley original; firmar cualquier otra
      // haría fallar la verificación del backend, que resuelve la categoría antes
      // de comprobar la firma.
      const category = this.repealCategory() ?? this.category;

      // En modo demo la clave privada está en el worker (backend); se envía sin firma.
      // REQUIRE_SIGNATURES=false en el backend acepta signature vacío.
      let signature = '';
      if (!this.identityService.isDemoMode()) {
        const message =
          `${id.pubkey}|${this.action}|${text_hash}|${law_id}|${created_at}|${category}`;
        signature = await this.identityService.sign(message);
      }

      const payload: any = {
        author_pubkey: id.pubkey,
        action: this.action,
        category,
        law_id,
        text,
        text_hash,
        created_at,
        signature,
      };

      const result = await firstValueFrom(this.apiService.proposeLaw(payload));
      this.success.set(result?.law_id ?? 'unknown');
      // La ley se aceptó igual: si no hay red, queda encolada y hay que decirlo
      // acá mismo, junto al "propuesta con éxito", o el autor se queda esperando
      // una ventana que no va a abrirse todavía.
      const av = result?.availability ?? null;
      this.postponed.set(av && !av.available ? av : null);
      this.availability.set(av);
      if (this.action !== 'derogacion') {
        this.text.set('');
      } else {
        this.lawIdToRepeal = '';
      }
    } catch (e: any) {
      this.error.set(e?.error?.detail || e.message || 'Failed to propose');
    } finally {
      this.submitting.set(false);
    }
  }
}
