import { Component, OnInit, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { Router, RouterModule } from '@angular/router';
import { AccountsService, DemoAccount } from '../../core/services/accounts.service';
import { IdentityService } from '../../core/services/identity.service';

/**
 * Elegir una cuenta demo.
 *
 * Desde el rediseño, la tabla de cuentas vive dentro de `/identity` — es el
 * camino normal y la única entrada desde la barra. Esta pantalla se conserva
 * porque la ruta ya existía y hay enlaces sueltos apuntándole: hace lo mismo en
 * formato de tarjetas y, una vez elegida la cuenta, te lleva al Panel.
 */
@Component({
  selector: 'app-account-selection',
  standalone: true,
  imports: [CommonModule, RouterModule, MatSnackBarModule],
  template: `
    <main class="vc-page vc-page--narrow">
      <div class="vc-head__text">
        <h6 class="vc-kicker">Identidad</h6>
        <h1 class="vc-title">Elegir cuenta demo</h1>
        <p class="vc-lead">
          Una cuenta por worker del despliegue. Se reservan por sesión: mientras la
          tengas tomada, nadie más puede usarla. En modo demo las firmas las resuelve el
          backend, así que no hay ninguna clave privada en este navegador.
        </p>
      </div>

      <div class="vc-grid vc-grid--cards accounts" *ngIf="!loading(); else cargando">
        <div class="card elev-sm acc" *ngFor="let a of accounts()"
             [class.vc-soft--75]="isMine(a)" [class.vc-owned]="isMine(a)"
             [class.vc-plain]="!isMine(a)" [class.acc--busy]="a.status === 'occupied' && !isMine(a)">
          <div class="vc-row acc__head">
            <h4 class="acc__name">{{ a.username }}</h4>
            <span class="tag vc-push"
                  [class.tag-accent]="isMine(a)"
                  [class.tag-neutral]="!isMine(a)">{{ stateLabel(a) }}</span>
          </div>
          <p class="vc-note-sm acc__worker">
            Mina con <code class="mono acc__code">{{ a.worker_id }}</code>
          </p>

          <div class="vc-rule vc-rule--short"></div>

          <div class="vc-actions">
            <span class="vc-label">Modo</span>
            <span class="tag" [ngClass]="modeCls(a.mode)">{{ a.mode }}</span>
          </div>

          <p class="vc-label acc__label">Clave pública</p>
          <code class="mono acc__key" [title]="a.pubkey">{{ a.pubkey.slice(0, 32) }}…</code>

          <p class="vc-note" *ngIf="a.status === 'occupied' && !isMine(a) && a.occupied_at">
            Tomada desde {{ a.occupied_at | date:'HH:mm:ss' }}.
          </p>

          <div class="vc-actions acc__cta">
            <button class="btn btn-primary acc__btn" *ngIf="!isMine(a)"
                    [disabled]="a.status === 'occupied' || busy()" (click)="use(a)">
              Entrar como {{ a.username }}
            </button>
            <button class="btn btn-ghost acc__btn" *ngIf="isMine(a)" (click)="release(a)">
              Liberar
            </button>
          </div>
        </div>
      </div>

      <ng-template #cargando>
        <p class="vc-empty accounts">Cargando cuentas…</p>
      </ng-template>

      <p class="vc-note accounts__alt">
        ¿Preferís tus propias claves? Generá un par ECDSA en este navegador desde
        <a routerLink="/identity">Identidad</a>.
      </p>
    </main>
  `,
  styles: [`
    .accounts { margin-top: 40px; }
    .accounts__alt { margin-top: 40px; }

    .acc { padding: 24px; }
    /* Una cuenta tomada por otra sesión no se oculta: se atenúa. Sigue siendo
       información útil —quién está adentro— aunque no se pueda usar. */
    .acc--busy { opacity: .55; }
    .acc__head { gap: 10px; }
    .acc__name { margin: 0; font-size: 19px; color: var(--color-neutral-100); }
    .acc__worker { margin-top: 8px; }
    .acc__code { font-size: 11.5px; color: var(--color-accent-300); }
    .acc__label { margin: 16px 0 6px; }
    .acc__key { font-size: 11.5px; word-break: break-all; color: var(--color-neutral-500); }
    .acc__cta { margin-top: 20px; }
    .acc__btn { font-size: 13px; }
  `]
})
export class AccountSelectionComponent implements OnInit {
  private accountsService = inject(AccountsService);
  private identityService = inject(IdentityService);
  private router = inject(Router);
  private snackBar = inject(MatSnackBar);

  accounts = signal<DemoAccount[]>([]);
  loading = signal(true);
  busy = signal(false);

  ngOnInit() {
    this.loadAccounts();
  }

  private loadAccounts() {
    this.accountsService.listAccounts().subscribe({
      next: (accounts) => {
        this.accounts.set(accounts);
        this.loading.set(false);
      },
      error: () => {
        this.loading.set(false);
        this.snackBar.open('No se pudieron cargar las cuentas demo.', 'Cerrar', { duration: 3000 });
      },
    });
  }

  isMine(account: DemoAccount): boolean {
    return this.accountsService.selectedAccount()?.username === account.username;
  }

  stateLabel(account: DemoAccount): string {
    if (this.isMine(account)) return 'tu sesión';
    return account.status === 'occupied' ? 'ocupada' : 'libre';
  }

  modeCls(mode: string): string {
    return mode === 'pool-coordinator' ? 'tag-accent'
      : mode === 'standalone' ? 'tag-outline' : 'tag-neutral';
  }

  use(account: DemoAccount) {
    if (account.status === 'occupied') {
      this.snackBar.open('Esa cuenta ya está en uso por otra sesión.', 'Cerrar', { duration: 3000 });
      return;
    }
    this.busy.set(true);
    this.accountsService.reserveAccount(account.username).subscribe({
      next: (res) => {
        this.busy.set(false);
        if (res.status !== 'reserved' && res.status !== 'already_reserved') return;
        this.accountsService.setSelectedAccount(account);
        this.identityService.identity.set({
          pubkey: account.pubkey,
          username: account.username,
          isDemo: true,
        });
        this.snackBar.open(`Entraste como "${account.username}".`, 'Cerrar', { duration: 2500 });
        this.router.navigate(['/dashboard']);
      },
      error: (err) => {
        this.busy.set(false);
        this.snackBar.open(
          err?.status === 409
            ? 'Esa cuenta ya está en uso por otra sesión.'
            : 'No se pudo tomar la cuenta. Probá de nuevo.',
          'Cerrar', { duration: 3000 });
        this.loadAccounts();
      },
    });
  }

  release(account: DemoAccount) {
    this.busy.set(true);
    this.accountsService.releaseAccount(account.username).subscribe({
      next: () => {
        this.busy.set(false);
        this.accountsService.setSelectedAccount(null);
        this.identityService.clearIdentity();
        this.snackBar.open(`Cuenta "${account.username}" liberada.`, 'Cerrar', { duration: 2500 });
        this.loadAccounts();
      },
      error: () => {
        this.busy.set(false);
        this.snackBar.open('No se pudo liberar la cuenta.', 'Cerrar', { duration: 3000 });
      },
    });
  }
}
