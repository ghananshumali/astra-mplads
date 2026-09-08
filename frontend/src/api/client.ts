/** Single API service layer. Every backend call in the app goes through here.
 *  No business logic lives on the client: risk scores, action permissions and
 *  synthesis all arrive already computed by the FastAPI backend. */

import type {
  AllowedActions,
  CaseDetail,
  CaseFilters,
  CaseListResponse,
  DataSource,
  DetectionRow,
  DistrictRow,
  Facets,
  LlmStatus,
  PipelineMeta,
  StateRow,
  Stats,
  Synthesis,
  Tier,
  WorkRecord,
} from "./types";

export const API_BASE =
  import.meta.env.VITE_API_BASE?.replace(/\/$/, "") || "http://localhost:8000";

export class ApiError extends Error {
  status: number;
  url: string;
  constructor(message: string, status: number, url: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.url = url;
  }
}

function qs(params: Record<string, unknown>): string {
  const sp = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      // FastAPI Query(list) expects the key repeated
      value.forEach((v) => v !== undefined && sp.append(key, String(v)));
    } else {
      sp.append(key, String(value));
    }
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const url = `${API_BASE}${path}`;
  let res: Response;
  try {
    res = await fetch(url, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch {
    // fetch() reports a blocked CORS preflight and a refused connection
    // identically, so name both possibilities rather than guessing.
    throw new ApiError(
      `Could not reach the ASTRA API at ${API_BASE}. Check that the backend ` +
        "is running, and that this page's origin is allowed by the API's " +
        "CORS policy (see ASTRA_CORS_ORIGINS).",
      0,
      url,
    );
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body?.detail ?? detail;
    } catch {
      /* keep statusText */
    }
    throw new ApiError(String(detail), res.status, url);
  }
  return (await res.json()) as T;
}

export const api = {
  health: () => request<{ platform: string; version: string }>("/"),

  cases: (f: CaseFilters = {}) =>
    request<CaseListResponse>(`/cases${qs({ ...f })}`),

  caseDetail: (flagId: string, tier: Tier) =>
    request<CaseDetail>(
      `/flags/case/${encodeURIComponent(flagId)}${qs({ tier })}`,
    ),

  synthesis: (flagId: string, tier: Tier, useLlm = true) =>
    request<Synthesis>(
      `/flags/case/${encodeURIComponent(flagId)}/synthesis${qs({
        tier,
        use_llm: useLlm,
      })}`,
    ),

  allowedActions: (flagId: string, tier: Tier) =>
    request<AllowedActions>(
      `/flags/case/${encodeURIComponent(flagId)}/actions${qs({ tier })}`,
    ),

  feedback: (
    flagId: string,
    action: "confirmed" | "false_positive" | "under_review",
    tier: Tier,
    note = "",
  ) =>
    request<{ ok: boolean }>(`/flags/${encodeURIComponent(flagId)}/feedback`, {
      method: "POST",
      body: JSON.stringify({ action, authority_tier: tier, note }),
    }),

  stats: (f: Pick<CaseFilters, "states" | "districts" | "constituencies" | "min_score"> = {}) =>
    request<Stats>(`/stats${qs({ ...f })}`),

  facets: () => request<Facets>("/meta/facets"),

  states: () => request<StateRow[]>("/analytics/states"),

  districts: (state?: string, limit = 25) =>
    request<DistrictRow[]>(`/analytics/districts${qs({ state, limit })}`),

  detections: (
    f: Pick<CaseFilters, "states" | "districts" | "constituencies"> = {},
  ) => request<DetectionRow[]>(`/analytics/detections${qs({ ...f })}`),

  work: (workId: string) =>
    request<WorkRecord>(`/works/${encodeURIComponent(workId)}`),

  agencyWorks: (name: string, limit = 40) =>
    request<Partial<WorkRecord>[]>(`/agencies/works${qs({ name, limit })}`),

  dataSource: () => request<DataSource>("/meta/data-source"),

  pipeline: () => request<PipelineMeta>("/meta/pipeline"),

  llm: () => request<LlmStatus>("/meta/llm"),
};
