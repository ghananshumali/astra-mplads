import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowUpCircle,
  Building2,
  CheckCircle2,
  Copy,
  Eye,
  Info,
  Scale,
  Sparkles,
  X,
} from "lucide-react";
import { useState } from "react";

import { api } from "../../api/client";
import type { Finding, PlanAction } from "../../api/types";
import {
  AGENT_META,
  RISK_META,
  SEVERITY_COLOR,
  STAGE_META,
  STATUS_META,
  TIERS,
  formatDate,
  riskLevel,
  rupees,
  titleCase,
} from "../../lib/format";
import { useAuthority } from "../../state/AuthorityContext";
import {
  Banner,
  Card,
  Chip,
  Collapse,
  Empty,
  ErrorState,
  RiskBadge,
  Skeleton,
  Spinner,
  StatusChip,
  Tabs,
} from "../ui";
import "./case.css";

/* ------------------------------------------------------- agent contribution */
function AgentTrace({
  signals,
  score,
}: {
  signals: {
    agent: string;
    agent_label: string;
    headline: string;
    metric: string | null;
    benchmark: string | null;
    contribution: number;
    share_pct: number;
    severity: string;
  }[];
  score: number;
}) {
  const meta = RISK_META[riskLevel(score)];
  return (
    <div className="stack gap-3">
      <p className="text-sm muted" style={{ margin: 0 }}>
        Each agent works independently. The orchestrator combines their findings
        into one score and one recommendation.
      </p>
      {signals.map((s, i) => (
        <div
          key={`${s.agent}-${i}`}
          className="trace-card"
          style={{ borderLeftColor: AGENT_META[s.agent]?.color ?? "var(--navy-500)" }}
        >
          <div className="row gap-2" style={{ justifyContent: "space-between" }}>
            <span className="semibold">{s.agent_label}</span>
            <Chip
              color={SEVERITY_COLOR[s.severity as keyof typeof SEVERITY_COLOR]}
              bg="var(--surface-3)"
            >
              +{s.contribution} · {s.share_pct}%
            </Chip>
          </div>
          <div className="text-xs dim">{AGENT_META[s.agent]?.role}</div>
          <div className="trace-headline">{s.headline}</div>
          <div className="trace-metrics">
            <span>
              <b className="dim">Measured</b> {s.metric ?? "—"}
            </span>
            <span>
              <b className="dim">Compared with</b> {s.benchmark ?? "—"}
            </span>
          </div>
        </div>
      ))}
      <div className="trace-arrow">↓</div>
      <div className="trace-synth">
        <b>Synthesiser / Orchestrator</b>
        <div className="text-sm">
          Combines every agent signal into one composite score.
        </div>
      </div>
      <div className="trace-arrow">↓</div>
      <div
        className="trace-final"
        style={{ background: meta.color }}
      >
        {meta.label} · {score.toFixed(0)}/100 — human review required
      </div>
    </div>
  );
}

