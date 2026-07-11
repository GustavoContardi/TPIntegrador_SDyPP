import { Component, inject, OnInit, signal } from '@angular/core';
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
import { ApiService } from '../../core/services/api.service';
import { IdentityService } from '../../core/services/identity.service';

interface WorkerStatus {
  worker_id: string;
  mode: string;
  pool_url: string;
  running: boolean;
  pubkey?: string;
}

interface PoolPolicy {
  decision: string;
  action?: string;
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
  ],
  template: `
    <div class="workers-container">
      <div class="header-container">
        <h1>Workers Management</h1>
        <button 
          mat-raised-button 
          color="accent" 
          (click)="showRegisterForm.set(true)" 
          *ngIf="identityService.identity() && !identityService.identity()?.isDemo && !showRegisterForm()">
          <mat-icon>add</mat-icon> Register Worker
        </button>
      </div>

      <!-- Register New Worker Card -->
      <mat-card class="register-card" *ngIf="showRegisterForm()">
        <mat-card-header>
          <mat-card-title>Register Custom Node (Worker)</mat-card-title>
          <mat-card-subtitle>Cryptographically bind a worker to your citizen identity</mat-card-subtitle>
        </mat-card-header>
        <mat-card-content>
          <p class="form-hint">
            Registering a worker ID maps it to your public key. Any management actions (like Switch Mode or Configure Policy) will require your signature. 
            The worker container should run with <code>WORKER_PRIVKEY_PEM</code> containing your private key.
          </p>
          <div class="form-field">
            <mat-form-field appearance="outline" class="full-width">
              <mat-label>Worker ID</mat-label>
              <input matInput [(ngModel)]="newWorkerId" placeholder="e.g. citizen-miner-1">
              <mat-hint>Choose a unique name to identify your worker container</mat-hint>
            </mat-form-field>
          </div>
        </mat-card-content>
        <mat-card-actions class="form-actions">
          <button mat-button (click)="cancelRegister()">Cancel</button>
          <button mat-raised-button color="accent" (click)="confirmRegister()" [disabled]="!newWorkerId().trim() || registering()">
            {{ registering() ? 'Registering...' : 'Register Worker' }}
          </button>
        </mat-card-actions>
      </mat-card>
      
      <!-- Worker Status Card -->
      <mat-card class="workers-card">
        <mat-card-header>
          <mat-card-title>Active & Registered Workers</mat-card-title>
        </mat-card-header>
        <mat-card-content>
          <div class="table-container">
            <table mat-table [dataSource]="workers()">
              <ng-container matColumnDef="worker_id">
                <th mat-header-cell *matHeaderCellDef>Worker ID</th>
                <td mat-cell *matCellDef="let worker">
                  <div class="worker-id-wrapper">
                    {{ worker.worker_id }}
                    <span class="owner-badge" *ngIf="isWorkerOwned(worker)">Mine</span>
                  </div>
                </td>
              </ng-container>

              <ng-container matColumnDef="pubkey">
                <th mat-header-cell *matHeaderCellDef>Public Key</th>
                <td mat-cell *matCellDef="let worker">
                  <span *ngIf="worker.pubkey" class="pubkey-text" [title]="worker.pubkey">
                    {{ worker.pubkey.slice(0, 16) }}...
                  </span>
                  <span *ngIf="!worker.pubkey" class="empty-text">-</span>
                </td>
              </ng-container>

              <ng-container matColumnDef="mode">
                <th mat-header-cell *matHeaderCellDef>Mode</th>
                <td mat-cell *matCellDef="let worker">
                  <span [class.mode-badge]="true" [class.mode-standalone]="worker.mode === 'standalone'" 
                        [class.mode-pool-coordinator]="worker.mode === 'pool-coordinator'"
                        [class.mode-pool-worker]="worker.mode === 'pool-worker'">
                    {{ worker.mode }}
                  </span>
                </td>
              </ng-container>

              <ng-container matColumnDef="pool_url">
                <th mat-header-cell *matHeaderCellDef>Pool URL</th>
                <td mat-cell *matCellDef="let worker">{{ worker.pool_url || '-' }}</td>
              </ng-container>

              <ng-container matColumnDef="policy">
                <th mat-header-cell *matHeaderCellDef>Policy</th>
                <td mat-cell *matCellDef="let worker">
                  <span [class.policy-badge]="true" [class.policy-accept]="getPolicyDisplay(worker) === 'Accept All'"
                        [class.policy-reject]="getPolicyDisplay(worker).startsWith('Reject')">
                    {{ getPolicyDisplay(worker) }}
                  </span>
                </td>
              </ng-container>

              <ng-container matColumnDef="running">
                <th mat-header-cell *matHeaderCellDef>Running</th>
                <td mat-cell *matCellDef="let worker">
                  <span [class.running-text]="worker.running" [class.stopped-text]="!worker.running">
                    {{ worker.running ? 'Running' : 'Stopped' }}
                  </span>
                </td>
              </ng-container>

              <ng-container matColumnDef="actions">
                <th mat-header-cell *matHeaderCellDef>Actions</th>
                <td mat-cell *matCellDef="let worker">
                  <div class="actions-cell">
                    <button 
                      mat-button 
                      (click)="openSwitchDialog(worker)" 
                      [disabled]="!worker.running || !isWorkerOwned(worker)"
                      title="Switch this worker to another mode">
                      Switch Mode
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

      <!-- Switch Worker Mode Form -->
      <mat-card class="switch-card" *ngIf="selectedWorker()">
        <mat-card-header>
          <mat-card-title>Switch Worker Mode</mat-card-title>
        </mat-card-header>
        <mat-card-content>
          <p><strong>Worker:</strong> {{ selectedWorker()?.worker_id }}</p>
          <p><strong>Current Mode:</strong> {{ selectedWorker()?.mode }}</p>

          <div class="form-field">
            <mat-form-field appearance="outline" class="full-width">
              <mat-label>Target Mode</mat-label>
              <mat-select [(value)]="targetMode">
                <mat-option value="standalone">Standalone</mat-option>
                <mat-option value="pool-coordinator">Pool Coordinator</mat-option>
                <mat-option value="pool-worker">Pool Worker</mat-option>
              </mat-select>
            </mat-form-field>
          </div>

          <div class="form-field" *ngIf="targetMode() === 'pool-worker'">
            <mat-form-field appearance="outline" class="full-width">
              <mat-label>Pool Coordinator URL</mat-label>
              <input matInput [(ngModel)]="poolUrl" placeholder="http://pool-coordinator:9001">
            </mat-form-field>
          </div>
        </mat-card-content>
        <mat-card-actions class="form-actions">
          <button mat-button (click)="cancelSwitch()">Cancel</button>
          <button mat-raised-button color="primary" (click)="confirmSwitch()" [disabled]="!canSwitch()">
            Switch Mode
          </button>
        </mat-card-actions>
      </mat-card>

      <!-- Configure Policy Form -->
      <mat-card class="policy-card" *ngIf="selectedPoolCoordinator()">
        <mat-card-header>
          <mat-card-title>Configure Pool Voting Policy</mat-card-title>
        </mat-card-header>
        <mat-card-content>
          <p><strong>Pool Coordinator:</strong> {{ selectedPoolCoordinator()?.worker_id }}</p>
          <div *ngIf="poolHealth()">
            <p><strong>Miners Connected:</strong> {{ poolHealth()?.miners }}</p>
            <p><strong>Current Policy:</strong> {{ poolHealth()?.voting_policy?.decision }}</p>
          </div>

          <div class="form-field">
            <mat-form-field appearance="outline" class="full-width">
              <mat-label>Decision</mat-label>
              <mat-select [(value)]="policyDecision">
                <mat-option value="accept">Accept All</mat-option>
                <mat-option value="reject">Reject Specific</mat-option>
              </mat-select>
            </mat-form-field>
          </div>

          <div class="form-field" *ngIf="policyDecision() === 'reject'">
            <mat-form-field appearance="outline" class="full-width">
              <mat-label>Action to Reject</mat-label>
              <mat-select [(value)]="policyAction">
                <mat-option value="promulgacion">Promulgación</mat-option>
                <mat-option value="derogacion">Derogación</mat-option>
                <mat-option value="">Reject All</mat-option>
              </mat-select>
            </mat-form-field>
          </div>
        </mat-card-content>
        <mat-card-actions class="form-actions">
          <button mat-button (click)="cancelPolicy()">Cancel</button>
          <button mat-raised-button color="primary" (click)="confirmPolicy()">
            Update Policy
          </button>
        </mat-card-actions>
      </mat-card>

      <div class="info-section">
        <h3>Worker Modes</h3>
        <ul>
          <li><strong>Standalone:</strong> Worker mines independently by subscribing to NCT challenges.</li>
          <li><strong>Pool Coordinator:</strong> Worker acts as a pool leader, fragments work, and manages pool workers.</li>
          <li><strong>Pool Worker:</strong> Worker connects to a pool coordinator and mines assigned fragments.</li>
        </ul>
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
      align-items: center;
      margin-bottom: 30px;
    }
    h1 {
      color: #e0e0e0;
      margin: 0;
      font-weight: 500;
    }
    .register-card, .workers-card, .switch-card, .policy-card {
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
    ::ng-deep .mat-mdc-form-field {
      --mdc-outlined-text-field-outline-color: #444;
      --mdc-outlined-text-field-label-text-color: #888;
    }
    ::ng-deep .mat-mdc-select-value {
      color: #e0e0e0;
    }
    ::ng-deep .mat-mdc-input-element {
      color: #e0e0e0;
    }
  `]
})
export class WorkersComponent implements OnInit {
  private apiService = inject(ApiService);
  identityService = inject(IdentityService);
  private snackBar = inject(MatSnackBar);

