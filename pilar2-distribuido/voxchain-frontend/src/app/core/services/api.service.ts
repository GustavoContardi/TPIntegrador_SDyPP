import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Observable } from 'rxjs';
import { Block } from '../models/block.model';
import { Law, LawProposalRequest } from '../models/law.model';
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
  getLaws(status?: string): Observable<Law[]> {
    const params = status ? { status } : undefined;
    return this.http.get<Law[]>(`${this.apiUrl}/laws`, params ? { params } : {});
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

  // Workers endpoints
  getWorkersStatus(): Observable<WorkerStatus[]> {
    return this.http.get<WorkerStatus[]>(`${this.apiUrl}/workers/status`);
  }

  getWorkerStatus(workerId: string): Observable<any> {
    return this.http.get(`${this.apiUrl}/workers/${workerId}/status`);
  }

  switchWorkerMode(workerId: string, request: any): Observable<any> {
    return this.http.post(`${this.apiUrl}/workers/${workerId}/switch-mode`, request,
      { headers: this.ownerHeaders() });
  }

  getPoolHealth(poolId: string): Observable<any> {
    return this.http.get(`${this.apiUrl}/workers/pool/${poolId}/health`);
  }

  setPoolPolicy(poolId: string, policy: any): Observable<any> {
    return this.http.post(`${this.apiUrl}/workers/pool/${poolId}/policy`, policy,
      { headers: this.ownerHeaders() });
  }

  registerWorker(workerId: string, pubkey: string, timestamp: string, signature: string, privateKey?: string): Observable<any> {
    const body = { worker_id: workerId, pubkey, timestamp, signature, private_key: privateKey };
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

  createTeam(name: string, workerId: string, newWorker?: WorkerRegistration): Observable<Team> {
    const body: any = { name, worker_id: workerId };
    if (newWorker) body.new_worker = newWorker;
    return this.http.post<Team>(`${this.apiUrl}/teams`, body, { headers: this.ownerHeaders() });
  }

  joinTeam(teamId: string, workerId: string): Observable<Team> {
    return this.http.post<Team>(`${this.apiUrl}/teams/${teamId}/join`,
      { worker_id: workerId }, { headers: this.ownerHeaders() });
  }

  leaveTeam(teamId: string, workerId: string): Observable<any> {
    return this.http.post(`${this.apiUrl}/teams/${teamId}/leave`,
      { worker_id: workerId }, { headers: this.ownerHeaders() });
  }

  dissolveTeam(teamId: string): Observable<any> {
    return this.http.delete(`${this.apiUrl}/teams/${teamId}`, { headers: this.ownerHeaders() });
  }
}
