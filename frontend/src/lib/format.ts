/** Presentation helpers. Formatting only — no derived analytics.
 *
 * Colours and orderings live here; every word shown to the user comes from the
 * translation files (see `src/i18n`), and money and dates from `useI18n().fmt`.
 */

import type { ReviewStatus, Severity, Tier } from "../api/types";

/** Indian digit grouping with Latin digits, in every language. */
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

export const RISK_META: Record<RiskLevel, { color: string; bg: string; border: string }> = {
  high: {
    color: "var(--risk-high)",
    bg: "var(--risk-high-bg)",
    border: "var(--risk-high-border)",
  },
  medium: {
    color: "var(--risk-medium)",
    bg: "var(--risk-medium-bg)",
    border: "var(--risk-medium-border)",
  },
  low: {
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

export const STATUS_META: Record<ReviewStatus, { color: string; bg: string }> = {
  pending: { color: "var(--status-new)", bg: "var(--surface-3)" },
  under_review: { color: "var(--status-review)", bg: "var(--info-bg)" },
  confirmed: { color: "var(--status-escalated)", bg: "var(--risk-high-bg)" },
  false_positive: { color: "var(--status-cleared)", bg: "var(--success-bg)" },
};

export const REVIEW_STATUSES = Object.keys(STATUS_META) as ReviewStatus[];

export const TIER_ORDER: Tier[] = ["ministry", "state", "district", "mp"];

/** The analysis modules, in display order, with their chart colours. */
export const AGENT_COLOR: Record<string, string> = {
  compliance: "var(--chart-1)",
  anomaly: "var(--chart-2)",
  entity_resolution: "var(--chart-3)",
  network: "var(--chart-5)",
  revisions: "var(--chart-4)",
  payments: "var(--chart-6)",
};

export type AgentKey =
  | "compliance"
  | "anomaly"
  | "entity_resolution"
  | "network"
  | "revisions"
  | "payments";

export function isAgentKey(agent: string): agent is AgentKey {
  return agent in AGENT_COLOR;
}

export type Stage = "immediate" | "next" | "if_unresolved" | "escalation";

export const STAGE_META: Record<Stage, { color: string; order: number }> = {
  immediate: { color: "var(--risk-high)", order: 0 },
  next: { color: "var(--navy-600)", order: 1 },
  if_unresolved: { color: "var(--risk-medium)", order: 2 },
  escalation: { color: "#7d3c98", order: 3 },
};

export function isStage(stage: string): stage is Stage {
  return stage in STAGE_META;
}

export function titleCase(s: string | null | undefined): string {
  if (!s) return "—";
  return s
    .toLowerCase()
    .split(/\s+/)
    .map((w) => (w.length > 2 ? w[0].toUpperCase() + w.slice(1) : w))
    .join(" ");
}
