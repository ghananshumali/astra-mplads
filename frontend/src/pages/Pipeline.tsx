import { useQuery } from "@tanstack/react-query";
import { CircleDot, Circle } from "lucide-react";

import { api } from "../api/client";
import { Banner, Card, ErrorState, Skeleton } from "../components/ui";
import { useI18n } from "../i18n/context";
import { agentText, ruleTitle } from "../i18n/labels";
import { T } from "../i18n/T";
import { AGENT_COLOR, compact } from "../lib/format";
import "./pages.css";

export default function Pipeline() {
  const i18n = useI18n();
  const { t, tOr, fmt, language } = i18n;
  const meta = useQuery({
    queryKey: ["pipeline", language.code],
    queryFn: () => api.pipeline(language.code),
  });

  if (meta.isError)
    return <ErrorState error={meta.error} onRetry={() => meta.refetch()} />;

  const coverage = meta.data?.rule_coverage ?? [];
  const zero = coverage.filter((r) => r.findings === 0 && !r.stood_down);
  const unchecked = Object.entries(meta.data?.unchecked_provisions ?? {});

  return (
    <div className="stack gap-5">
      <header className="page-head">
        <div>
          <h1 className="page-title">{t("pipe.title")}</h1>
          <p className="page-sub">
            {t("pipe.sub")}
            {meta.data?.ran_at ? ` · ${t("pipe.lastRun", { when: fmt.ago(meta.data.ran_at) })}` : ""}
          </p>
        </div>
      </header>

      <Card title={t("pipe.routing.title")} subtitle={t("pipe.routing.sub")}>
        {meta.isLoading ? (
          <Skeleton h={140} />
        ) : (
          <div className="stack gap-3">
            {(meta.data?.router_trace ?? []).map((tr) => (
              <div key={tr.agent} className="row gap-3">
                {tr.dispatched ? (
                  <CircleDot size={16} style={{ color: AGENT_COLOR[tr.agent] }} />
                ) : (
                  <Circle size={16} className="dim" />
                )}
                <div className="grow">
                  <div className="semibold">{agentText(i18n, tr.agent, "label")}</div>
                  <div className="text-xs dim">{agentText(i18n, tr.agent, "role")}</div>
                </div>
                <div className="text-sm">
                  {tr.dispatched ? (
                    <b>{t("pipe.findings", { count: compact(tr.findings ?? 0) })}</b>
                  ) : (
                    <span className="dim">{tr.reason}</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title={t("pipe.coverage.title")} subtitle={t("pipe.coverage.sub")} tight>
        {meta.isLoading ? (
          <Skeleton h={220} />
        ) : (
          <>
            <div className="table-scroll" style={{ maxHeight: 420 }}>
              <table className="table">
                <thead>
                  <tr>
                    <th style={{ width: 108 }}>{t("pipe.col.rule")}</th>
                    <th>{t("pipe.col.check")}</th>
                    <th style={{ width: 150 }}>{t("pipe.col.basis")}</th>
                    <th style={{ width: 110 }}>{t("pipe.col.findings")}</th>
                  </tr>
                </thead>
                <tbody>
                  {coverage.map((r) => (
                    <tr key={r.rule_id} style={{ cursor: "default" }}>
                      <td className="mono text-xs">{r.rule_id}</td>
                      <td>
                        {ruleTitle(i18n, r.rule_id, r.title)}
                        {r.stood_down && (
                          <div className="text-xs dim">
                            {t("pipe.notEvaluated", { reason: r.stood_down })}
                          </div>
                        )}
                      </td>
                      <td className="text-xs muted">
                        {r.basis ? tOr(`basis.${r.basis}`, r.basis) : "—"}
                      </td>
                      <td
                        className="num semibold"
                        style={{
                          color: r.findings ? "var(--text)" : "var(--text-3)",
                        }}
                      >
                        {r.stood_down ? "—" : compact(r.findings)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {zero.length > 0 && (
              <div style={{ paddingTop: 12 }}>
                <Banner tone="neutral">
                  <T
                    k="pipe.zero"
                    values={{ rules: <b>{zero.map((z) => z.rule_id).join(", ")}</b> }}
                  />
                </Banner>
              </div>
            )}
            {unchecked.length > 0 && (
              <div style={{ paddingTop: 12 }}>
                <Banner tone="neutral">
                  <b>{t("pipe.unchecked")}</b>
                  <ul className="text-sm" style={{ margin: "6px 0 0", paddingInlineStart: 18 }}>
                    {unchecked.map(([para, why]) => (
                      <li key={para}>
                        {t("pipe.para", { para, text: tOr(`unchecked.${para}`, why) })}
                      </li>
                    ))}
                  </ul>
                </Banner>
              </div>
            )}
          </>
        )}
      </Card>
    </div>
  );
}