/* --------------------------------------------------------------- evidence */
function EvidenceList({ findings }: { findings: Finding[] }) {
  if (!findings.length) return <Empty title="No findings recorded" />;
  return (
    <div className="stack gap-2">
      {findings.map((f, i) => (
        <Collapse
          key={`${f.rule_id}-${i}`}
          title={
            <span className="row gap-2">
              <span
                className="sev-dot"
                style={{ background: SEVERITY_COLOR[f.severity] }}
              />
              <span className="semibold">{f.rule_title}</span>
            </span>
          }
          meta={
            <Chip bg="var(--surface-3)">
              {AGENT_META[f.agent]?.short ?? f.agent}
            </Chip>
          }
        >
          <div className="stack gap-3">
            <p style={{ margin: 0 }}>{f.summary}</p>
            {f.clause && (
              <div className="clause">
                <Scale size={13} />
                <span>{f.clause}</span>
              </div>
            )}
            <div>
              <div className="field-label">Raw values (audit trail)</div>
              <pre className="json">
                {JSON.stringify(f.details, null, 2)}
              </pre>
            </div>
          </div>
        </Collapse>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------ action plan */
function ActionPlan({ plan }: { plan: PlanAction[] }) {
  if (!plan.length)
    return <Empty title="No actions available to this authority" />;
  const stages = [...new Set(plan.map((p) => p.stage))].sort(
    (a, b) => STAGE_META[a].order - STAGE_META[b].order,
  );
  let n = 0;
  return (
    <div className="stack gap-4">
      {stages.map((stage) => (
        <div key={stage}>
          <div
            className="stage-label"
            style={{ color: STAGE_META[stage].color }}
          >
            {STAGE_META[stage].label}
          </div>
          <div className="stack gap-2">
            {plan
              .filter((p) => p.stage === stage)
              .map((a) => {
                n += 1;
                return (
                  <div
                    key={a.action_id}
                    className="action-card"
                    style={{ borderLeftColor: STAGE_META[stage].color }}
                  >
                    <div className="action-title">
                      <span className="action-n">{n}</span>
                      {a.label}
                    </div>
                    <div className="text-sm muted">{a.detail}</div>
                    {a.reason && (
                      <div className="action-why">Why: {a.reason}</div>
                    )}
                  </div>
                );
              })}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ main */
export default function CaseDetail({
  flagId,
  onClose,
}: {
  flagId: string;
  onClose: () => void;
}) {
  const { tier } = useAuthority();
  const qc = useQueryClient();
  const [tab, setTab] = useState("why");
  const [note, setNote] = useState("");

  const detail = useQuery({
    queryKey: ["case", flagId, tier],
    queryFn: () => api.caseDetail(flagId, tier),
  });
  const synth = useQuery({
    queryKey: ["synthesis", flagId, tier],
    queryFn: () => api.synthesis(flagId, tier),
    enabled: tab === "ai",
  });
  const allowed = useQuery({
    queryKey: ["actions", flagId, tier],
    queryFn: () => api.allowedActions(flagId, tier),
    enabled: tab === "ai",
  });
  const work = useQuery({
    queryKey: ["work", detail.data?.entity_id],
    queryFn: () => api.work(detail.data!.entity_id),
    enabled: !!detail.data && detail.data.entity_type === "work",
    retry: false,
  });

  const review = useMutation({
    mutationFn: (action: "confirmed" | "false_positive" | "under_review") =>
      api.feedback(flagId, action, tier, note),
    onSuccess: () => {
      setNote("");
      qc.invalidateQueries({ queryKey: ["case", flagId] });
      qc.invalidateQueries({ queryKey: ["cases"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
    },
  });

  if (detail.isLoading)
    return (
      <Card>
        <div className="stack gap-3">
          <Skeleton h={22} w="65%" />
          <Skeleton h={14} w="40%" />
          <Skeleton h={140} />
        </div>
      </Card>
    );
  if (detail.isError)
    return (
      <Card>
        <ErrorState error={detail.error} onRetry={() => detail.refetch()} />
      </Card>
    );

  const c = detail.data!;
  const brief = c.brief;
  const meta = RISK_META[riskLevel(c.risk_score)];
  const dupFindings = c.findings.filter((f) => f.rule_id.startsWith("D-"));
  const netFindings = c.findings.filter((f) => f.agent === "network");

  const tabs = [
    { value: "why", label: "Why flagged" },
    { value: "ai", label: "AI synthesis" },
    { value: "trace", label: "Agent trace" },
    { value: "evidence", label: "Evidence", count: c.findings.length },
    { value: "record", label: "Work record" },
    { value: "duplicates", label: "Duplicates", count: dupFindings.length },
  ];

  return (
    <div className="case-detail stack gap-4 fade-in">
      {/* ------------------------------------------------------- header */}
      <Card tight>
        <div className="case-head">
          <div className="grow">
            <div className="row gap-2" style={{ marginBottom: 4 }}>
              <RiskBadge score={c.risk_score} showBar />
              <StatusChip status={c.review_status} />
            </div>
            <h2 className="case-title">{c.display_title}</h2>
            <div className="case-sub">
              <code className="mono">{c.entity_id}</code>
              <span className="dot">·</span>
              {[c.district, c.state].filter(Boolean).join(", ") || "—"}
            </div>
          </div>
          <button className="btn btn-ghost btn-sm" onClick={onClose} title="Close">
            <X size={16} />
          </button>
        </div>
        <div className="viewing-as">
          <Eye size={12} />
          Viewing as <b>{TIERS[tier].label}</b> — {brief?.lens ?? TIERS[tier].lens}
        </div>
      </Card>

      <Tabs value={tab} onChange={setTab} items={tabs} />

      {/* --------------------------------------------------------- why */}
      {tab === "why" && brief && (
        <div className="stack gap-4">
          <div
            className="primary-risk"
            style={{ background: meta.bg, borderColor: meta.border }}
          >
            <div className="primary-label" style={{ color: meta.color }}>
              Primary risk
            </div>
            <div className="primary-headline">{brief.primary_risk}</div>
            <p className="primary-plain">{brief.primary_plain}</p>
          </div>

          {brief.signals.length > 1 && (
            <div className="stack gap-2">
              <div className="section-label">Supporting signals</div>
              {brief.signals.slice(1).map((s, i) => (
                <div
                  key={i}
                  className="signal-card"
                  style={{ borderLeftColor: SEVERITY_COLOR[s.severity] }}
                >
                  <div className="row gap-2">
                    <span className="semibold grow">{s.headline}</span>
                    <Chip bg="var(--surface-3)">
                      {AGENT_META[s.agent]?.short}
                    </Chip>
                  </div>
                  <p className="text-sm muted" style={{ margin: "4px 0 0" }}>
                    {s.plain}
                  </p>
                </div>
              ))}
            </div>
          )}

          {brief.context_note && (
            <Banner tone="info">{brief.context_note}</Banner>
          )}

          <Card title="Recommended next steps" subtitle={brief.tier_label} tight>
            <ol className="steps">
              {brief.actions.map((a, i) => (
                <li key={i}>{a}</li>
              ))}
            </ol>
            <p className="text-sm dim" style={{ margin: "8px 0 0" }}>
              {brief.closing}
            </p>
          </Card>

          <ReviewPanel
            status={c.review_status}
            note={note}
            setNote={setNote}
            onAction={(a) => review.mutate(a)}
            pending={review.isPending}
            error={review.error}
          />
        </div>
      )}

      {/* ---------------------------------------------------------- AI */}
      {tab === "ai" && (
        <div className="stack gap-4">
          {synth.isLoading ? (
            <Card>
              <div className="row gap-2 muted">
                <Spinner /> Synthesising for {TIERS[tier].label}…
              </div>
            </Card>
          ) : synth.isError ? (
            <ErrorState error={synth.error} onRetry={() => synth.refetch()} />
          ) : synth.data ? (
            <>
              <div className="row gap-2" style={{ flexWrap: "wrap" }}>
                {synth.data.source === "groq" ? (
                  <Chip color="#0f5233" bg="var(--success-bg)" dot>
                    <Sparkles size={11} /> AI synthesis · {synth.data.model}
                  </Chip>
                ) : (
                  <Chip color="var(--text-2)" bg="var(--surface-3)" dot>
                    Deterministic synthesis
                  </Chip>
                )}
                {synth.data.latency_ms ? (
                  <span className="text-xs dim">{synth.data.latency_ms} ms</span>
                ) : null}
              </div>

              {synth.data.source === "deterministic" && (
                <Banner tone="neutral">
                  {fallbackMessage(synth.data.fallback_reason)} The analysis
                  below is complete — only the wording differs.
                </Banner>
              )}

              <div
                className="primary-risk"
                style={{ background: meta.bg, borderColor: meta.border }}
              >
                <div className="primary-label" style={{ color: meta.color }}>
                  Key risk · {synth.data.risk_level} ·{" "}
                  {synth.data.risk_score.toFixed(0)}/100
                </div>
                <div className="primary-headline">
                  {synth.data.key_risk_summary}
                </div>
                <p className="primary-plain">{synth.data.case_explanation}</p>
              </div>

              {synth.data.authority_specific_summary && (
                <Card
                  title={`What this means for ${synth.data.tier_label}`}
                  tight
                >
                  <p style={{ margin: 0 }}>
                    {synth.data.authority_specific_summary}
                  </p>
                </Card>
              )}

              <Card
                title="Recommended action plan"
                subtitle={`Constrained to actions permitted for ${synth.data.tier_label}`}
                tight
              >
                <ActionPlan plan={synth.data.action_plan} />
                {synth.data.plan_rationale && (
                  <>
                    <div className="section-label" style={{ marginTop: 16 }}>
                      Why these actions
                    </div>
                    <p className="text-sm muted" style={{ margin: 0 }}>
                      {synth.data.plan_rationale}
                    </p>
                  </>
                )}
              </Card>

              {synth.data.limitations_or_missing_evidence.length > 0 && (
                <Collapse title="Limitations and missing evidence">
                  <ul className="bullets">
                    {synth.data.limitations_or_missing_evidence.map((l, i) => (
                      <li key={i}>{l}</li>
                    ))}
                  </ul>
                </Collapse>
              )}

              {allowed.data && (
                <Collapse
                  title="Permitted actions for this authority"
                  meta={
                    <Chip bg="var(--surface-3)">
                      {allowed.data.allowed_actions.length}
                    </Chip>
                  }
                >
                  <p className="text-sm dim" style={{ marginTop: 0 }}>
                    Computed deterministically from role, risk level and the
                    evidence found — with no model involvement. The AI may only
                    select and sequence from this list.
                  </p>
                  <ul className="bullets">
                    {allowed.data.allowed_actions.map((a) => (
                      <li key={a.action_id}>
                        <b>{a.label}</b>{" "}
                        <span className="dim">
                          — {STAGE_META[a.stage]?.label}
                        </span>
                      </li>
                    ))}
                  </ul>
                </Collapse>
              )}

              {synth.data.rejected_actions.length > 0 && (
                <Banner tone="warn">
                  {synth.data.rejected_actions.length} suggested action(s) fell
                  outside this authority's permissions and were removed by the
                  constraint engine.
                </Banner>
              )}
              {synth.data.unverified_numbers.length > 0 && (
                <Banner tone="warn">
                  Figures that could not be matched to the underlying evidence:{" "}
                  <b>{synth.data.unverified_numbers.join(", ")}</b>. Treat these
                  as unverified.
                </Banner>
              )}

              <Banner tone="neutral" icon={<Info size={15} />}>
                {synth.data.constraint_notice}
              </Banner>

              <ReviewPanel
                status={c.review_status}
                note={note}
                setNote={setNote}
                onAction={(a) => review.mutate(a)}
                pending={review.isPending}
                error={review.error}
              />
            </>
          ) : null}
        </div>
      )}

      {/* ------------------------------------------------------- trace */}
      {tab === "trace" && brief && (
        <Card tight>
          <AgentTrace signals={brief.signals} score={c.risk_score} />
        </Card>
      )}

      {/* ---------------------------------------------------- evidence */}
      {tab === "evidence" && (
        <Card tight>
          <EvidenceList findings={c.findings} />
        </Card>
      )}

      {/* ------------------------------------------------------ record */}
      {tab === "record" && (
        <Card tight>
          {c.entity_type !== "work" ? (
            <Banner tone="neutral">
              This case is a <b>{c.entity_type}-level</b> finding rather than a
              single work record.
            </Banner>
          ) : work.isLoading ? (
            <Skeleton h={160} />
          ) : work.isError || !work.data ? (
            <Empty title="Work record unavailable" />
          ) : (
            <div className="stack gap-4">
              <div>
                <div className="field-label">Full description</div>
                <p style={{ margin: 0 }}>{work.data.description ?? "—"}</p>
              </div>
              <div className="record-grid">
                <Field label="Work code" value={work.data.work_id} mono />
                <Field label="Work type" value={work.data.category} />
                <Field
                  label="Location"
                  value={[work.data.district, work.data.state]
                    .filter(Boolean)
                    .join(", ")}
                />
                <Field label="Constituency" value={work.data.constituency} />
                <Field label="MP" value={titleCase(work.data.mp_name)} />
                <Field
                  label="Implementing agency"
                  value={titleCase(work.data.ia_name)}
                />
                <Field
                  label="Vendor paid"
                  value={
                    work.data.vendor_name
                      ? titleCase(work.data.vendor_name)
                      : "No payment recorded"
                  }
                />
                <Field label="Workflow stage" value={work.data.status} />
                <Field
                  label="Sanctioned"
                  value={
                    work.data.sanctioned_amount
                      ? rupees(work.data.sanctioned_amount)
                      : "Not yet sanctioned"
                  }
                />
                <Field
                  label="Recommended cost"
                  value={rupees(work.data.estimated_cost)}
                />
                <Field
                  label="Paid to date"
                  value={
                    work.data.total_paid
                      ? rupees(work.data.total_paid)
                      : "None recorded"
                  }
                />
                <Field
                  label="Sanctioned on"
                  value={formatDate(work.data.sanction_date)}
                />
                <Field
                  label="Completed on"
                  value={
                    work.data.completion_date
                      ? formatDate(work.data.completion_date)
                      : "Not recorded"
                  }
                />
                <Field label="Data era" value={work.data.era} mono />
              </div>
            </div>
          )}
        </Card>
      )}

      {/* -------------------------------------------------- duplicates */}
      {tab === "duplicates" && (
        <Card tight>
          {dupFindings.length === 0 ? (
            <Empty
              icon={<Copy size={20} />}
              title="No duplicate-risk evidence"
              hint="The entity-resolution agent did not find a matching record for this case."
            />
          ) : (
            <div className="stack gap-4">
              <p className="text-sm muted" style={{ margin: 0 }}>
                Approval checks review one work at a time, so the same work
                entered twice is not caught by the normal workflow. These are
                candidates for verification, not confirmed duplicates.
              </p>
              {dupFindings.map((f, i) => {
                const d = f.details as Record<string, unknown>;
                if (f.rule_id === "D-DUP-02") {
                  return (
                    <div key={i} className="dup-card">
                      <div className="semibold">
                        {String(d.cluster_size)} works share one low-detail
                        description in {String(d.district)}
                      </div>
                      <code className="json">
                        {String(d.normalised_description ?? "")}
                      </code>
                      <div className="text-sm muted">
                        Total {rupees(Number(d.total_cost))}
                      </div>
                    </div>
                  );
                }
                return (
                  <div key={i} className="dup-card">
                    <div className="dup-compare">
                      <div>
                        <div className="field-label">This work</div>
                        <code className="mono text-xs">{c.entity_id}</code>
                        <p className="dup-text">
                          {String(d.this_description ?? "—")}
                        </p>
                      </div>
                      <div>
                        <div className="field-label">Matched work</div>
                        <code className="mono text-xs">
                          {String(d.pair_work_id ?? "—")}
                        </code>
                        <p className="dup-text">
                          {String(d.other_description ?? "—")}
                        </p>
                      </div>
                    </div>
                    <div className="dup-stats">
                      <Stat
                        label="Description match"
                        value={`${Math.round(Number(d.semantic_sim ?? 0) * 100)}%`}
                      />
                      <Stat
                        label="Same amount"
                        value={d.same_sanction_amount ? "Yes" : "No"}
                      />
                      <Stat
                        label="Evidence"
                        value={titleCase(String(d.evidence_strength ?? "—"))}
                      />
                    </div>
                    <div className="text-sm muted">
                      <b>Pattern:</b> {String(d.duplication_mode ?? "—")}
                    </div>
                    {d.geo_km === null && (
                      <div className="text-xs dim">
                        Proximity is assessed at district level — this eSAKSHI
                        export carries no asset coordinates.
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </Card>
      )}

      {netFindings.length > 0 && tab === "record" && (
        <Card title="Agency network signal" tight>
          {netFindings.map((f, i) => (
            <p key={i} className="text-sm" style={{ margin: 0 }}>
              <Building2 size={13} /> {f.summary}
            </p>
          ))}
        </Card>
      )}

      <div className="review-notice">
        <AlertTriangle size={13} />
        {c.disclaimer}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- helpers */
function Field({
  label,
  value,
  mono,
}: {
  label: string;
  value: string | null | undefined;
  mono?: boolean;
}) {
  return (
    <div>
      <div className="field-label">{label}</div>
      <div className={mono ? "mono text-sm" : "text-sm"}>{value || "—"}</div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat">
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

function ReviewPanel({
  status,
  note,
  setNote,
  onAction,
  pending,
  error,
}: {
  status: keyof typeof STATUS_META;
  note: string;
  setNote: (v: string) => void;
  onAction: (a: "confirmed" | "false_positive" | "under_review") => void;
  pending: boolean;
  error: unknown;
}) {
  const flow: (keyof typeof STATUS_META)[] = [
    "pending",
    "under_review",
    "confirmed",
    "false_positive",
  ];
  return (
    <Card title="Human review decision" tight>
      <div className="flow">
        {flow.map((s, i) => (
          <span key={s} className={`flow-step${s === status ? " on" : ""}`}>
            {STATUS_META[s].label}
            {i < flow.length - 1 && <span className="flow-arrow">→</span>}
          </span>
        ))}
      </div>
      <input
        className="input"
        placeholder="Reviewer note (optional) — record what you verified…"
        value={note}
        onChange={(e) => setNote(e.target.value)}
        style={{ marginBottom: 10 }}
      />
      <div className="row gap-2" style={{ flexWrap: "wrap" }}>
        <button
          className="btn btn-sm"
          disabled={pending}
          onClick={() => onAction("under_review")}
        >
          <Eye size={13} /> Mark under review
        </button>
        <button
          className="btn btn-sm btn-danger"
          disabled={pending}
          onClick={() => onAction("confirmed")}
        >
          <ArrowUpCircle size={13} /> Escalate
        </button>
        <button
          className="btn btn-sm btn-success"
          disabled={pending}
          onClick={() => onAction("false_positive")}
        >
          <CheckCircle2 size={13} /> Close as false positive
        </button>
        {pending && <Spinner size={14} />}
      </div>
      {error ? (
        <div className="text-sm" style={{ color: "var(--danger)", marginTop: 8 }}>
          Could not record the decision. {(error as Error).message}
        </div>
      ) : null}
      <p className="text-xs dim" style={{ margin: "10px 0 0" }}>
        The authority decides the outcome — ASTRA only prioritises what to look
        at. False-positive decisions feed threshold recalibration.
      </p>
    </Card>
  );
}

function fallbackMessage(reason: string | null): string {
  switch (reason) {
    case "no_api_key":
      return "No AI provider key is configured, so the deterministic layer produced this.";
    case "llm_disabled":
      return "AI synthesis is switched off.";
    case "timeout":
      return "The AI service did not respond in time.";
    case "rate_limited":
      return "The AI service rate limit was reached.";
    case "unauthorized":
      return "The AI service rejected the configured key.";
    case "language_guardrail":
      return "The AI response failed a safety check and was discarded.";
    default:
      return "The AI service was unavailable.";
  }
}
