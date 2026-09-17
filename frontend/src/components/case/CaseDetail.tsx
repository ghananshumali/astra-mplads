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
import type { Finding, PlanAction, ReviewStatus } from "../../api/types";
import { useI18n } from "../../i18n/context";
import type { MessageKey } from "../../i18n/context";
import { actionText, agentText, dupMode, ruleTitle, stageLabel } from "../../i18n/labels";
import {
  AGENT_COLOR,
  RISK_META,
  SEVERITY_COLOR,
  STAGE_META,
  isStage,
  riskLevel,
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

const REVIEW_FLOW: ReviewStatus[] = ["pending", "under_review", "confirmed", "false_positive"];

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
  const i18n = useI18n();
  const { t } = i18n;
  const level = riskLevel(score);
  const meta = RISK_META[level];
  return (
    <div className="stack gap-3">
      <p className="text-sm muted" style={{ margin: 0 }}>
        {t("case.trace.intro")}
      </p>
      {signals.map((s, i) => (
        <div
          key={`${s.agent}-${i}`}
          className="trace-card"
          style={{ borderInlineStartColor: AGENT_COLOR[s.agent] ?? "var(--navy-500)" }}
        >
          <div className="row gap-2" style={{ justifyContent: "space-between" }}>
            <span className="semibold">{agentText(i18n, s.agent, "label")}</span>
            <Chip
              color={SEVERITY_COLOR[s.severity as keyof typeof SEVERITY_COLOR]}
              bg="var(--surface-3)"
            >
              +{s.contribution} · {s.share_pct}%
            </Chip>
          </div>
          <div className="text-xs dim">{agentText(i18n, s.agent, "role")}</div>
          <div className="trace-headline">{s.headline}</div>
          <div className="trace-metrics">
            <span>
              <b className="dim">{t("case.trace.measured")}</b> {s.metric ?? "—"}
            </span>
            <span>
              <b className="dim">{t("case.trace.compared")}</b> {s.benchmark ?? "—"}
            </span>
          </div>
        </div>
      ))}
      <div className="trace-arrow">↓</div>
      <div className="trace-synth">
        <b>{t("case.trace.synth")}</b>
        <div className="text-sm">{t("case.trace.synthText")}</div>
      </div>
      <div className="trace-arrow">↓</div>
      <div className="trace-final" style={{ background: meta.color }}>
        {t("case.trace.final", { risk: t(`risk.${level}`), score: score.toFixed(0) })}
      </div>
    </div>
  );
}

/* --------------------------------------------------------------- evidence */
function EvidenceList({ findings }: { findings: Finding[] }) {
  const i18n = useI18n();
  const { t } = i18n;
  if (!findings.length) return <Empty title={t("case.evidence.empty")} />;
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
              <span className="semibold">{ruleTitle(i18n, f.rule_id, f.rule_title)}</span>
            </span>
          }
          meta={<Chip bg="var(--surface-3)">{agentText(i18n, f.agent, "short")}</Chip>}
        >
          <div className="stack gap-3">
            <p style={{ margin: 0 }}>{f.explained?.plain ?? f.summary}</p>
            {(f.explained?.clause ?? f.clause) && (
              <div className="clause">
                <Scale size={13} />
                <span>{f.explained?.clause ?? f.clause}</span>
              </div>
            )}
            <div>
              <div className="field-label">{t("case.evidence.raw")}</div>
              {f.explained && (
                <p className="text-sm muted" style={{ margin: "0 0 6px" }}>
                  <b>{t("case.evidence.original")}</b> <span lang="en">{f.summary}</span>
                </p>
              )}
              <pre className="json">{JSON.stringify(f.details, null, 2)}</pre>
            </div>
          </div>
        </Collapse>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------ action plan */
