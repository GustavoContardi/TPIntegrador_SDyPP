import { Routes } from '@angular/router';
import { identityGuard } from './core/guards/identity.guard';

/**
 * El mapa de la app, dividido en dos por un criterio: **mirar es público,
 * actuar pide identidad.**
 *
 * Todo lo que sólo lee el estado de la red —la portada, el panel, la cadena,
 * las leyes, el estado del sistema, los mineros— se ve sin registrarse. Es una
 * blockchain de gobierno: su registro es público por definición, y pedir
 * identidad para leerlo contradecía el proyecto. Además rompía la barra, que
 * ofrece esos destinos siempre y rebotaba a /identity al tocarlos.
 *
 * El guard queda sólo donde la pantalla existe para escribir: proponer una ley
 * y participar de una ventana. Igual no es él quien protege nada — la
 * autorización real la hace el backend exigiendo la firma del dueño sobre cada
 * acción; esto sólo evita mostrar un formulario que no se va a poder enviar.
 */
export const routes: Routes = [
  // ── públicas: leer la red no requiere identidad ────────────────────────
  {
    path: '',
    loadComponent: () => import('./features/home/home.component').then(m => m.HomeComponent)
  },
  {
    path: 'dashboard',
    loadComponent: () => import('./features/dashboard/dashboard.component').then(m => m.DashboardComponent)
  },
  {
    path: 'chain',
    loadComponent: () => import('./features/chain/chain.component').then(m => m.ChainComponent)
  },
  {
    path: 'laws',
    loadComponent: () => import('./features/laws/laws.component').then(m => m.LawsComponent)
  },
  {
    path: 'health',
    loadComponent: () => import('./features/health/health.component').then(m => m.HealthComponent)
  },
  // Todo lo que escribe (fundar equipo, unirse, cambiar de modo) lo autoriza el
  // backend por dueño, así que la pantalla queda de sólo lectura para quien no
  // se registró.
  {
    path: 'workers',
    loadComponent: () => import('./features/workers/workers.component').then(m => m.WorkersComponent)
  },

  // ── identidad ──────────────────────────────────────────────────────────
  {
    path: 'identity',
    loadComponent: () => import('./features/identity/identity.component').then(m => m.IdentityComponent)
  },

  // ── con identidad: son pantallas para hacer algo, no para mirar ────────
  {
    path: 'propose',
    loadComponent: () => import('./features/propose-law/propose-law.component').then(m => m.ProposeLawComponent),
    canActivate: [identityGuard]
  },
  {
    path: 'queue',
    loadComponent: () => import('./features/queue/queue.component').then(m => m.QueueComponent),
    canActivate: [identityGuard]
  },

  // Los equipos viven dentro de la página de mineros. La ruta se conserva para
  // que no se rompan los enlaces que ya existían.
  { path: 'teams', redirectTo: 'workers', pathMatch: 'full' },
  { path: '**', redirectTo: '' }
];
