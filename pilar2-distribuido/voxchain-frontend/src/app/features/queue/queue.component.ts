import { Component, inject, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatTabsModule } from '@angular/material/tabs';
import { MatTableModule } from '@angular/material/table';
import { MatIconModule } from '@angular/material/icon';
import { ApiService } from '../../core/services/api.service';
import { IdentityService } from '../../core/services/identity.service';
import { EventsService } from '../../core/services/events.service';
import { LAW_CATEGORIES, Law, LawCategory, categoryLabel } from '../../core/models/law.model';
import { Window } from '../../core/models/window.model';
import { RouterModule } from '@angular/router';

@Component({
  selector: 'app-queue',
  standalone: true,
  imports: [CommonModule, RouterModule, MatCardModule, MatButtonModule, MatTabsModule, MatTableModule, MatIconModule],
  template: `
    <div class="queue-container">
      <h1>Cola de votación</h1>

      <div *ngIf="!identityService.identity()" class="no-identity">
        <mat-card>
          <mat-card-content>
            <p>Necesitás <a routerLink="/identity">crear una identidad</a> para participar.</p>
          </mat-card-content>
        </mat-card>
      </div>

      <div *ngIf="(activeWindow() || eventsService.activeWindow()) as window" class="active-window">
        <mat-card>
          <mat-card-header>
            <mat-card-title>Ventana de votación activa</mat-card-title>
            <span class="status-badge active">ABIERTA</span>
          </mat-card-header>
          <mat-card-content>
            <div class="window-details">
              <p><strong>Ventana:</strong> {{ window.voting_window_id }}</p>
              <p><strong>Ley:</strong> {{ window.law_id }}</p>
              <p><strong>Acción:</strong> {{ window.action }}</p>
              <p>
                <strong>Área:</strong>
                <span class="category-chip">{{ label(window.category) }}</span>
                <span class="category-note">
                  sólo aportan cómputo los equipos que votan esta área
                </span>
              </p>
              <p><strong>Dificultad:</strong> {{ window.n_zeros_required }} ceros</p>
              <p><strong>Vence:</strong> {{ window.deadline }}</p>
              <div class="challenge-box">
                <p><strong>Challenge (partial_hash_base):</strong></p>
                <code>{{ window.partial_hash_base }}</code>
              </div>
            </div>
          </mat-card-content>
          <mat-card-actions>
            <button mat-raised-button color="primary" (click)="participate(window)" [disabled]="!identityService.identity()">
              Participar en esta ventana
            </button>
          </mat-card-actions>
        </mat-card>
      </div>

      <div *ngIf="!activeWindow() && nextLaw() as next" class="next-law">
        <mat-card>
          <mat-card-header>
            <mat-card-title>Próxima ley</mat-card-title>
            <span class="status-badge upcoming">EN ESPERA</span>
          </mat-card-header>
          <mat-card-content>
            <p><strong>ID de ley:</strong> {{ next.law_id }}</p>
            <p><strong>Autor:</strong> {{ next.author_pubkey.slice(0, 16) }}...</p>
            <p><strong>Acción:</strong> {{ next.action }}</p>
            <p><strong>Área:</strong>
              <span class="category-chip">{{ label(next.category) }}</span>
            </p>
            <p><strong>Estado:</strong> {{ next.status }}</p>
          </mat-card-content>
          <mat-card-actions>
            <button mat-raised-button color="primary" (click)="prepareForNext(next)" [disabled]="!identityService.identity()">
              Prepararse para esta ley
            </button>
          </mat-card-actions>
        </mat-card>
      </div>

      <mat-tab-group>
        <mat-tab label="Pending ({{ queue().length }})">
          <ng-template matTabContent>
            <div class="queue-list">
              <mat-card *ngFor="let law of queue(); let i = index">
                <mat-card-content>
                  <div class="queue-item">
                    <span class="position">#{{ i + 1 }}</span>
                    <div class="info">
                      <p class="law-id">{{ law.law_id }}</p>
                      <p class="meta">
                        {{ law.action }}
                        <span class="category-chip">{{ label(law.category) }}</span>
                        — {{ law.author_pubkey.slice(0, 16) }}...
                      </p>
                      <button mat-button color="accent" (click)="showLawText(law.law_id)" class="view-text-btn">Ver texto</button>
                      <div *ngIf="lawTexts()[law.law_id]" class="law-text-box">
                        <pre>{{ lawTexts()[law.law_id] }}</pre>
                      </div>
                    </div>
                  </div>
                </mat-card-content>
              </mat-card>
              <p *ngIf="queue().length === 0" class="empty-queue">No hay leyes en la cola.</p>
            </div>
          </ng-template>
        </mat-tab>
        <mat-tab label="History ({{ history().length }})">
          <ng-template matTabContent>
            <div class="history-list">
              <mat-card *ngFor="let law of history()">
                <mat-card-content>
                  <div class="history-item">
                    <div class="info">
                      <p class="law-id">{{ law.law_id }}</p>
                      <p class="meta">
                        {{ law.action }}
                        <span class="category-chip">{{ label(law.category) }}</span>
                        — {{ law.author_pubkey.slice(0, 16) }}...
                      </p>
                      <button mat-button color="accent" (click)="showLawText(law.law_id)" class="view-text-btn">Ver texto</button>
                      <div *ngIf="lawTexts()[law.law_id]" class="law-text-box">
                        <pre>{{ lawTexts()[law.law_id] }}</pre>
                      </div>
                    </div>
                    <span class="status-badge voted">PROMULGADA</span>
                  </div>
                </mat-card-content>
              </mat-card>
              <p *ngIf="history().length === 0" class="empty-queue">Todavía no se procesó ninguna ley.</p>
            </div>
          </ng-template>
        </mat-tab>
      </mat-tab-group>
    </div>
  `,
  styles: [`
    .queue-container {
      padding: 20px;
      max-width: 1000px;
      margin: 0 auto;
    }
    h1, h2 { color: #e0e0e0; }
    mat-card {
      background-color: #1e1e1e;
      color: #e0e0e0;
      margin-bottom: 16px;
    }
    mat-card-title { color: #e0e0e0; }
    .status-badge {
      font-size: 0.75rem;
      padding: 2px 8px;
      border-radius: 10px;
      font-weight: bold;
    }
    .status-badge.active {
      background-color: #4caf50;
      color: #fff;
    }
    .status-badge.upcoming {
      background-color: #ff9800;
      color: #fff;
    }
    .status-badge.voted {
      background-color: #1565c0;
      color: #fff;
    }
    .window-details p, .next-law p {
      margin: 4px 0;
      color: #b0b0b0;
    }
    .challenge-box {
      margin-top: 12px;
      padding: 8px;
      background-color: #2a2a2a;
      border-radius: 4px;
    }
    .challenge-box code {
      font-family: 'Courier New', monospace;
      font-size: 0.8rem;
      color: #64b5f6;
      word-break: break-all;
    }
    .queue-item, .history-item {
      display: flex;
      align-items: center;
      gap: 16px;
    }
    .history-item {
      justify-content: space-between;
    }
    .position {
      font-size: 1.2rem;
      font-weight: bold;
      color: #64b5f6;
      min-width: 40px;
    }
    .law-id {
      font-weight: bold;
      margin: 0;
    }
    .meta {
      font-size: 0.85rem;
      color: #888;
      margin: 2px 0 0;
    }
    .view-text-btn {
      font-size: 0.75rem;
      padding: 0 8px;
      min-width: auto;
      line-height: 24px;
      margin-top: 4px;
    }
    .law-text-box {
      margin-top: 8px;
      padding: 8px;
      background-color: #2a2a2a;
      border-radius: 4px;
      max-height: 200px;
      overflow-y: auto;
    }
    .law-text-box pre {
      font-size: 0.8rem;
      color: #b0b0b0;
      white-space: pre-wrap;
      word-break: break-word;
      margin: 0;
    }
    .empty-queue, .no-identity p {
      color: #888;
    }
    .no-identity a { color: #64b5f6; }
    .category-chip { margin: 0 6px; }
    .category-note { font-size: 0.8rem; color: #777; }
  `]
})
export class QueueComponent implements OnInit {
  private apiService = inject(ApiService);
  identityService = inject(IdentityService);
  eventsService = inject(EventsService);

