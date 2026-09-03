import { Routes } from '@angular/router';
import { accountSelectedGuard } from './core/guards/account-selected.guard';

export const routes: Routes = [
  // La portada es pública: explica el proyecto antes de pedir nada. Antes la
  // raíz era un guard que rebotaba a /dashboard o /identity, así que no había
  // ningún lugar donde contar de qué se trata.
  {
    path: '',
    loadComponent: () => import('./features/home/home.component').then(m => m.HomeComponent)
  },
  { 
    path: 'select-account', 
    loadComponent: () => import('./features/account-selection/account-selection.component').then(m => m.AccountSelectionComponent) 
  },
  { 
    path: 'dashboard', 
    loadComponent: () => import('./features/dashboard/dashboard.component').then(m => m.DashboardComponent),
    canActivate: [accountSelectedGuard]
  },
  { 
    path: 'chain', 
    loadComponent: () => import('./features/chain/chain.component').then(m => m.ChainComponent),
    canActivate: [accountSelectedGuard]
  },
  { 
    path: 'laws', 
    loadComponent: () => import('./features/laws/laws.component').then(m => m.LawsComponent),
    canActivate: [accountSelectedGuard]
  },
  { 
    path: 'health', 
    loadComponent: () => import('./features/health/health.component').then(m => m.HealthComponent),
    canActivate: [accountSelectedGuard]
  },
  { 
    path: 'identity', 
    loadComponent: () => import('./features/identity/identity.component').then(m => m.IdentityComponent)
  },
  { 
    path: 'propose', 
    loadComponent: () => import('./features/propose-law/propose-law.component').then(m => m.ProposeLawComponent),
    canActivate: [accountSelectedGuard]
  },
  { 
    path: 'queue', 
    loadComponent: () => import('./features/queue/queue.component').then(m => m.QueueComponent),
    canActivate: [accountSelectedGuard]
  },
  // Pública a propósito: es uno de los dos destinos de la portada, y mostrar el
  // estado de la red no requiere identidad. Todo lo que escribe (fundar equipo,
  // unirse, cambiar de modo) lo autoriza el backend por dueño, así que la
  // pantalla queda de sólo lectura para quien no se registró.
  {
    path: 'workers',
    loadComponent: () => import('./features/workers/workers.component').then(m => m.WorkersComponent)
  },
  // Los equipos viven dentro de la página de mineros. La ruta se conserva para
  // que no se rompan los enlaces que ya existían.
  { path: 'teams', redirectTo: 'workers', pathMatch: 'full' },
  { path: '**', redirectTo: '' }
];

