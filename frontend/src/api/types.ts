/** Types mirroring the ASTRA FastAPI contract.
 *  Kept in sync by hand with astra/api/main.py — the backend is the source of
 *  truth and this client never recomputes anything it returns. */

export type Tier = "ministry" | "state" | "district" | "mp";
export type ReviewStatus =
  | "pending"
  | "under_review"
  | "confirmed"
  | "false_positive";
export type Severity = "critical" | "high" | "medium" | "low";
export type AgentName =
  | "compliance"
  | "anomaly"
  | "entity_resolution"
  | "network";

export interface CaseSummary {
  flag_id: string;
  display_title: string;
  entity_id: string;
  entity_type: "work" | "constituency" | "agency";
  state: string | null;
  district: string | null;
  constituency: string | null;
  era: string;
  risk_score: number;
  alert: boolean;
  review_status: ReviewStatus;
  primary_signal: string | null;
  agents: AgentName[];
  rule_ids: string[];
  finding_count: number;
}

export interface CaseListResponse {
  total: number;
  limit: number;
  offset: number;
  disclaimer: string;
  cases: CaseSummary[];
}

export interface Finding {
  agent: AgentName;
  rule_id: string;
  rule_title: string;
  clause: string | null;
  severity: Severity;
  entity_type: string;
  entity_id: string;
  /** The agent's own statement, as recorded (English). */
  summary: string;
  details: Record<string, unknown>;
  /** In a language other than English: the finding explained in that language. */
  explained?: {
    headline: string;
    plain: string;
    metric: string | null;
    benchmark: string | null;
    clause: string | null;
  };
}

export interface BriefSignal {
  rule_id: string;
  agent: AgentName;
  agent_label: string;
  severity: Severity;
  severity_label: string;
  contribution: number;
  share_pct: number;
  clause: string | null;
  headline: string;
  plain: string;
  metric: string | null;
  benchmark: string | null;
  technical: string;
  occurrences: number;
  actions: string[];
}

export interface TierBrief {
  tier: Tier;
  tier_label: string;
  lens: string;
  opening: string;
  primary_risk: string;
  primary_plain: string;
  signals: BriefSignal[];
  corroboration: string;
  context_note: string;
  actions: string[];
  closing: string;
  disclaimer: string;
}

export interface CaseDetail {
  flag_id: string;
  display_title: string | null;
  entity_id: string;
  entity_type: string;
  state: string | null;
  district: string | null;
  constituency: string | null;
  era: string;
  risk_score: number;
  alert: boolean;
  review_status: ReviewStatus;
  primary_signal: string | null;
  brief: TierBrief | null;
  all_tier_briefs: Record<Tier, TierBrief>;
  findings: Finding[];
  narrative: string;
  disclaimer: string;
}

export interface PlanAction {
  action_id: string;
  label: string;
  detail: string;
  stage: "immediate" | "next" | "if_unresolved" | "escalation";
  reason: string;
}

export interface Synthesis {
  source: "groq" | "deterministic";
  fallback_reason: string | null;
  model: string | null;
  latency_ms?: number;
  cached?: boolean;
  case_id: string;
  tier: Tier;
  tier_label: string;
  risk_score: number;
  risk_level: string;
  key_risk_summary: string;
  case_explanation: string;
  supporting_signals: {
    source_agent: string;
    signal: string;
    evidence: string;
  }[];
  authority_specific_summary: string;
  action_plan: PlanAction[];
  plan_rationale: string;
  limitations_or_missing_evidence: string[];
  rejected_actions: string[];
  unverified_numbers: string[];
  constraint_notice: string;
  human_review_notice: string;
}

export interface AllowedActions {
  flag_id: string;
  tier: Tier;
  tier_label: string;
  risk_score: number;
  evidence_present: string[];
  allowed_actions: Omit<PlanAction, "reason">[];
  constraint_notice: string;
}

export interface Stats {
  total: number;
  high: number;
  medium: number;
  low: number;
  under_review: number;
  closed: number;
  bands: Record<string, number>;
  corpus: { works: number; districts: number; states: number };
}

export interface Facets {
  states: string[];
  districts: string[];
  constituencies: string[];
  status_counts: Record<string, number>;
  total: number;
  alerts: number;
  high: number;
  rules: { rule_id: string; title: string; count: number }[];
  agents: [string, string][];
}

export interface StateRow {
  state: string;
  flags: number;
  high_risk: number;
  alerts: number;
  avg_risk: number;
}

export interface DistrictRow {
  state: string;
  district: string;
  flags: number;
  high_risk: number;
  alerts: number;
  avg_risk: number;
}

