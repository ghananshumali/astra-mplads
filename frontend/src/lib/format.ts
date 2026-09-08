/** Presentation helpers. Formatting only — no derived analytics. */

import type { ReviewStatus, Severity, Tier } from "../api/types";

/** Indian money convention: lakh / crore, matching the backend's own output. */
export function rupees(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const v = Number(value);
  if (Math.abs(v) >= 1e7) return `₹${(v / 1e7).toFixed(2)} crore`;
  if (Math.abs(v) >= 1e5) return `₹${(v / 1e5).toFixed(2)} lakh`;
  return `₹${v.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

export function compact(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n.toLocaleString("en-IN");
}

export type RiskLevel = "high" | "medium" | "low";

/** Single source of truth for risk banding — mirrors astra/explain.py. */
export function riskLevel(score: number): RiskLevel {
  if (score >= 70) return "high";
  if (score >= 40) return "medium";
  return "low";
}

export const RISK_META: Record<
  RiskLevel,
  { label: string; color: string; bg: string; border: string }
> = {
  high: {
    label: "High risk",
    color: "var(--risk-high)",
    bg: "var(--risk-high-bg)",
    border: "var(--risk-high-border)",
  },
  medium: {
    label: "Medium risk",
    color: "var(--risk-medium)",
    bg: "var(--risk-medium-bg)",
    border: "var(--risk-medium-border)",
  },
  low: {
    label: "Low risk",
    color: "var(--risk-low)",
    bg: "var(--risk-low-bg)",
    border: "var(--risk-low-border)",
  },
};

export const SEVERITY_COLOR: Record<Severity, string> = {
  critical: "var(--sev-critical)",
  high: "var(--sev-high)",
  medium: "var(--sev-medium)",
  low: "var(--sev-low)",
};

export const STATUS_META: Record<
  ReviewStatus,
  { label: string; short: string; color: string; bg: string }
> = {
  pending: {
    label: "Awaiting review",
    short: "New",
    color: "var(--status-new)",
    bg: "var(--surface-3)",
  },
  under_review: {
    label: "Under review",
    short: "Reviewing",
    color: "var(--status-review)",
    bg: "var(--info-bg)",
  },
  confirmed: {
    label: "Escalated",
    short: "Escalated",
    color: "var(--status-escalated)",
    bg: "var(--risk-high-bg)",
  },
  false_positive: {
    label: "Closed — false positive",
    short: "Cleared",
    color: "var(--status-cleared)",
    bg: "var(--success-bg)",
  },
};

export const TIERS: Record<
  Tier,
  { label: string; short: string; lens: string; scope: string }
> = {
  ministry: {
    label: "Ministry (MoSPI)",
    short: "Ministry",
    lens: "National trends and policy signals",
    scope: "All states",
  },
  state: {
    label: "State Nodal Authority",
    short: "State Nodal",
    lens: "Patterns repeating across districts",
    scope: "One state",
  },
  district: {
    label: "District Authority",
    short: "District",
    lens: "Ground-level execution and verification",
    scope: "One district",
  },
  mp: {
    label: "Member of Parliament",
    short: "MP",
    lens: "Works recommended in your constituency",
    scope: "One constituency",
  },
};

export const AGENT_META: Record<
  string,
  { label: string; short: string; role: string; color: string }
> = {
  compliance: {
    label: "Compliance Agent",
    short: "Compliance",
    role: "Checks each work against MPLADS scheme rules",
    color: "var(--chart-1)",
  },
  anomaly: {
    label: "Statistical Anomaly Agent",
    short: "Anomaly",
    role: "Compares cost against similar works in the same state",
    color: "var(--chart-2)",
  },
  entity_resolution: {
    label: "Entity Resolution Agent",
    short: "Duplicates",
    role: "Looks for the same work recorded more than once",
    color: "var(--chart-3)",
  },
  network: {
    label: "Network Analysis Agent",
    short: "Network",
    role: "Looks at contractor and agency patterns across districts",
    color: "var(--chart-5)",
  },
};

export const STAGE_META: Record<
  string,
  { label: string; color: string; order: number }
> = {
  immediate: { label: "Immediate", color: "var(--risk-high)", order: 0 },
  next: { label: "Next steps", color: "var(--navy-600)", order: 1 },
  if_unresolved: {
    label: "If concerns persist",
    color: "var(--risk-medium)",
    order: 2,
  },
  escalation: { label: "Escalation", color: "#7d3c98", order: 3 },
};

export function titleCase(s: string | null | undefined): string {
  if (!s) return "—";
  return s
    .toLowerCase()
    .split(/\s+/)
    .map((w) => (w.length > 2 ? w[0].toUpperCase() + w.slice(1) : w))
    .join(" ");
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleDateString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const mins = Math.round((Date.now() - d.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} h ago`;
  return `${Math.round(hrs / 24)} d ago`;
}