  workers = signal<WorkerStatus[]>([]);
  displayedColumns: string[] = ['worker_id', 'pubkey', 'mode', 'pool_url', 'policy', 'running', 'actions'];
  selectedWorker = signal<WorkerStatus | null>(null);
  targetMode = signal<string>('standalone');
  poolUrl = signal<string>('');
  selectedPoolCoordinator = signal<WorkerStatus | null>(null);
  poolHealth = signal<PoolHealth | null>(null);
  policyDecision = signal<string>('accept');
  policyAction = signal<string>('');
  poolPolicies = signal<Record<string, PoolPolicy>>({});

  showRegisterForm = signal(false);
  newWorkerId = signal('');
  registering = signal(false);

  ngOnInit() {
    this.loadWorkers();
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
    if (!policy.action) {
      return 'Reject All';
    }
    const actions = policy.action.split(',').map(a => a.trim()).sort();
    if (actions.length === 2 && actions.includes('promulgacion') && actions.includes('derogacion')) {
      return 'Reject All';
    }
    return `Reject: ${policy.action}`;
  }

  openSwitchDialog(worker: WorkerStatus) {
    this.selectedWorker.set(worker);
    this.targetMode.set(worker.mode);
    this.poolUrl.set(worker.pool_url || '');
  }

  cancelSwitch() {
    this.selectedWorker.set(null);
    this.targetMode.set('standalone');
    this.poolUrl.set('');
  }

