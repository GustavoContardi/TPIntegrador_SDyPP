import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Observable, from, switchMap } from 'rxjs';
import { Block } from '../models/block.model';
import { Law, LawCategory, LawProposalRequest } from '../models/law.model';
import { Window } from '../models/window.model';
import { Team, WorkerRegistration, WorkerStatus } from '../models/worker.model';
import { IdentityService } from './identity.service';

@Injectable({
  providedIn: 'root'
})
export class ApiService {
  private apiUrl = '/api';
  private identityService = inject(IdentityService);

  private getOwnerId(): string {
    const id = this.identityService.identity();
    if (!id) return 'default';
    if (id.isDemo) {
      return id.username || 'default';
    }
    return id.pubkey;
  }

  constructor(private http: HttpClient) {}

  // Chain endpoints
  getChain(): Observable<Block[]> {
    return this.http.get<Block[]>(`${this.apiUrl}/chain`);
  }

  getBlock(blockHash: string): Observable<Block> {
    return this.http.get<Block>(`${this.apiUrl}/chain/${blockHash}`);
  }

  // Laws endpoints
  getLaws(status?: string, category?: string): Observable<Law[]> {
    const params: Record<string, string> = {};
    if (status) params['status'] = status;
    if (category) params['category'] = category;
    return this.http.get<Law[]>(`${this.apiUrl}/laws`,
      Object.keys(params).length ? { params } : {});
  }

  /**
   * Áreas de gobierno disponibles.
   *
   * Se piden al backend en vez de usar sólo la constante del cliente porque los
   * slugs tienen que ser exactamente los que valida el NCT; la constante es el
   * respaldo mientras llega la respuesta.
   */
  getLawCategories(): Observable<LawCategory[]> {
    return this.http.get<LawCategory[]>(`${this.apiUrl}/laws/categories`);
  }

  getLaw(lawId: string): Observable<Law> {
    return this.http.get<Law>(`${this.apiUrl}/laws/${lawId}`);
  }

  getLawText(lawId: string): Observable<string> {
    return this.http.get(`${this.apiUrl}/laws/${lawId}/text`, { responseType: 'text' });
  }

  getNextLaw(): Observable<Law | null> {
    return this.http.get<Law | null>(`${this.apiUrl}/laws/next`);
  }

  getLawQueue(): Observable<Law[]> {
    return this.http.get<Law[]>(`${this.apiUrl}/laws/queue`);
  }

  proposeLaw(proposal: LawProposalRequest): Observable<Law> {
    return this.http.post<Law>(`${this.apiUrl}/laws`, proposal);
  }

  // Windows endpoints
  getActiveWindow(): Observable<Window> {
    return this.http.get<Window>(`${this.apiUrl}/windows/active`);
  }

  getWindow(windowId: string): Observable<Window> {
    return this.http.get<Window>(`${this.apiUrl}/windows/${windowId}`);
  }

  // Health endpoint
  getHealth(): Observable<any> {
    return this.http.get(`${this.apiUrl}/health`);
  }

  private ownerHeaders(): HttpHeaders {
    return new HttpHeaders().set('X-Owner-Id', this.getOwnerId());
  }

  /**
   * Cabeceras de una acción de administración, con la firma que la autoriza.
   *
   * El backend verifica `recurso|acción|timestamp` contra el dueño que tiene
   * guardado. Antes alcanzaba con `X-Owner-Id`, que lo elige quien llama y
   * contiene una pubkey que está en cada bloque de la cadena: cualquiera que
   * supiera a quién imitar podía sacarle un minero de su equipo a otro.
   *
   * En modo demo no se firma —la privada la tiene el backend, no el navegador—
   * y esas cuentas siguen el camino custodial de siempre.
   */
  private async signedHeaders(resourceId: string, action: string): Promise<HttpHeaders> {
    const headers = this.ownerHeaders();
    const id = this.identityService.identity();
    if (!id || id.isDemo) return headers;

    const timestamp = new Date().toISOString();
    const signature = await this.identityService.sign(`${resourceId}|${action}|${timestamp}`);
    return headers.set('X-Timestamp', timestamp).set('X-Signature', signature);
  }

