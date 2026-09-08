import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, Database, Sparkles } from "lucide-react";

import { api } from "../api/client";
import { Banner, Card, Chip, ErrorState, Skeleton } from "../components/ui";
import { compact, formatDate } from "../lib/format";
import "./pages.css";

export default function DataSource() {
  const src = useQuery({ queryKey: ["data-source"], queryFn: api.dataSource });
  const llm = useQuery({ queryKey: ["llm"], queryFn: api.llm });

  if (src.isError)
    return <ErrorState error={src.error} onRetry={() => src.refetch()} />;

  const fresh = src.data?.freshness_vs_live_portal;

  return (
    <div className="stack gap-5">
      <header className="page-head">
        <div>
          <h1 className="page-title">Data source & provenance</h1>
          <p className="page-sub">
            Where the analysed corpus came from, and how current it is
          </p>
        </div>
      </header>

      {src.isLoading ? (
        <Skeleton h={120} />
      ) : (
        <>
          <div className="kpi-grid">
            <div className="kpi">
              <span
                className="kpi-icon"
                style={{ color: "var(--navy-600)", background: "#1a437f14" }}
              >
                <Database size={18} />
              </span>
              <span className="kpi-body">
                <span className="kpi-label">Resolved mode</span>
                <span className="kpi-value" style={{ color: "var(--navy-600)" }}>
                  {(src.data?.mode_resolved ?? "—").toUpperCase()}
                </span>
              </span>
            </div>
            <div className="kpi">
              <span
                className="kpi-icon"
                style={{ color: "var(--navy-600)", background: "#1a437f14" }}
              >
                <Database size={18} />
              </span>
              <span className="kpi-body">
                <span className="kpi-label">Works ingested</span>
                <span className="kpi-value" style={{ color: "var(--navy-600)" }}>
                  {compact(src.data?.works ?? 0)}
                </span>
              </span>
            </div>
            <div className="kpi">
              <span
                className="kpi-icon"
                style={{ color: "var(--navy-600)", background: "#1a437f14" }}
              >
                <Database size={18} />
              </span>
              <span className="kpi-body">
                <span className="kpi-label">Fund-flow records</span>
                <span className="kpi-value" style={{ color: "var(--navy-600)" }}>
                  {compact(src.data?.fundflows ?? 0)}
                </span>
              </span>
            </div>
          </div>

          {fresh && (
            <Banner tone="success" icon={<CheckCircle2 size={15} />}>
              <b>Live check against mplads.mospi.gov.in</b> — this batch holds{" "}
              {compact(fresh.local_recommended_works)} recommended works; the
              official portal reported{" "}
              {compact(fresh.live_recommended_works)} when queried at{" "}
              {formatDate(fresh.checked_at)}. That is{" "}
              <b>{fresh.coverage_pct}% coverage</b> ({fresh.tenure}).
            </Banner>
          )}

          {llm.data && (
            <Card title="AI synthesis provider" tight>
              <div className="row gap-3" style={{ flexWrap: "wrap" }}>
                <Chip
                  color={llm.data.configured ? "#0f5233" : "var(--text-2)"}
                  bg={llm.data.configured ? "var(--success-bg)" : "var(--surface-3)"}
                  dot
                >
                  <Sparkles size={11} />
                  {llm.data.configured ? "Configured" : "Not configured"}
                </Chip>
                <span className="text-sm muted">
                  Provider <b>{llm.data.provider}</b> · model{" "}
                  <b className="mono">{llm.data.model}</b>
                  {llm.data.key_hint ? ` · key ${llm.data.key_hint}` : ""}
                </span>
              </div>
              {!llm.data.configured && (
                <p className="text-sm dim" style={{ margin: "10px 0 0" }}>
                  No <code>GROQ_API_KEY</code> is set, so case synthesis uses the
                  deterministic layer. Add a key to <code>.env</code> and restart
                  the API to enable AI synthesis. Every other capability is
                  unaffected.
                </p>
              )}
            </Card>
          )}

          <Card
            title="Source ledger for this batch"
            subtitle="Dual-mode ingestion: live official interfaces are attempted under a strict time budget, with the official CSV exports supplying the corpus when live access is slow or partial"
            tight
          >
            <div className="table-scroll" style={{ maxHeight: 380 }}>
              <table className="table">
                <thead>
                  <tr>
                    <th>Source</th>
                    <th style={{ width: 88 }}>Mode</th>
                    <th style={{ width: 92 }}>Rows</th>
                    <th style={{ width: 108 }}>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {(src.data?.provenance ?? []).map((p, i) => (
                    <tr key={i} style={{ cursor: "default" }}>
                      <td>
                        <div className="semibold text-sm">{p.source}</div>
                        <div className="text-xs dim clamp-2">{p.detail}</div>
                      </td>
                      <td className="text-xs">{p.mode}</td>
                      <td className="num text-sm">{compact(p.rows)}</td>
                      <td>
                        <Chip
                          color={
                            p.status === "ok" || p.status === "selected"
                              ? "#0f5233"
                              : "var(--text-2)"
                          }
                          bg={
                            p.status === "ok" || p.status === "selected"
                              ? "var(--success-bg)"
                              : "var(--surface-3)"
                          }
                        >
                          {p.status}
                        </Chip>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <div className="grid-2">
            <Card title="Work records by data era" tight>
              <EraList data={src.data?.work_eras} />
            </Card>
            <Card title="Fund-flow records by data era" tight>
              <EraList data={src.data?.fundflow_eras} />
              <p className="text-xs dim" style={{ margin: "10px 0 0" }}>
                Pre-2023 rows come from the live open-data interfaces, which is
                what makes the era-separated baselines real rather than
                hypothetical.
              </p>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}

function EraList({ data }: { data?: Record<string, number> }) {
  const entries = Object.entries(data ?? {});
  if (!entries.length) return <p className="text-sm dim">No data.</p>;
  const total = entries.reduce((a, [, v]) => a + v, 0);
  return (
    <div className="stack gap-2">
      {entries.map(([era, n]) => (
        <div key={era} className="row gap-3">
          <span className="text-sm mono" style={{ width: 90 }}>
            {era}
          </span>
          <span className="det-bar grow">
            <span style={{ width: `${(n / total) * 100}%` }} />
          </span>
          <span className="num text-sm semibold" style={{ width: 66, textAlign: "right" }}>
            {compact(n)}
          </span>
        </div>
      ))}
    </div>
  );
}
