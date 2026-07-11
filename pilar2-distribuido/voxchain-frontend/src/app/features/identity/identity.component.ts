import { Component, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { Router, RouterModule } from '@angular/router';
import { IdentityService } from '../../core/services/identity.service';
import { AccountsService } from '../../core/services/accounts.service';

@Component({
  selector: 'app-identity',
  standalone: true,
  imports: [
    CommonModule,
    MatCardModule,
    MatButtonModule,
    MatIconModule,
    MatSnackBarModule,
    RouterModule,
  ],
  template: `
    <div class="identity-container">
      <h1>Identity & Node Registration</h1>

      <div class="identity-grid" *ngIf="!identityService.identity()">
        <!-- Demo Accounts Card -->
        <mat-card class="identity-card demo-card">
          <mat-card-header>
            <mat-icon mat-card-avatar class="card-icon">people</mat-icon>
            <mat-card-title>Use Demo Account</mat-card-title>
            <mat-card-subtitle>Quick access to preconfigured nodes</mat-card-subtitle>
          </mat-card-header>
          <mat-card-content>
            <p>Access VoxChain using one of the 5 demo accounts. The system will handle the cryptographic signatures on the backend for you.</p>
          </mat-card-content>
          <mat-card-actions>
            <button mat-raised-button color="primary" routerLink="/select-account">
              Select Demo Account
            </button>
          </mat-card-actions>
        </mat-card>

        <!-- Custom Account Card -->
        <mat-card class="identity-card custom-card">
          <mat-card-header>
            <mat-icon mat-card-avatar class="card-icon">vpn_key</mat-icon>
            <mat-card-title>Register Custom Node Identity</mat-card-title>
            <mat-card-subtitle>True cryptographic self-sovereignty</mat-card-subtitle>
          </mat-card-header>
          <mat-card-content>
            <p>Generate a new ECDSA P-256 keypair locally in your browser. Your private key never leaves your browser, and you will sign proposals locally.</p>
          </mat-card-content>
          <mat-card-actions>
            <button mat-raised-button color="accent" (click)="generate()" [disabled]="generating()">
              {{ generating() ? 'Generating...' : 'Generate New Keypair' }}
            </button>
          </mat-card-actions>
        </mat-card>
      </div>

      <!-- Identity Active Card -->
      <mat-card class="active-identity-card" *ngIf="identityService.identity() as id">
        <mat-card-header>
          <mat-icon mat-card-avatar class="active-icon" [color]="identityService.isDemoMode() ? 'primary' : 'accent'">
            {{ identityService.isDemoMode() ? 'admin_panel_settings' : 'shield' }}
          </mat-icon>
          <mat-card-title>Identity Active</mat-card-title>
          <mat-card-subtitle>
            {{ identityService.isDemoMode() ? 'Demo Mode' : 'Custom Cryptographic Node Identity' }}
          </mat-card-subtitle>
        </mat-card-header>
        <mat-card-content>
          <div class="account-info" *ngIf="identityService.isDemoMode()">
            <p><strong>Username:</strong> <span class="account-name">{{ identityService.getUsername() }}</span></p>
            <p class="demo-badge">Backend-Managed Signature</p>
          </div>

          <div class="key-field">
            <div class="key-header">
              <strong>Public Key (SPKI Base64):</strong>
              <button mat-icon-button (click)="copyToClipboard(id.pubkey)" title="Copy Public Key">
                <mat-icon class="small-icon">content_copy</mat-icon>
              </button>
            </div>
            <code class="key-block pubkey-block">{{ id.pubkey }}</code>
          </div>

          <div class="key-field" *ngIf="!identityService.isDemoMode() && id.exportedPrivkey">
            <div class="key-header">
              <strong>Private Key (Standard PEM Format):</strong>
              <div class="key-actions">
                <button mat-icon-button (click)="showPrivateKey.set(!showPrivateKey())" [title]="showPrivateKey() ? 'Hide Private Key' : 'Show Private Key'">
                  <mat-icon class="small-icon">{{ showPrivateKey() ? 'visibility_off' : 'visibility' }}</mat-icon>
                </button>
                <button mat-icon-button (click)="copyToClipboard(formatAsPem(id.exportedPrivkey))" title="Copy PEM Private Key">
                  <mat-icon class="small-icon">content_copy</mat-icon>
                </button>
              </div>
            </div>
            <pre class="key-block privkey-block" *ngIf="showPrivateKey()">{{ formatAsPem(id.exportedPrivkey) }}</pre>
            <div class="key-placeholder" *ngIf="!showPrivateKey()">••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••</div>
            
            <div class="warning-banner">
              <mat-icon class="warning-icon">warning</mat-icon>
              <div class="warning-text">
                <strong>Important:</strong> Save the private key as a <code>.pem</code> file on your computer. To run a voter/miner node with this identity, start your worker with the environment variable:
                <br>
                <code>WORKER_PRIVKEY_PEM=/path/to/private_key.pem</code>
              </div>
            </div>
          </div>
        </mat-card-content>
        <mat-card-actions class="active-actions">
          <button mat-raised-button color="warn" (click)="clear()" *ngIf="!identityService.isDemoMode()">
            Clear Identity
          </button>
          <button mat-button color="primary" (click)="changeAccount()" *ngIf="identityService.isDemoMode()">
            Change Account
          </button>
        </mat-card-actions>
      </mat-card>
    </div>
  `,
  styles: [`
    .identity-container {
      padding: 40px 20px;
      max-width: 1000px;
      margin: 0 auto;
    }
    h1 {
      color: #e0e0e0;
      margin-bottom: 30px;
      font-weight: 500;
    }
    .identity-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 24px;
    }
    .identity-card {
      background-color: #1e1e1e;
      color: #e0e0e0;
      border: 1px solid #333;
      border-radius: 8px;
      transition: all 0.3s ease;
      display: flex;
      flex-direction: column;
      height: 100%;
    }
    .identity-card:hover {
      transform: translateY(-4px);
      box-shadow: 0 8px 16px rgba(0, 0, 0, 0.4);
      border-color: #555;
    }
    .card-icon {
      font-size: 32px;
      height: 32px;
      width: 32px;
      color: #90caf9;
      margin-right: 12px;
    }
    mat-card-title {
      color: #e0e0e0;
      font-size: 1.25rem;
      font-weight: 600;
    }
    mat-card-subtitle {
      color: #888;
    }
    mat-card-content {
      padding: 16px;
      flex-grow: 1;
      color: #b0b0b0;
      line-height: 1.6;
    }
    mat-card-actions {
      padding: 16px;
      display: flex;
      justify-content: flex-end;
    }
    .active-identity-card {
      background-color: #1e1e1e;
      color: #e0e0e0;
      border: 1px solid #333;
      border-radius: 8px;
      padding: 16px;
    }
    .active-icon {
      font-size: 32px;
      height: 32px;
      width: 32px;
      margin-right: 12px;
    }
    .account-info {
      margin: 16px 0;
      padding: 16px;
      background-color: #2a2a2a;
      border-radius: 6px;
      border-left: 4px solid #2196f3;
    }
    .account-name {
      color: #2196f3;
      font-weight: 600;
      font-size: 1.1rem;
    }
    .demo-badge {
      color: #4caf50;
      font-weight: 600;
      margin-top: 8px;
      font-size: 0.9rem;
    }
    .key-field {
      margin: 24px 0;
    }
    .key-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
      color: #e0e0e0;
    }
    .key-actions {
      display: flex;
      gap: 4px;
    }
    .small-icon {
      font-size: 18px;
      height: 18px;
      width: 18px;
      color: #888;
    }
    .small-icon:hover {
      color: #fff;
    }
    .key-block {
      font-family: 'Courier New', monospace;
      font-size: 0.85rem;
      background-color: #0c0c0c;
      padding: 16px;
      border-radius: 6px;
      display: block;
      border: 1px solid #222;
      overflow-x: auto;
    }
    .pubkey-block {
      color: #90caf9;
      word-break: break-all;
      white-space: pre-wrap;
    }
    .privkey-block {
      color: #a5d6a7;
      margin: 0;
      white-space: pre;
    }
    .key-placeholder {
      font-family: 'Courier New', monospace;
      font-size: 0.85rem;
      background-color: #0c0c0c;
      color: #444;
      padding: 16px;
      border-radius: 6px;
      border: 1px solid #222;
      letter-spacing: 2px;
    }
    .warning-banner {
      margin-top: 24px;
      background-color: rgba(255, 152, 0, 0.1);
      border-left: 4px solid #ff9800;
      border-radius: 6px;
      padding: 16px;
      display: flex;
      gap: 16px;
      align-items: flex-start;
    }
    .warning-icon {
      color: #ff9800;
      font-size: 28px;
      height: 28px;
      width: 28px;
    }
    .warning-text {
      color: #ffb74d;
      font-size: 0.95rem;
      line-height: 1.6;
    }
    .warning-text code {
      background-color: rgba(0, 0, 0, 0.3);
      padding: 2px 6px;
      border-radius: 4px;
      color: #fff;
      font-family: monospace;
    }
    .active-actions {
      margin-top: 24px;
      padding: 0;
      display: flex;
      justify-content: flex-start;
    }
  `]
})
export class IdentityComponent {
  identityService = inject(IdentityService);
  accountsService = inject(AccountsService);
  router = inject(Router);
  snackBar = inject(MatSnackBar);

  generating = signal(false);
  showPrivateKey = signal(false);

  async generate() {
    this.generating.set(true);
    try {
      await this.identityService.generateKeypair();
      this.snackBar.open('Custom cryptographic identity generated locally!', 'Close', { duration: 3000 });
    } catch (err: any) {
      console.error(err);
      this.snackBar.open('Generation failed: ' + err.message, 'Close', { duration: 3000 });
    } finally {
      this.generating.set(false);
    }
  }

  clear() {
    this.identityService.clearIdentity();
    this.showPrivateKey.set(false);
    this.snackBar.open('Identity cleared.', 'Close', { duration: 2000 });
  }

  changeAccount() {
    this.accountsService.clearSession();
    this.identityService.clearIdentity();
    this.router.navigate(['/select-account']);
  }

  formatAsPem(b64: string | null): string {
    if (!b64) return '';
    const header = '-----BEGIN PRIVATE KEY-----\n';
    const footer = '\n-----END PRIVATE KEY-----';
    const regex = /.{1,64}/g;
    const lines = b64.match(regex) || [b64];
    return header + lines.join('\n') + footer;
  }

  copyToClipboard(text: string) {
    navigator.clipboard.writeText(text);
    this.snackBar.open('Copied to clipboard!', 'Close', { duration: 2000 });
  }
}