function ActionPlan({ plan }: { plan: PlanAction[] }) {
  const i18n = useI18n();
  const { t } = i18n;
  if (!plan.length) return <Empty title={t("case.plan.empty")} />;
  const order = (stage: string) => (isStage(stage) ? STAGE_META[stage].order : 9);
  const color = (stage: string) => (isStage(stage) ? STAGE_META[stage].color : "var(--navy-500)");
  const stages = [...new Set(plan.map((p) => p.stage))].sort((a, b) => order(a) - order(b));
  let n = 0;
  return (
    <div className="stack gap-4">
      {stages.map((stage) => (
        <div key={stage}>
          <div className="stage-label" style={{ color: color(stage) }}>
            {stageLabel(i18n, stage)}
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
                    style={{ borderInlineStartColor: color(stage) }}
                  >
                    <div className="action-title">
                      <span className="action-n">{n}</span>
                      {actionText(i18n, a.action_id, "label", a.label)}
                    </div>
                    <div className="text-sm muted">
                      {actionText(i18n, a.action_id, "detail", a.detail)}
                    </div>
                    {a.reason && (
                      <div className="action-why">{t("case.plan.why", { reason: a.reason })}</div>
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
  const i18n = useI18n();
  const { t, tOr, fmt } = i18n;
  const lang = i18n.language.code;
  const { tier } = useAuthority();
  const qc = useQueryClient();
  const [tab, setTab] = useState("why");
  const [note, setNote] = useState("");

  const detail = useQuery({
    queryKey: ["case", flagId, tier, lang],
    queryFn: () => api.caseDetail(flagId, tier, lang),
  });
  const synth = useQuery({
    queryKey: ["synthesis", flagId, tier, lang],
    queryFn: () => api.synthesis(flagId, tier, lang),
    enabled: tab === "ai",
  });
  const allowed = useQuery({
    queryKey: ["actions", flagId, tier, lang],
    queryFn: () => api.allowedActions(flagId, tier, lang),
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
  const level = riskLevel(c.risk_score);
  const meta = RISK_META[level];
  const dupFindings = c.findings.filter((f) => f.rule_id.startsWith("D-"));
  const netFindings = c.findings.filter((f) => f.agent === "network");
  const tierLabel = t(`tier.${tier}.label`);
  const entityLevel: MessageKey =
    c.entity_type === "agency" ? "entity.agency" : c.entity_type === "work" ? "entity.work" : "entity.constituency";

  const tabs = [
    { value: "why", label: t("case.tab.why") },
    { value: "ai", label: t("case.tab.ai") },
    { value: "trace", label: t("case.tab.trace") },
    { value: "evidence", label: t("case.tab.evidence"), count: c.findings.length },
    { value: "record", label: t("case.tab.record") },
    { value: "duplicates", label: t("case.tab.duplicates"), count: dupFindings.length },
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
          <button className="btn btn-ghost btn-sm" onClick={onClose} title={t("case.close")}>
            <X size={16} />
          </button>
        </div>
        <div className="viewing-as">
          <Eye size={12} />
          {t("case.viewingAs", { tier: tierLabel, lens: t(`tier.${tier}.lens`) })}
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
              {t("case.primaryRisk")}
            </div>
            <div className="primary-headline">{brief.primary_risk}</div>
            <p className="primary-plain">{brief.primary_plain}</p>
          </div>

          {brief.signals.length > 1 && (
            <div className="stack gap-2">
              <div className="section-label">{t("case.supporting")}</div>
              {brief.signals.slice(1).map((s, i) => (
                <div
                  key={i}
                  className="signal-card"
                  style={{ borderInlineStartColor: SEVERITY_COLOR[s.severity] }}
                >
                  <div className="row gap-2">
                    <span className="semibold grow">{s.headline}</span>
                    <Chip bg="var(--surface-3)">{agentText(i18n, s.agent, "short")}</Chip>
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

          <Card title={t("case.nextSteps")} subtitle={tierLabel} tight>
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
                <Spinner /> {t("case.ai.loading", { tier: tierLabel })}
              </div>
            </Card>
          ) : synth.isError ? (
            <ErrorState error={synth.error} onRetry={() => synth.refetch()} />
          ) : synth.data ? (
            <>
              <div className="row gap-2" style={{ flexWrap: "wrap" }}>
                {synth.data.source === "groq" ? (
                  <Chip color="#0f5233" bg="var(--success-bg)" dot>
                    <Sparkles size={11} /> {t("case.ai.chip", { model: synth.data.model ?? "" })}
                  </Chip>
                ) : (
                  <Chip color="var(--text-2)" bg="var(--surface-3)" dot>
                    {t("case.ai.deterministic")}
                  </Chip>
                )}
                {synth.data.latency_ms ? (
                  <span className="text-xs dim">{t("case.ai.latency", { ms: synth.data.latency_ms })}</span>
                ) : null}
              </div>

              {synth.data.source === "deterministic" && (
                <Banner tone="neutral">
                  {tOr(`fallback.${synth.data.fallback_reason}`, t("fallback.default"))}{" "}
                  {t("case.ai.fallbackTail")}
                </Banner>
              )}

              <div
                className="primary-risk"
                style={{ background: meta.bg, borderColor: meta.border }}
              >
                <div className="primary-label" style={{ color: meta.color }}>
                  {t("case.ai.keyRisk", {
                    risk: t(`risk.${riskLevel(synth.data.risk_score)}`),
                    score: synth.data.risk_score.toFixed(0),
                  })}
                </div>
                <div className="primary-headline">
                  {synth.data.key_risk_summary}
                </div>
                <p className="primary-plain">{synth.data.case_explanation}</p>
              </div>

              {synth.data.authority_specific_summary && (
                <Card title={t("case.ai.meansFor", { tier: tierLabel })} tight>
                  <p style={{ margin: 0 }}>
                    {synth.data.authority_specific_summary}
                  </p>
                </Card>
              )}

              <Card
                title={t("case.ai.plan")}
                subtitle={t("case.ai.planSub", { tier: tierLabel })}
                tight
              >
                <ActionPlan plan={synth.data.action_plan} />
                {synth.data.plan_rationale && (
                  <>
                    <div className="section-label" style={{ marginTop: 16 }}>
                      {t("case.ai.whyActions")}
                    </div>
                    <p className="text-sm muted" style={{ margin: 0 }}>
                      {synth.data.plan_rationale}
                    </p>
                  </>
                )}
              </Card>

              {synth.data.limitations_or_missing_evidence.length > 0 && (
                <Collapse title={t("case.ai.limitations")}>
                  <ul className="bullets">
                    {synth.data.limitations_or_missing_evidence.map((l, i) => (
                      <li key={i}>{l}</li>
                    ))}
                  </ul>
                </Collapse>
              )}

              {allowed.data && (
                <Collapse
                  title={t("case.ai.permitted")}
                  meta={
                    <Chip bg="var(--surface-3)">
                      {allowed.data.allowed_actions.length}
                    </Chip>
                  }
                >
                  <p className="text-sm dim" style={{ marginTop: 0 }}>
                    {t("case.ai.permittedNote")}
                  </p>
                  <ul className="bullets">
                    {allowed.data.allowed_actions.map((a) => (
                      <li key={a.action_id}>
                        <b>{actionText(i18n, a.action_id, "label", a.label)}</b>{" "}
                        <span className="dim">— {stageLabel(i18n, a.stage)}</span>
                      </li>
                    ))}
                  </ul>
                </Collapse>
              )}

              {synth.data.rejected_actions.length > 0 && (
                <Banner tone="warn">
                  {t("case.ai.rejected", { count: synth.data.rejected_actions.length })}
                </Banner>
              )}
              {synth.data.unverified_numbers.length > 0 && (
                <Banner tone="warn">
                  {t("case.ai.unverified", { figures: synth.data.unverified_numbers.join(", ") })}
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
              {t("case.record.notWork", { level: t(entityLevel) })}
            </Banner>
          ) : work.isLoading ? (
            <Skeleton h={160} />
          ) : work.isError || !work.data ? (
            <Empty title={t("case.record.unavailable")} />
          ) : (
            <div className="stack gap-4">
              <div>
                <div className="field-label">{t("case.record.description")}</div>
                <p style={{ margin: 0 }}>{work.data.description ?? "—"}</p>
              </div>
              <div className="record-grid">
                <Field label={t("record.workCode")} value={work.data.work_id} mono />
                <Field label={t("record.workType")} value={work.data.category} />
                <Field
                  label={t("record.portalCategory")}
                  value={work.data.work_category ?? t("record.notRecorded")}
                />
                <Field
                  label={t("record.location")}
                  value={[work.data.district, work.data.state].filter(Boolean).join(", ")}
                />
                <Field label={t("record.constituency")} value={work.data.constituency} />
                <Field label={t("record.mp")} value={titleCase(work.data.mp_name)} />
                <Field
                  label={t("record.districtAuthority")}
                  value={titleCase(work.data.ia_name)}
                />
                <Field
                  label={t("record.implementingAgency")}
                  value={
                    work.data.implementing_agency
                      ? work.data.implementing_agency
                      : t("record.noPayment")
                  }
                />
                <Field
                  label={t("record.vendor")}
                  value={
                    work.data.vendor_name
                      ? titleCase(work.data.vendor_name) +
                        (work.data.vendor_id
                          ? ` · ${t("record.vendorId", { id: work.data.vendor_id })}`
                          : "")
                      : t("record.noPayment")
                  }
                />
                <Field
                  label={t("record.letter")}
                  value={work.data.letter_no ?? t("record.notRecorded")}
                  mono
                />
                <Field
                  label={t("record.term")}
                  value={
                    work.data.term_start && work.data.term_end
                      ? t("record.termRange", {
                          start: fmt.date(work.data.term_start),
                          end: fmt.date(work.data.term_end),
                        })
                      : t("record.notRecorded")
                  }
                />
                <Field label={t("record.stage")} value={work.data.status} />
                <Field
                  label={t("record.sanctioned")}
                  value={
                    work.data.sanctioned_amount
                      ? fmt.rupees(work.data.sanctioned_amount)
                      : t("record.notSanctioned")
                  }
                />
                <Field
                  label={t("record.recommendedCost")}
                  value={fmt.rupees(work.data.estimated_cost)}
                />
                <Field
                  label={t("record.paid")}
                  value={
                    work.data.total_paid
                      ? fmt.rupees(work.data.total_paid)
                      : t("record.noneRecorded")
                  }
                />
                <Field
                  label={t("record.sanctionedOn")}
                  value={fmt.date(work.data.sanction_date)}
                />
                <Field
                  label={t("record.completedOn")}
                  value={
                    work.data.completion_date
                      ? fmt.date(work.data.completion_date)
                      : t("record.notRecorded")
                  }
                />
                <Field label={t("record.era")} value={work.data.era} mono />
              </div>
              <PaymentTimeline workId={work.data.work_id} />
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
              title={t("case.dup.empty")}
              hint={t("case.dup.emptyHint")}
            />
          ) : (
            <DuplicatesPanel findings={dupFindings} entityId={c.entity_id} />
          )}
        </Card>
      )}

      {netFindings.length > 0 && tab === "record" && (
        <Card title={t("case.network.title")} tight>
          {netFindings.map((f, i) => (
            <p key={i} className="text-sm" style={{ margin: 0 }}>
              <Building2 size={13} /> {f.summary}
            </p>
          ))}
        </Card>
      )}

      <div className="review-notice">
        <AlertTriangle size={13} />
        {lang === "en" ? c.disclaimer : t("case.disclaimer")}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------ payments */
function PaymentTimeline({ workId }: { workId: string }) {
  const { t, tn, fmt } = useI18n();
  const payments = useQuery({
    queryKey: ["payments", workId],
    queryFn: () => api.payments(workId),
    retry: false,
  });
  if (payments.isLoading) return <Skeleton h={120} />;
  if (payments.isError || !payments.data) return null;
  const { summary, payments: rows } = payments.data;
  return (
    <div className="stack gap-2">
      <div className="field-label">{t("pay.title")}</div>
      {rows.length === 0 ? (
        <p className="text-sm dim" style={{ margin: 0 }}>
          {t("pay.none")}
        </p>
      ) : (
        <>
          <p className="text-sm muted" style={{ margin: 0 }}>
            {tn("pay.summary", summary.count, {
              total: fmt.rupees(summary.total),
              vendors: summary.vendors,
              first: fmt.date(summary.first_paid_on),
              last: fmt.date(summary.last_paid_on),
            })}
            {summary.share_of_sanctioned !== null &&
              " " +
                t("pay.share", {
                  share: Math.round(summary.share_of_sanctioned * 100),
                  sanctioned: fmt.rupees(summary.sanctioned_amount),
                })}
          </p>
          <div className="table-scroll" style={{ maxHeight: 320 }}>
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: 110 }}>{t("pay.col.date")}</th>
                  <th style={{ width: 110 }}>{t("pay.col.amount")}</th>
                  <th>{t("pay.col.vendor")}</th>
                  <th style={{ width: 110 }}>{t("pay.col.status")}</th>
                  <th>{t("pay.col.notes")}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((p) => (
                  <tr key={p.seq}>
                    <td className="text-xs">{fmt.date(p.paid_on)}</td>
                    <td className="text-xs num">{fmt.rupees(p.amount)}</td>
                    <td className="text-xs">
                      {titleCase(p.vendor_name)}
                      {p.vendor_id && <span className="dim"> · {p.vendor_id}</span>}
                    </td>
                    <td className="text-xs muted">
                      {p.status === "Payment In-Progress"
                        ? t("pay.status.inProgress")
                        : p.status === "Payment Success"
                          ? t("pay.status.success")
                          : (p.status ?? "—")}
                    </td>
                    <td>
                      <span className="row gap-1" style={{ flexWrap: "wrap" }}>
                        {p.before_sanction && (
                          <Chip color="var(--risk-high)">{t("pay.mark.beforeSanction")}</Chip>
                        )}
                        {p.days_after_completion !== null && (
                          <Chip color="var(--risk-medium)">
                            {tn("pay.mark.afterCompletion", p.days_after_completion)}
                          </Chip>
                        )}
                        {p.repeats > 1 && (
                          <Chip color="var(--risk-medium)">
                            {tn("pay.mark.repeated", p.repeats)}
                          </Chip>
                        )}
                        {p.year_end_week && <Chip>{t("pay.mark.yearEnd")}</Chip>}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

/* ------------------------------------------------------------- duplicates */
type Details = Record<string, unknown>;
type BatchMember = {
  work_id: string;
  amount: number | null;
  payees: string[];
  status: string | null;
  letter_no: string | null;
};

function DuplicatesPanel({ findings, entityId }: { findings: Finding[]; entityId: string }) {
  const i18n = useI18n();
  const { t, tOr, fmt } = i18n;
  const batches = findings.filter((f) => f.rule_id === "D-DUP-01" && (f.details as Details).batch);
  const pairs = findings.filter((f) => f.rule_id === "D-DUP-01" && (f.details as Details).pair_work_id);
  const clusters = findings.filter((f) => f.rule_id === "D-DUP-02");
  const first = findings.map((f) => f.details as Details).find((d) => d.this_description);
  const totalMatches = Math.max(0, ...pairs.map((f) => Number((f.details as Details).total_matches ?? 0)));
  const stage = (s: unknown) => (s ? stageLabel(i18n, String(s)) : "—");
  const photoLabel = (check: unknown) => {
    switch (check) {
      case "same_file":
        return t("case.dup.photo.sameFile");
      case "look_alike":
        return t("case.dup.photo.lookAlike");
      case "different_photos":
        return t("case.dup.photo.different");
      case "no_photo":
        return t("case.dup.photo.none");
      case "not_completed":
        return t("case.dup.photo.notCompleted");
      case "failed":
        return t("case.dup.photo.failed");
      default:
        return t("case.dup.photo.notRun");
    }
  };
  // payee names as the portal records them: "GP Kapisda B" must not become "Gp Kapisda b"
  const names = (list: unknown) => (Array.isArray(list) && list.length ? list.join(", ") : "—");
  const nowrap = { whiteSpace: "nowrap" as const };

  return (
    <div className="stack gap-4">
      <p className="text-sm muted" style={{ margin: 0 }}>
        {t("case.dup.intro")}
      </p>
      {first && (
        <div>
          <div className="field-label">{t("case.dup.this")}</div>
          <code className="mono text-xs">{entityId}</code>
          {first.this_cost != null && (
            <span className="text-xs muted"> · {fmt.rupees(Number(first.this_cost))}</span>
          )}
          <p className="dup-text">{String(first.this_description)}</p>
        </div>
      )}

      {batches.map((f, i) => {
        const d = f.details as Details;
        // this work first, then the rest of the batch in portal order
        const members = [...((d.members as BatchMember[]) ?? [])].sort(
          (a, b) => Number(b.work_id === entityId) - Number(a.work_id === entityId),
        );
        const groups = (d.same_payee_groups as { payee: string; work_ids: string[] }[]) ?? [];
        const flagged = new Set(groups.flatMap((g) => g.work_ids));
        return (
          <div key={`b${i}`} className="dup-card">
            <div className="semibold">
              {t("case.dup.batch.title", { count: String(d.batch_size), mp: String(d.batch_mp ?? "") })}
            </div>
            <div className="dup-stats">
              <Stat label={t("case.dup.batch.total")} value={fmt.rupees(Number(d.batch_total))} />
              <Stat label={t("case.dup.batch.letters")} value={String(d.batch_letters ?? "—")} />
              <Stat label={t("case.dup.batch.payees")} value={String(d.batch_payees ?? "—")} />
              <Stat
                label={t("case.dup.photoCheck")}
                value={
                  Number((d.photo_summary as Details | undefined)?.checked ?? 0) > 0
                    ? t("case.dup.photo.batch", {
                        checked: String((d.photo_summary as Details).checked),
                        count: String(d.batch_size),
                        same: String((d.photo_summary as Details).shared_groups ?? 0),
                        alike: String((d.photo_summary as Details).look_alike_pairs ?? 0),
                      })
                    : t("case.dup.photo.notRun")
                }
              />
            </div>
            {Array.isArray((d.photo_summary as Details | undefined)?.look_alike) &&
              ((d.photo_summary as Details).look_alike as string[][]).length > 0 && (
                <Banner tone="warn">
                  {t("case.dup.photo.batchLookAlike", {
                    pairs: ((d.photo_summary as Details).look_alike as string[][])
                      .map((p) => p.join(" ↔ "))
                      .join(", "),
                  })}
                </Banner>
              )}
            <p className="text-sm muted" style={{ margin: 0 }}>
              {t("case.dup.batch.held")}
            </p>
            {groups.map((g) => (
              <Banner key={g.payee} tone="warn">
                {t("case.dup.batch.samePayee", { count: String(g.work_ids.length), payee: g.payee })}
              </Banner>
            ))}
            <div className="table-scroll" style={{ maxHeight: 320 }}>
              <table className="table">
                <thead>
                  <tr>
                    <th>{t("case.dup.col.work")}</th>
                    <th style={{ width: 110 }}>{t("case.dup.col.amount")}</th>
                    <th>{t("case.dup.col.paidTo")}</th>
                    <th style={{ width: 140 }}>{t("case.dup.col.stage")}</th>
                    <th>{t("case.dup.col.letter")}</th>
                  </tr>
                </thead>
                <tbody>
                  {members.map((m) => (
                    <tr key={m.work_id} className={m.work_id === entityId ? "semibold" : undefined}>
                      <td className="text-xs mono" style={nowrap}>
                        {m.work_id}
                        {m.work_id === entityId && <div className="dim">{t("case.dup.this")}</div>}
                      </td>
                      <td className="text-xs num">{m.amount != null ? fmt.rupees(m.amount) : "—"}</td>
                      <td className="text-xs">
                        {names(m.payees)}{" "}
                        {flagged.has(m.work_id) && (
                          <Chip color="var(--risk-medium)">{t("case.dup.samePayeeChip")}</Chip>
                        )}
                      </td>
                      <td className="text-xs muted">{stage(m.status)}</td>
                      <td className="text-xs mono muted" style={nowrap}>
                        {m.letter_no ?? "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {Number(d.members_shown) < Number(d.batch_size) && (
              <div className="text-xs dim">
                {t("case.dup.batch.shown", { shown: String(d.members_shown), count: String(d.batch_size) })}
              </div>
            )}
          </div>
        );
      })}

      {pairs.length > 0 && (
        <div className="dup-card">
          <div className="semibold">{t("case.dup.pairs.title")}</div>
          {pairs.some((f) => (f.details as Details).held) && (
            <p className="text-sm muted" style={{ margin: 0 }}>
              {t("case.dup.held")}
            </p>
          )}
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th>{t("case.dup.col.work")}</th>
                  <th style={{ width: 110 }}>{t("case.dup.col.amount")}</th>
                  <th>{t("case.dup.col.paidTo")}</th>
                  <th style={{ width: 130 }}>{t("case.dup.col.stage")}</th>
                  <th style={{ width: 90 }}>{t("case.dup.match")}</th>
                  <th>{t("case.dup.col.status")}</th>
                </tr>
              </thead>
              <tbody>
                {pairs.map((f, i) => {
                  const d = f.details as Details;
                  const strength = String(d.evidence_strength ?? "");
                  return (
                    <tr key={`p${i}`}>
                      <td className="text-xs">
                        <code className="mono" style={nowrap}>
                          {String(d.pair_work_id)}
                        </code>
                        {/* the matched description only where it differs from this work's */}
                        {String(d.other_description ?? "").trim().toLowerCase() !==
                          String(d.this_description ?? "").trim().toLowerCase() && (
                          <div className="muted">{String(d.other_description ?? "")}</div>
                        )}
                      </td>
                      <td className="text-xs num">
                        {d.other_cost != null ? fmt.rupees(Number(d.other_cost)) : "—"}
                      </td>
                      <td className="text-xs">
                        {names(d.other_payees)}
                        {Boolean(d.shared_payee) && (
                          <div>
                            <Chip color="var(--risk-medium)">
                              {t("case.dup.bothPaid", { payee: String(d.shared_payee) })}
                            </Chip>
                          </div>
                        )}
                      </td>
                      <td className="text-xs muted">{stage(d.other_status)}</td>
                      <td className="text-xs num">{Math.round(Number(d.semantic_sim ?? 0) * 100)}%</td>
                      <td className="text-xs">
                        {d.photo_match ? (
                          <Chip color="var(--risk-high)">{photoLabel(d.photo_check)}</Chip>
                        ) : (
                          <Chip>
                            {d.held
                              ? t("case.dup.status.held")
                              : strength
                                ? tOr(`strength.${strength}`, titleCase(strength))
                                : "—"}
                          </Chip>
                        )}
                        <div className="dim">
                          {d.duplication_mode ? dupMode(i18n, String(d.duplication_mode)) : ""}
                        </div>
                        {Boolean(d.held || d.photo_match) &&
                          (d.photo_check === "look_alike" ? (
                            <div>
                              <Chip color="var(--risk-medium)">{photoLabel(d.photo_check)}</Chip>
                              <div className="dim mono">
                                {String(d.this_file ?? "")} / {String(d.other_file ?? "")}
                              </div>
                            </div>
                          ) : (
                            <div className="dim">
                              {t("case.dup.photoCheck")}: {photoLabel(d.photo_check)}
                            </div>
                          ))}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {totalMatches > pairs.length && (
            <div className="text-xs dim">
              {t("case.dup.more", { shown: String(pairs.length), count: String(totalMatches) })}
            </div>
          )}
        </div>
      )}

      {clusters.map((f, i) => {
        const d = f.details as Details;
        return (
          <div key={`c${i}`} className="dup-card">
            <div className="semibold">
              {t("case.dup.cluster", { count: String(d.cluster_size), district: String(d.district) })}
            </div>
            <code className="json">{String(d.normalised_description ?? "")}</code>
            <div className="text-sm muted">
              {t("case.dup.total", { amount: fmt.rupees(Number(d.total_cost)) })}
            </div>
          </div>
        );
      })}

      <div className="text-xs dim">{t("case.dup.proximity")}</div>
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
  status: ReviewStatus;
  note: string;
  setNote: (v: string) => void;
  onAction: (a: "confirmed" | "false_positive" | "under_review") => void;
  pending: boolean;
  error: unknown;
}) {
  const { t } = useI18n();
  return (
    <Card title={t("review.title")} tight>
      <div className="flow">
        {REVIEW_FLOW.map((s, i) => (
          <span key={s} className={`flow-step${s === status ? " on" : ""}`}>
            {t(`status.${s}.label`)}
            {i < REVIEW_FLOW.length - 1 && <span className="flow-arrow">→</span>}
          </span>
        ))}
      </div>
      <input
        className="input"
        placeholder={t("review.note")}
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
          <Eye size={13} /> {t("review.markUnderReview")}
        </button>
        <button
          className="btn btn-sm btn-danger"
          disabled={pending}
          onClick={() => onAction("confirmed")}
        >
          <ArrowUpCircle size={13} /> {t("review.escalate")}
        </button>
        <button
          className="btn btn-sm btn-success"
          disabled={pending}
          onClick={() => onAction("false_positive")}
        >
          <CheckCircle2 size={13} /> {t("review.closeFalsePositive")}
        </button>
        {pending && <Spinner size={14} />}
      </div>
      {error ? (
        <div className="text-sm" style={{ color: "var(--danger)", marginTop: 8 }}>
          {t("review.error", { message: (error as Error).message })}
        </div>
      ) : null}
      <p className="text-xs dim" style={{ margin: "10px 0 0" }}>
        {t("review.footer")}
      </p>
    </Card>
  );
}
