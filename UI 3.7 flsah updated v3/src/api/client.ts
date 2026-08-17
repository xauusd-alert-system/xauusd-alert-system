/**
 * Centralized Typed API Client.
 * Adheres strictly to the honesty and data envelope contracts.
 * Missing / unavailable data is never converted to a numeric fallback.
 */

import {
  SystemStatusResponse,
  SignalResponse,
  MatrixResponse,
  CorrelationResponse,
  SentimentResponse,
  MonteCarloResponse,
  PositionsResponse,
  DealMetricsResponse,
  InstitutionalMetricsResponse,
  LedgerEventsResponse,
  ExecutionQualityResponse,
  ProvenanceAuditResponse,
} from '../types/contracts.js';

export class ApiClient {
  private static tokenKey = 'xau_alert_owner_token';

  public static getOwnerToken(): string | null {
    if (typeof window === 'undefined') return null;
    return sessionStorage.getItem(this.tokenKey) || null;
  }

  public static setOwnerToken(token: string) {
    if (typeof window === 'undefined') return;
    if (!token) {
      sessionStorage.removeItem(this.tokenKey);
    } else {
      sessionStorage.setItem(this.tokenKey, token.trim());
    }
  }

  private static async fetchWithAuth<T>(url: string, requiresAuth = false): Promise<T | null> {
    const headers: Record<string, string> = {};
    const token = this.getOwnerToken();

    if (requiresAuth && token) {
      headers['Authorization'] = `Bearer ${token}`;
    }

    try {
      const response = await fetch(url, { headers });
      if (response.status === 403) {
        throw new Error('OWNER_AUTH_REQUIRED');
      }
      if (response.status === 503 || response.status === 501) {
        const errorJson = await response.json().catch(() => ({}));
        return {
          available: false,
          status_code: response.status,
          detail: errorJson.detail || 'Service unavailable',
        } as unknown as T;
      }
      if (!response.ok) {
        throw new Error(`HTTP_${response.status}`);
      }
      return await response.json();
    } catch (err: any) {
      console.warn(`Fetch error for ${url}:`, err.message);
      return null;
    }
  }

  public static async getStatus(): Promise<SystemStatusResponse | null> {
    return this.fetchWithAuth<SystemStatusResponse>('/api/status');
  }

  public static async getSignal(asset: string = 'XAUUSD', nCandles: number = 300): Promise<SignalResponse | null> {
    return this.fetchWithAuth<SignalResponse>(`/signal?asset=${encodeURIComponent(asset)}&n_candles=${nCandles}`);
  }

  public static async getSignalMatrix(): Promise<MatrixResponse | null> {
    return this.fetchWithAuth<MatrixResponse>('/api/matrix');
  }

  public static async getCorrelation(): Promise<CorrelationResponse | null> {
    return this.fetchWithAuth<CorrelationResponse>('/api/correlation');
  }

  public static async getSentiment(): Promise<SentimentResponse | null> {
    return this.fetchWithAuth<SentimentResponse>('/api/sentiment');
  }

  public static async getMonteCarlo(simulations: number = 1000): Promise<MonteCarloResponse | null> {
    return this.fetchWithAuth<MonteCarloResponse>(`/api/monte-carlo?simulations=${simulations}`);
  }

  public static async getPositions(): Promise<PositionsResponse | null> {
    return this.fetchWithAuth<PositionsResponse>('/api/positions');
  }

  public static async getMetrics(period: string = 'week'): Promise<DealMetricsResponse | null> {
    return this.fetchWithAuth<DealMetricsResponse>(`/api/metrics?period=${encodeURIComponent(period)}`);
  }

  public static async getInstitutionalMetrics(): Promise<InstitutionalMetricsResponse | null> {
    return this.fetchWithAuth<InstitutionalMetricsResponse>('/api/institutional-metrics');
  }

  public static async getPaperStatus(): Promise<any | null> {
    return this.fetchWithAuth<any>('/api/paper-status');
  }

  // Owner endpoints
  public static async getLedgerEvents(limit = 100, sinceMs?: number): Promise<LedgerEventsResponse | null> {
    const query = sinceMs ? `?limit=${limit}&since_ms=${sinceMs}` : `?limit=${limit}`;
    return this.fetchWithAuth<LedgerEventsResponse>(`/api/ledger/events${query}`, true);
  }

  public static async getExecutionQuality(asset?: string): Promise<ExecutionQualityResponse | null> {
    const query = asset ? `?asset=${encodeURIComponent(asset)}` : '';
    return this.fetchWithAuth<ExecutionQualityResponse>(`/api/ledger/execution-quality${query}`, true);
  }

  public static async getProvenance(groupId: string): Promise<ProvenanceAuditResponse | null> {
    return this.fetchWithAuth<ProvenanceAuditResponse>(`/api/provenance/${encodeURIComponent(groupId)}`, true);
  }

  public static async getLifecycleTrace(intentId: string): Promise<any | null> {
    return this.fetchWithAuth<any>(`/api/ledger/lifecycle/${encodeURIComponent(intentId)}`, true);
  }
}