  canSwitch(): boolean {
    const mode = this.targetMode();
    if (mode === 'pool-worker' && !this.poolUrl()) {
      return false;
    }
    return true;
  }

  confirmSwitch() {
    const worker = this.selectedWorker();
    if (!worker) return;

    const request = {
      target: this.targetMode(),
      pool_url: this.poolUrl()
    };

    this.apiService.switchWorkerMode(worker.worker_id, request).subscribe({
      next: () => {
        this.loadWorkers();
        this.cancelSwitch();
        this.snackBar.open(`Worker mode switch command published for ${worker.worker_id}`, 'Close', { duration: 3000 });
      },
      error: (err) => {
        console.error('Failed to switch worker mode:', err);
        this.snackBar.open('Failed to switch mode: ' + (err.error?.detail || err.message), 'Close', { duration: 4000 });
      }
    });
  }

  openPolicyDialog(worker: WorkerStatus) {
    if (worker.mode !== 'pool-coordinator') {
      this.snackBar.open('Only pool coordinators can have voting policies', 'Close', { duration: 3000 });
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

    this.apiService.setPoolPolicy(pool.worker_id, policy).subscribe({
      next: () => {
        this.loadPoolHealth(pool.worker_id);
        this.loadPoolPolicy(pool.worker_id);
        this.cancelPolicy();
        this.snackBar.open('Pool policy updated successfully', 'Close', { duration: 3000 });
      },
      error: (err) => {
        console.error('Failed to set pool policy:', err);
        this.snackBar.open('Failed to set pool policy: ' + (err.error?.detail || err.message), 'Close', { duration: 4000 });
      }
    });
  }

  // Ownership verification helper in frontend
  isWorkerOwned(worker: WorkerStatus): boolean {
    const id = this.identityService.identity();
    if (!id) return false;

    // 1. Demo workers owned by specific demo accounts
    const demoMappings: Record<string, string[]> = {
      "valentin": ["worker-standalone"],
      "gustavo": ["worker-pool-coordinator"],
      "matt": ["worker-pool-miner-1"],
      "profesor1": ["worker-pool-miner-2"],
      "profesor2": ["worker-pool-miner-3"]
    };

    if (id.isDemo) {
      const owned = demoMappings[id.username || ''] || [];
      return owned.includes(worker.worker_id);
    }

    // 2. Dynamic workers where the worker's reported pubkey matches the citizen pubkey
    return worker.pubkey === id.pubkey;
  }

  isDynamicWorker(worker: WorkerStatus): boolean {
    const hardcoded = ["worker-1", "worker-2", "pool-coordinator-1", "worker-standalone", "worker-pool-coordinator", "worker-pool-miner-1", "worker-pool-miner-2", "worker-pool-miner-3"];
    return !hardcoded.includes(worker.worker_id);
  }

  cancelRegister() {
    this.showRegisterForm.set(false);
    this.newWorkerId.set('');
  }

  async confirmRegister() {
    const id = this.identityService.identity();
    if (!id || id.isDemo) {
      this.snackBar.open('Only custom cryptographic identities can register new nodes.', 'Close', { duration: 3000 });
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
        next: () => {
          this.snackBar.open(`Worker "${workerId}" registered and deployed to cluster successfully!`, 'Close', { duration: 3000 });
          this.cancelRegister();
          this.loadWorkers();
        },
        error: (err) => {
          console.error(err);
          this.snackBar.open('Registration failed: ' + (err.error?.detail || err.message), 'Close', { duration: 4000 });
          this.registering.set(false);
        }
      });
    } catch (err: any) {
      console.error(err);
      this.snackBar.open('Signing failed: ' + err.message, 'Close', { duration: 4000 });
      this.registering.set(false);
    }
  }

  async confirmUnregister(worker: WorkerStatus) {
    if (!confirm(`Are you sure you want to unregister worker "${worker.worker_id}"?`)) {
      return;
    }

    const id = this.identityService.identity();
    if (!id || id.isDemo) {
      this.snackBar.open('Only custom cryptographic identities can unregister nodes.', 'Close', { duration: 3000 });
      return;
    }

    try {
      const timestamp = new Date().toISOString();
      const message = `${worker.worker_id}|delete|${timestamp}`;
      const signature = await this.identityService.sign(message);

      this.apiService.unregisterWorker(worker.worker_id, timestamp, signature).subscribe({
        next: () => {
          this.snackBar.open(`Worker "${worker.worker_id}" unregistered.`, 'Close', { duration: 3000 });
          this.loadWorkers();
        },
        error: (err) => {
          console.error(err);
          this.snackBar.open('Failed to unregister: ' + (err.error?.detail || err.message), 'Close', { duration: 4000 });
        }
      });
    } catch (err: any) {
      console.error(err);
      this.snackBar.open('Signing failed: ' + err.message, 'Close', { duration: 4000 });
    }
  }
}