  /** Ejecuta `call` con las cabeceras firmadas de esa acción. */
  private signed<T>(resourceId: string, action: string,
                    call: (headers: HttpHeaders) => Observable<T>): Observable<T> {
    return from(this.signedHeaders(resourceId, action)).pipe(switchMap(call));
  }

  // Workers endpoints
  getWorkersStatus(): Observable<WorkerStatus[]> {
    return this.http.get<WorkerStatus[]>(`${this.apiUrl}/workers/status`);
  }

  getWorkerStatus(workerId: string): Observable<any> {
    return this.http.get(`${this.apiUrl}/workers/${workerId}/status`);
  }

  switchWorkerMode(workerId: string, request: any): Observable<any> {
    return this.signed(workerId, 'switch-mode', (headers) =>
      this.http.post(`${this.apiUrl}/workers/${workerId}/switch-mode`, request, { headers }));
  }

  getPoolHealth(poolId: string): Observable<any> {
    return this.http.get(`${this.apiUrl}/workers/pool/${poolId}/health`);
  }

  setPoolPolicy(poolId: string, policy: any): Observable<any> {
    return this.signed(poolId, 'pool-policy', (headers) =>
      this.http.post(`${this.apiUrl}/workers/pool/${poolId}/policy`, policy, { headers }));
  }

  registerWorker(workerId: string, pubkey: string, timestamp: string, signature: string, deploy = true): Observable<any> {
    const body = { worker_id: workerId, pubkey, timestamp, signature, deploy };
    return this.http.post(`${this.apiUrl}/workers/register`, body);
  }

  unregisterWorker(workerId: string, timestamp: string, signature: string): Observable<any> {
    const headers = new HttpHeaders()
      .set('X-Signature', signature)
      .set('X-Timestamp', timestamp);
    return this.http.delete(`${this.apiUrl}/workers/${workerId}`, { headers });
  }

  // Teams endpoints
  //
  // Entrar al modo cooperativo se hace por acá y sólo por acá: el backend
  // rechaza un switch manual a pool-worker/pool-coordinator, justamente para
  // que el modo del minero y su pertenencia a un equipo no puedan divergir.
  listTeams(): Observable<Team[]> {
    return this.http.get<Team[]>(`${this.apiUrl}/teams`);
  }

  getTeam(teamId: string): Observable<Team> {
    return this.http.get<Team>(`${this.apiUrl}/teams/${teamId}`);
  }

  createTeam(name: string, workerId: string, categories: string[] = [],
             newWorker?: WorkerRegistration): Observable<Team> {
    const body: any = { name, worker_id: workerId, categories };
    if (newWorker) body.new_worker = newWorker;
    return this.signed(workerId, 'create-team', (headers) =>
      this.http.post<Team>(`${this.apiUrl}/teams`, body, { headers }));
  }

  /**
   * Cambia las áreas de ley que vota el equipo. Lista vacía = vuelve a votar todo.
   *
   * El backend baja la agenda al coordinador en la misma operación, así que
   * cuando esto responde el equipo ya está minando (o ignorando) lo que
   * corresponde.
   */
  setTeamCategories(teamId: string, categories: string[]): Observable<Team> {
    return this.signed(teamId, 'set-categories', (headers) =>
      this.http.put<Team>(`${this.apiUrl}/teams/${teamId}/categories`, { categories }, { headers }));
  }

  joinTeam(teamId: string, workerId: string): Observable<Team> {
    return this.signed(workerId, 'join-team', (headers) =>
      this.http.post<Team>(`${this.apiUrl}/teams/${teamId}/join`, { worker_id: workerId }, { headers }));
  }

  leaveTeam(teamId: string, workerId: string): Observable<any> {
    return this.signed(workerId, 'leave-team', (headers) =>
      this.http.post(`${this.apiUrl}/teams/${teamId}/leave`, { worker_id: workerId }, { headers }));
  }

  dissolveTeam(teamId: string): Observable<any> {
    return this.signed(teamId, 'dissolve-team', (headers) =>
      this.http.delete(`${this.apiUrl}/teams/${teamId}`, { headers }));
  }
}
