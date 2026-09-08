import { Identity, IdentityService } from '../services/identity.service';

export interface WorkerStatus {
  worker_id: string;
  mode: string;
  pool_url: string;
  running: boolean;
  pubkey?: string;
  /** Dirección HTTP con la que el worker es alcanzable si coordina un equipo. */
  address?: string | null;
  team_id?: string | null;
  team_name?: string | null;
  team_role?: 'coordinator' | 'member' | null;
}

export interface TeamMember {
  worker_id: string;
  role: 'coordinator' | 'member';
  mode: string;
  running: boolean;
  pubkey?: string | null;
}

export interface Team {
  team_id: string;
  name: string;
  owner: string;
  coordinator_worker_id: string;
  coordinator_url: string;
  created_at: string;
  /**
   * Áreas de ley sobre las que vota el equipo. Vacío = vota todas.
   *
   * No es cosmético: con agenda declarada, el coordinador del equipo ignora las
   * ventanas de otras áreas y ni él ni sus mineros aportan un solo hash a esas
   * leyes.
   */
  categories: string[];
  members: TeamMember[];
  member_count: number;
  /** Mineros con keep-alive vivo contra el coordinador. Puede diferir de member_count. */
  miners_connected?: number | null;
  coordinator_online: boolean;
}

/** Mineros de las cuentas de demo, que no tienen clave privada en el navegador. */
const DEMO_WORKERS: Record<string, string[]> = {
  valentin: ['worker-standalone'],
  gustavo: ['worker-pool-coordinator'],
  matt: ['worker-pool-miner-1'],
  profesor1: ['worker-pool-miner-2'],
  profesor2: ['worker-pool-miner-3'],
};

/**
 * Si el minero pertenece a la identidad activa.
 *
 * El backend hace la verificación autoritativa (cabecera `X-Owner-Id` contra
 * `worker:owner:*` en Redis); esto es sólo para decidir qué botones mostrar.
 */
export function isOwnedBy(worker: WorkerStatus, identity: Identity | null): boolean {
  if (!identity) return false;
  if (identity.isDemo) {
    return (DEMO_WORKERS[identity.username || ''] || []).includes(worker.worker_id);
  }
  return worker.pubkey === identity.pubkey;
}

/** El worker es dinámico (lo registró un usuario), no uno de los precargados. */
export function isDynamicWorker(worker: WorkerStatus): boolean {
  const preconfigured = [
    'worker-1', 'worker-2', 'pool-coordinator-1',
    'worker-standalone', 'worker-pool-coordinator',
    'worker-pool-miner-1', 'worker-pool-miner-2', 'worker-pool-miner-3',
  ];
  return !preconfigured.includes(worker.worker_id);
}

export interface WorkerRegistration {
  worker_id: string;
  pubkey: string;
  timestamp: string;
  signature: string;
  /** Si el alta además levanta el pod del minero en el clúster. */
  deploy?: boolean;
}

/**
 * Arma el alta firmada de un minero nuevo.
 *
 * La firma prueba la posesión de la clave privada sobre
 * `${workerId}|register|${timestamp}`, y el timestamp acota la ventana de
 * replay. **La clave privada no viaja**: el minero genera la suya al arrancar
 * dentro de su propio proceso y la vincula a este dueño con un token de un solo
 * uso. Antes el alta subía la privada del ciudadano al backend para que el pod
 * firmara con ella, lo que le daba a cualquiera con acceso al clúster la
 * capacidad de votar como esa persona indefinidamente.
 */
export async function buildRegistration(
  identityService: IdentityService,
  workerId: string,
): Promise<WorkerRegistration> {
  const identity = identityService.identity();
  if (!identity || identity.isDemo) {
    throw new Error('Sólo las identidades propias pueden registrar mineros.');
  }
  const timestamp = new Date().toISOString();
  const signature = await identityService.sign(`${workerId}|register|${timestamp}`);

  return {
    worker_id: workerId,
    pubkey: identity.pubkey,
    timestamp,
    signature,
    deploy: true,
  };
}
