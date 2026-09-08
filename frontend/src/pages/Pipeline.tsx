import { useQuery } from "@tanstack/react-query";
import { CircleDot, Circle } from "lucide-react";

import { api } from "../api/client";
import { Banner, Card, ErrorState, Skeleton } from "../components/ui";
import { AGENT_META, compact, relativeTime } from "../lib/format";
import "./pages.css";

export default function Pipeline() {
  const meta = useQuery({ queryKey: ["pipeline"], queryFn: api.pipeline });

  if (meta.isError)
    return <ErrorState error={meta.error} onRetry={() => meta.refetch()} />;

  const zero = (meta.data?.rule_coverage ?? []).filter((r) => r.findings === 0);

  return (
    <div className="stack gap-5">
      <header className="page-head">
        <div>
          <h1 className="page-title">Analysis pipeline</h1>
          <p className="page-sub">
            Which agents ran on this batch, and what each rule found
            {meta.data?.ran_at
              ? ` · last run ${relativeTime(meta.data.ran_at)}`
              : ""}
          </p>
        </div>
      </header>

      <Card
        title="Agent routing"
        subtitle="The orchestrator dispatches only agents whose required inputs exist in the current data, and records that decision"
      >
        {meta.isLoading ? (
          <Skeleton h={140} />
        ) : (
          <div className="stack gap-3">
            {(meta.data?.router_trace ?? []).map((t) => (
              <div key={t.agent} className="row gap-3">
                {t.dispatched ? (
                  <CircleDot
                    size={16}
                    style={{ color: AGENT_META[t.agent]?.color }}
                  />
                ) : (
                  <Circle size={16} className="dim" />
                )}
                <div className="grow">
                  <div className="semibold">
                    {AGENT_META[t.agent]?.label ?? t.agent}
                  </div>
                  <div className="text-xs dim">
                    {AGENT_META[t.agent]?.role ?? ""}
                  </div>
                </div>
                <div className="text-sm">
                  {t.dispatched ? (
                    <b>{compact(t.findings ?? 0)} findings</b>
                  ) : (
                    <span className="dim">{t.reason}</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card
        title="Rule coverage"
        subtitle="Every check the compliance and analytics engines evaluated on this batch"
        tight
      >
        {meta.isLoading ? (
          <Skeleton h={220} />
        ) : (
          <>
            <div className="table-scroll" style={{ maxHeight: 420 }}>
              <table className="table">
                <thead>
                  <tr>
                    <th style={{ width: 108 }}>Rule</th>
                    <th>Check</th>
                    <th style={{ width: 110 }}>Cases found</th>
                  </tr>
                </thead>
                <tbody>
                  {(meta.data?.rule_coverage ?? []).map((r) => (
                    <tr key={r.rule_id} style={{ cursor: "default" }}>
                      <td className="mono text-xs">{r.rule_id}</td>
                      <td>{r.title}</td>
                      <td
                        className="num semibold"
                        style={{
                          color: r.findings ? "var(--text)" : "var(--text-3)",
                        }}
                      >
                        {compact(r.findings)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {zero.length > 0 && (
              <div style={{ paddingTop: 12 }}>
                <Banner tone="neutral">
                  Rules reporting zero are shown deliberately:{" "}
                  <b>{zero.map((z) => z.rule_id).join(", ")}</b> found no
                  matches — either the corpus is clean on that check, or this
                  data source lacks the inputs the rule requires.
                </Banner>
              </div>
            )}
          </>
        )}
      </Card>
    </div>
  );
}
