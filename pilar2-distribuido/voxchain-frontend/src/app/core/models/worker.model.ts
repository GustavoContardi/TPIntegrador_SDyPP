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
  private_key?: string;
}

/**
 * Arma el alta firmada de un minero nuevo.
 *
 * La firma prueba la posesión de la clave privada sobre
 * `${workerId}|register|${timestamp}`, y el timestamp acota la ventana de
 * replay. La clave privada viaja **sólo** en este alta y con un único destino:
 * el Secret de Kubernetes que monta el pod del minero, para que el minero pueda
 * firmar los nonces que encuentra con la identidad de su dueño. Es el único
 * punto del sistema donde la privada sale del navegador, y es una decisión del
 * usuario al desplegar su propio nodo.
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

  let privateKey: string | undefined;
  if (identity.exportedPrivkey) {
    const lines: string[] = [];
    for (let i = 0; i < identity.exportedPrivkey.length; i += 64) {
      lines.push(identity.exportedPrivkey.slice(i, i + 64));
    }
    privateKey = `-----BEGIN PRIVATE KEY-----\n${lines.join('\n')}\n-----END PRIVATE KEY-----`;
  }

  return {
    worker_id: workerId,
    pubkey: identity.pubkey,
    timestamp,
    signature,
    ...(privateKey ? { private_key: privateKey } : {}),
  };
}