export interface DetectionRow {
  rule_id: string;
  title: string;
  count: number;
}

export interface WorkRecord {
  work_id: string;
  description: string | null;
  category: string | null;
  state: string | null;
  district: string | null;
  constituency: string | null;
  mp_name: string | null;
  ia_name: string | null;
  vendor_name: string | null;
  /** Portal id of `vendor_name`; names are not unique. */
  vendor_id: string | null;
  /** Agency executing the work, from payment records; empty before a first payment. */
  implementing_agency: string | null;
  /** The portal's own class, e.g. "Repair and Renovation". */
  work_category: string | null;
  letter_no: string | null;
  term_start: string | null;
  term_end: string | null;
  estimated_cost: number | null;
  sanctioned_amount: number | null;
  expenditure: number | null;
  total_paid: number | null;
  payment_count: number | null;
  status: string | null;
  recommended_date: string | null;
  sanction_date: string | null;
  completion_date: string | null;
  era: string | null;
}

/** Unattended running, from the supervisor (`GET /meta/ops`). */
export interface OpsStatus {
  supervisor_running: boolean;
  started_at: string | null;
  last_tick_at: string | null;
  stopped_at: string | null;
  keep_awake: { requested: boolean; held: boolean } | null;
  services: Record<
    string,
    {
      running: boolean;
      healthy?: boolean;
      pid: number | null;
      started_at: string | null;
      restarts_last_hour: number;
      last_exit_code: number | null;
      log: string;
    }
  > | null;
  /** Problems being tracked; `notified_at` is set once an alert went out. */
  conditions: {
    key: string;
    title: string;
    detail: string;
    severity: string;
    first_seen: string;
    notified_at: string | null;
  }[];
  last_alert: { title: string; body: string; at: string } | null;
  backup: {
    last_ok_at?: string | null;
    last_error?: string | null;
    last_error_at?: string | null;
    last_bytes?: number;
    kept: number;
    newest: { name: string; bytes: number; taken_at: string } | null;
  };
}

/** One payment record the portal lists, with what its dates say against the work. */
export interface PaymentRecord {
  work_id: string;
  seq: number;
  paid_on: string | null;
  amount: number | null;
  vendor_id: string | null;
  vendor_name: string | null;
  implementing_agency: string | null;
  /** "Payment Success" or "Payment In-Progress". */
  status: string | null;
  /** Days after the recorded completion, when paid after it. */
  days_after_completion: number | null;
  before_sanction: boolean;
  /** Dated 25 to 31 March. */
  year_end_week: boolean;
  /** Records on the work with the same date, amount and vendor, this one included. */
  repeats: number;
}

export interface WorkPayments {
  work_id: string;
  payments: PaymentRecord[];
  summary: {
    count: number;
    total: number;
    sanctioned_amount: number | null;
    share_of_sanctioned: number | null;
    vendors: number;
    in_progress: number;
    first_paid_on: string | null;
    last_paid_on: string | null;
    after_completion: number;
    repeated: number;
  };
}

export interface DataSource {
  mode_requested?: string;
  mode_resolved?: string;
  /** True when the corpus is the live eSAKSHI portal kept current by the poller. */
  live?: boolean;
  works?: number;
  /** Work records by house: "LS" / "RS". */
  houses?: Record<string, number>;
  fundflows?: number;
  work_eras?: Record<string, number>;
  fundflow_eras?: Record<string, number>;
  freshness_vs_live_portal?: {
    live_recommended_works: number;
    local_recommended_works: number;
    coverage_pct: number;
    delta: number;
    tenure: string;
    checked_at: string;
  } | null;
  ingested_at?: string;
  provenance: {
    source: string;
    mode: string;
    table_name: string;
    rows: number;
    status: string;
    detail: string;
    fetched_at: string | null;
  }[];
}

/** One portal tile figure, portal and stored side by side: [count, rupees].
 *  A null count means the portal does not report one (expenditure). */
export interface ParityFigure {
  portal: [number | null, number | null];
  stored: [number | null, number | null];
  exact: boolean;
}

export type ParityTile = "recommended" | "sanctioned" | "completed" | "expenditure";

/** Slice-by-slice agreement between the stored corpus and the portal's tiles. */
export interface Parity {
  registered_slices: number;
  checked_slices: number;
  exact_slices: number;
  exception_count: number;
  exceptions: {
    shard_id: string;
    house: "LS" | "RS";
    place: string | null;
    differences: { tile: ParityTile; measure: "count" | "rupees"; portal: number; stored: number }[];
    checked_at: string;
    recheck_after: string | null;
  }[];
  duplicate_listings: number;
  national: Partial<Record<"LS" | "RS", Partial<Record<ParityTile, ParityFigure>>>>;
  awaiting_removal: number;
  oldest_missing_since: string | null;
  removed_from_portal: number;
  latest_removal: string | null;
}

