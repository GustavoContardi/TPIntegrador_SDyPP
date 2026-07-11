import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Observable } from 'rxjs';
import { Block } from '../models/block.model';
import { Law, LawProposalRequest } from '../models/law.model';
import { Window } from '../models/window.model';
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

  // Workers endpoints
  getWorkersStatus(): Observable<any[]> {
    return this.http.get<any[]>(`${this.apiUrl}/workers/status`);
  }

  getWorkerStatus(workerId: string): Observable<any> {
    return this.http.get(`${this.apiUrl}/workers/${workerId}/status`);
  }

  switchWorkerMode(workerId: string, request: any): Observable<any> {
    const headers = new HttpHeaders().set('X-Owner-Id', this.getOwnerId());
    return this.http.post(`${this.apiUrl}/workers/${workerId}/switch-mode`, request, { headers });
  }

  getPoolHealth(poolId: string): Observable<any> {
    return this.http.get(`${this.apiUrl}/workers/pool/${poolId}/health`);
  }

  setPoolPolicy(poolId: string, policy: any): Observable<any> {
    const headers = new HttpHeaders().set('X-Owner-Id', this.getOwnerId());
    return this.http.post(`${this.apiUrl}/workers/pool/${poolId}/policy`, policy, { headers });
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
}