  queue = signal<Law[]>([]);
  nextLaw = signal<Law | null>(null);
  activeWindow = signal<Window | null>(null);
  history = signal<Law[]>([]);
  lawTexts = signal<Record<string, string>>({});
  /** Etiquetas de las áreas; el backend las pisa con la lista autoritativa. */
  categories = signal<LawCategory[]>(LAW_CATEGORIES);

  ngOnInit() {
    this.loadData();
    this.apiService.getLawCategories().subscribe({
      next: (cats) => { if (cats?.length) this.categories.set(cats); },
      error: () => {},  // nos quedamos con las etiquetas locales
    });
  }

  label(category: string): string {
    return categoryLabel(category, this.categories());
  }

  private loadData() {
    this.apiService.getLawQueue().subscribe((laws: Law[]) => {
      this.queue.set(laws);
    });
    this.apiService.getNextLaw().subscribe((law: Law | null) => {
      this.nextLaw.set(law);
    });
    this.apiService.getActiveWindow().subscribe({
      next: (w) => this.activeWindow.set(w),
      error: () => this.activeWindow.set(null)
    });
    this.apiService.getLaws('promulgated').subscribe((laws: Law[]) => {
      this.history.set(laws);
    });
  }

  participate(window: Window) {
    const challenge = {
      voting_window_id: window.voting_window_id,
      law_id: window.law_id,
      action: window.action,
      category: window.category,
      n_zeros_required: window.n_zeros_required,
      partial_hash_base: window.partial_hash_base,
      deadline: window.deadline,
    };
    const blob = new Blob([JSON.stringify(challenge, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `challenge-${window.voting_window_id}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  showLawText(lawId: string) {
    if (this.lawTexts()[lawId]) {
      const texts = { ...this.lawTexts() };
      delete texts[lawId];
      this.lawTexts.set(texts);
      return;
    }
    this.apiService.getLawText(lawId).subscribe({
      next: (text) => this.lawTexts.set({ ...this.lawTexts(), [lawId]: text }),
      error: () => console.error('Failed to load law text for', lawId)
    });
  }

  prepareForNext(law: Law) {
    const info = {
      law_id: law.law_id,
      action: law.action,
      category: law.category,
      author_pubkey: law.author_pubkey,
      text_hash: law.text_hash,
    };
    const blob = new Blob([JSON.stringify(info, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `next-law-${law.law_id}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }
}
