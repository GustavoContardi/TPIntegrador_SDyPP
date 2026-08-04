import { Component, inject, signal, effect, untracked } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatTableModule } from '@angular/material/table';
import { MatButtonModule } from '@angular/material/button';
import { MatTabsModule } from '@angular/material/tabs';
import { MatIconModule } from '@angular/material/icon';
import { ApiService } from '../../core/services/api.service';
import { EventsService } from '../../core/services/events.service';
import { Law } from '../../core/models/law.model';

@Component({
  selector: 'app-laws',
  standalone: true,
  imports: [CommonModule, MatCardModule, MatTableModule, MatButtonModule, MatTabsModule, MatIconModule],
  template: `
    <div class="laws-container">
      <div class="laws-header">
        <h1>Leyes</h1>
        <button mat-stroked-button class="action-btn" (click)="loadLaws()">
          <svg class="btn-svg" viewBox="0 0 24 24" fill="currentColor">
            <path d="M17.65 6.35C16.2 4.9 14.21 4 12 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08c-.82 2.33-3.04 4-5.65 4-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/>
          </svg>
          Refresh
        </button>
      </div>

      <mat-card>
        <mat-card-content>
          <mat-tab-group>
            <mat-tab label="All">
              <ng-template matTabContent>
                <table mat-table [dataSource]="allLaws()">
                  <ng-container matColumnDef="law_id">
                    <th mat-header-cell *matHeaderCellDef>ID de ley</th>
                    <td mat-cell *matCellDef="let law">{{ law.law_id }}</td>
                  </ng-container>

                  <ng-container matColumnDef="author">
                    <th mat-header-cell *matHeaderCellDef>Autor</th>
                    <td mat-cell *matCellDef="let law">{{ law.author_pubkey.slice(0, 12) }}...</td>
                  </ng-container>

                  <ng-container matColumnDef="status">
                    <th mat-header-cell *matHeaderCellDef>Estado</th>
                    <td mat-cell *matCellDef="let law">{{ law.status }}</td>
                  </ng-container>

                  <ng-container matColumnDef="action">
                    <th mat-header-cell *matHeaderCellDef>Acción</th>
                    <td mat-cell *matCellDef="let law">{{ law.action }}</td>
                  </ng-container>

                  <tr mat-header-row *matHeaderRowDef="displayedColumns"></tr>
                  <tr mat-row *matRowDef="let row; columns: displayedColumns;"></tr>
                </table>
              </ng-template>
            </mat-tab>
            <mat-tab label="Pending">
              <ng-template matTabContent>
                <table mat-table [dataSource]="pendingLaws()">
                  <ng-container matColumnDef="law_id">
                    <th mat-header-cell *matHeaderCellDef>ID de ley</th>
                    <td mat-cell *matCellDef="let law">{{ law.law_id }}</td>
                  </ng-container>

                  <ng-container matColumnDef="author">
                    <th mat-header-cell *matHeaderCellDef>Autor</th>
                    <td mat-cell *matCellDef="let law">{{ law.author_pubkey.slice(0, 12) }}...</td>
                  </ng-container>

                  <ng-container matColumnDef="status">
                    <th mat-header-cell *matHeaderCellDef>Estado</th>
                    <td mat-cell *matCellDef="let law">{{ law.status }}</td>
                  </ng-container>

                  <ng-container matColumnDef="action">
                    <th mat-header-cell *matHeaderCellDef>Acción</th>
                    <td mat-cell *matCellDef="let law">{{ law.action }}</td>
                  </ng-container>

                  <tr mat-header-row *matHeaderRowDef="displayedColumns"></tr>
                  <tr mat-row *matRowDef="let row; columns: displayedColumns;"></tr>
                </table>
              </ng-template>
            </mat-tab>
            <mat-tab label="Promulgated">
              <ng-template matTabContent>
                <table mat-table [dataSource]="promulgatedLaws()">
                  <ng-container matColumnDef="law_id">
                    <th mat-header-cell *matHeaderCellDef>ID de ley</th>
                    <td mat-cell *matCellDef="let law">{{ law.law_id }}</td>
                  </ng-container>

                  <ng-container matColumnDef="author">
                    <th mat-header-cell *matHeaderCellDef>Autor</th>
                    <td mat-cell *matCellDef="let law">{{ law.author_pubkey.slice(0, 12) }}...</td>
                  </ng-container>

                  <ng-container matColumnDef="status">
                    <th mat-header-cell *matHeaderCellDef>Estado</th>
                    <td mat-cell *matCellDef="let law">{{ law.status }}</td>
                  </ng-container>

                  <ng-container matColumnDef="action">
                    <th mat-header-cell *matHeaderCellDef>Acción</th>
                    <td mat-cell *matCellDef="let law">{{ law.action }}</td>
                  </ng-container>

                  <tr mat-header-row *matHeaderRowDef="displayedColumns"></tr>
                  <tr mat-row *matRowDef="let row; columns: displayedColumns;"></tr>
                </table>
              </ng-template>
            </mat-tab>
          </mat-tab-group>
        </mat-card-content>
      </mat-card>
    </div>
  `,
  styles: [`
    .laws-container {
      padding: 40px 20px;
      max-width: 1400px;
      margin: 0 auto;
    }
    .laws-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 30px;
    }
    h1 {
      color: #e0e0e0;
      margin: 0;
      font-weight: 500;
    }
    .action-btn {
      --mdc-outlined-button-outline-color: #444;
      --mdc-outlined-button-label-text-color: #bbb;
      font-size: 0.8rem !important;
      height: 32px !important;
      line-height: 32px !important;
      padding: 0 10px !important;
    }
    .action-btn:hover {
      --mdc-outlined-button-outline-color: #888;
      --mdc-outlined-button-label-text-color: #fff;
      background-color: rgba(255, 255, 255, 0.05);
    }
    .btn-svg {
      width: 14px;
      height: 14px;
      margin-right: 6px;
      vertical-align: middle;
      display: inline-block;
    }
    mat-card {
      background-color: #1e1e1e;
      color: #e0e0e0;
      border: 1px solid #333;
      border-radius: 8px;
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
  `]
})
export class LawsComponent {
  private apiService = inject(ApiService);
  private eventsService = inject(EventsService);
  allLaws = signal<Law[]>([]);
  pendingLaws = signal<Law[]>([]);
  promulgatedLaws = signal<Law[]>([]);
  displayedColumns: string[] = ['law_id', 'author', 'status', 'action'];

  constructor() {
    effect(() => {
      // Track SSE signals so this effect re-runs on new blocks or law updates
      this.eventsService.latestBlock();
      this.eventsService.lawsChanged();
      untracked(() => this.loadLaws());
    });
  }

  loadLaws() {
    this.apiService.getLaws().subscribe((laws: Law[]) => {
      this.allLaws.set(laws);
      this.pendingLaws.set(laws.filter((l: Law) => l.status === 'pending_queue'));
      this.promulgatedLaws.set(laws.filter((l: Law) => l.status === 'promulgated'));
    });
  }
}