/** GET /meta/freshness — health of the live sync with the eSAKSHI portal. */
export interface Freshness {
  status: "ok" | "degraded" | "no data";
  parity?: Parity;
  poller_running: boolean;
  poller_since: string | null;
  last_check_at: string | null;
  poll_interval_seconds: number | null;
  /** Local time of the nightly full re-read, e.g. "03:00". */
  reconcile_at: string | null;
  sweep_in_progress: boolean;
  sweep_started_at: string | null;
  last_update: { at: string; area: string; detail: string } | null;
  /** The rotation that re-reads the area read longest ago. Null until a
   *  poller has reported its settings. */
  rolling: {
    enabled: boolean;
    /** Running now: poller up, and inside its hours. */
    active_now: boolean;
    areas_per_check: number;
    /** Local hours, e.g. "08:00-20:00", or "always". */
    hours: string;
    min_age_hours: number;
    last_at: string | null;
  } | null;
  /** The nightly photo check of held duplicate matches. Null until a poller
   *  has reported its settings. */
  photos?: {
    enabled: boolean;
    /** Running now: poller up, and inside its hours. */
    active_now: boolean;
    /** Local hours, e.g. "21:00-07:00". */
    hours: string;
    per_night: number;
    last_at: string | null;
    last_night: { night: string; asked: number } | null;
    held_works: number;
    checked: number;
    by_status: Record<string, number>;
    /** Checks recorded that the risk flags do not include yet. */
    results_waiting: boolean;
    paused_until: string | null;
  } | null;
  /** When the risk flags were last recomputed from the stored data. */
  analysis: {
    last_at: string | null;
    in_progress: boolean;
    started_at: string | null;
    /** Stored portal changes the current flags do not include yet. */
    changes_waiting: boolean;
    every_minutes: number | null;
  };
  oldest_shard_fetch: string | null;
  registered_shards: number;
  reconciled_shards: number;
  reconciled_pct: number | null;
  quarantined: number;
  count_mismatched: number;
  never_fetched: number;
  last_full_reconciliation: string | null;
  hours_since_reconciliation: number | null;
  reconciliation_overdue: boolean;
  /** The poller's view of the portal, from its circuit breaker. `open` is true
   *  only while the poller runs and the portal is not answering. */
  portal?: {
    open: boolean;
    next_attempt_at: string | null;
    failing_since: string | null;
    last_error: string | null;
    last_error_at: string | null;
    checked_at: string | null;
  } | null;
  stale: {
    shard_id: string;
    /** A national check, a state-level check, or a registered area. */
    scope: "national" | "state" | "area";
    place: string | null;
    state: string | null;
    house: "LS" | "RS";
    consecutive_failures: number;
    stale_since: string | null;
    last_fetch: string | null;
    last_error: string | null;
  }[];
}

/** GET /meta/recent-updates — what the portal changed most recently. */
export interface RecentUpdates {
  stores: { area: string; records: number; detail: string; at: string }[];
  changes: {
    work_id: string;
    observed_at: string;
    shard_id: string | null;
    place: string | null;
    state: string | null;
    house: string | null;
    mp_name: string | null;
    description: string | null;
    status: string | null;
    fields: { field: string; old_value: string | null; new_value: string | null }[];
  }[];
}

export interface PipelineMeta {
  ran_at: string | null;
  flags_produced?: number;
  router_trace: {
    agent: string;
    dispatched: boolean;
    reason: string;
    findings?: number;
  }[];
  rule_coverage: {
    rule_id: string;
    title: string;
    findings: number;
    /** guideline | guideline-screening | heuristic | statistical */
    basis?: string | null;
    evaluated?: boolean;
    /** Why the rule could not be evaluated on this data, when it could not. */
    stood_down?: string;
  }[];
  /** Guideline paragraphs no field in the data lets ASTRA check, with the reason. */
  unchecked_provisions?: Record<string, string>;
}

export interface LlmStatus {
  provider: string;
  model: string;
  configured: boolean;
  key_hint: string | null;
  timeout_seconds: number;
  cache: { entries: number; limit: number };
}

export interface CaseFilters {
  states?: string[];
  districts?: string[];
  constituencies?: string[];
  entity_types?: string[];
  statuses?: string[];
  rule_ids?: string[];
  min_score?: number;
  search?: string;
  order?: string;
  limit?: number;
  offset?: number;
}
