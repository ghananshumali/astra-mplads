import { useQuery } from "@tanstack/react-query";
import { Building2, Info } from "lucide-react";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../api/client";
import type { CaseSummary } from "../api/types";
import { Banner, Card, Empty, ErrorState, RiskBadge, Skeleton } from "../components/ui";
import { compact, rupees, titleCase } from "../lib/format";
import { useAuthority } from "../state/AuthorityContext";
import "./pages.css";

/** Agency/vendor cases come from the network agent as agency-level flags. */
export default function Network() {
  const nav = useNavigate();
  const { scopeFilters } = useAuthority();
  const [picked, setPicked] = useState<CaseSummary | null>(null);

  const list = useQuery({
    queryKey: ["network-cases", scopeFilters],
    queryFn: () =>
      api.cases({
        ...scopeFilters,
        entity_types: ["agency"],
        order: "risk",
        limit: 60,
      }),
  });

  const agencyName = useMemo(
    () => (picked ? picked.entity_id.split(":").slice(1).join(":") : null),
    [picked],
  );

  const works = useQuery({
    queryKey: ["agency-works", agencyName],
    queryFn: () => api.agencyWorks(agencyName!, 40),
    enabled: !!agencyName,
  });

  const detail = useQuery({
    queryKey: ["case", picked?.flag_id, "district"],
    queryFn: () => api.caseDetail(picked!.flag_id, "district"),
    enabled: !!picked,
  });

  const netFinding = detail.data?.findings.find((f) => f.agent === "network");
  const d = (netFinding?.details ?? {}) as Record<string, unknown>;

  return (
    <div className="stack gap-5">
      <header className="page-head">
        <div>
          <h1 className="page-title">Contractor & agency network</h1>
          <p className="page-sub">
            Actors recurring across districts, and those whose works price above
            peer benchmarks
          </p>
        </div>
      </header>

      <Banner tone="neutral" icon={<Info size={15} />}>
        These are screening signals for verification — never an allegation
        against a firm or an officer. Actors supplying nationally procured items
        (vehicles, books, equipment) are deliberately down-weighted.
      </Banner>

      <div className="grid-2">
        <Card
          title="Flagged agencies and vendors"
          subtitle="Select one to see its portfolio"
          tight
        >
          {list.isError ? (
            <ErrorState error={list.error} onRetry={() => list.refetch()} />
          ) : list.isLoading ? (
            <Skeleton h={280} />
          ) : (list.data?.cases ?? []).length === 0 ? (
            <Empty
              icon={<Building2 size={20} />}
              title="No network signals in scope"
              hint="The network agent did not flag any agency or vendor within your authority scope."
            />
          ) : (
            <div className="table-scroll" style={{ maxHeight: 420 }}>
              <table className="table">
                <thead>
                  <tr>
                    <th style={{ width: 84 }}>Risk</th>
                    <th>Actor</th>
                    <th style={{ width: 92 }}>State</th>
                  </tr>
                </thead>
                <tbody>
                  {list.data!.cases.map((c) => (
                    <tr
                      key={c.flag_id}
                      className={picked?.flag_id === c.flag_id ? "selected" : ""}
                      onClick={() => setPicked(c)}
                    >
                      <td>
                        <RiskBadge score={c.risk_score} size="sm" />
                      </td>
                      <td>
                        <div className="semibold clamp-2">
                          {c.display_title}
                        </div>
                        <div className="text-xs dim clamp-2">
                          {c.primary_signal}
                        </div>
                      </td>
                      <td className="text-xs muted truncate">{c.state ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <Card
          title={picked ? titleCase(agencyName ?? "") : "Actor detail"}
          subtitle={
            picked
              ? "Pattern metrics and portfolio from the network agent"
              : "Select an actor from the list"
          }
          tight
        >
          {!picked ? (
            <Empty title="Nothing selected" hint="Choose an agency or vendor." />
          ) : detail.isLoading ? (
            <Skeleton h={220} />
          ) : (
            <div className="stack gap-4">
              {netFinding && (
                <>
                  <p className="text-sm" style={{ margin: 0 }}>
                    {netFinding.summary}
                  </p>
                  <div className="dup-stats">
                    <Metric label="Works" value={compact(Number(d.works ?? 0))} />
                    <Metric
                      label="Districts"
                      value={String(d.districts ?? "—")}
                    />
                    <Metric label="States" value={String(d.states ?? "—")} />
                    <Metric
                      label="Above benchmark"
                      value={`${Math.round(Number(d.overrun_share ?? 0) * 100)}%`}
                    />
                  </div>
                  {Boolean(d.likely_national_supplier) && (
                    <Banner tone="info">
                      Work types include nationally procured items, so operating
                      across many districts is expected. This signal is
                      down-weighted accordingly.
                    </Banner>
                  )}
                  {Array.isArray(d.district_list) && (
                    <div>
                      <div className="field-label">Districts covered</div>
                      <div className="text-sm muted">
                        {(d.district_list as string[]).join(", ")}
                      </div>
                    </div>
                  )}
                </>
              )}

              <div>
                <div className="field-label">Portfolio in the corpus</div>
                {works.isLoading ? (
                  <Skeleton h={140} />
                ) : (works.data ?? []).length === 0 ? (
                  <p className="text-sm dim">No other works found.</p>
                ) : (
                  <div className="table-scroll" style={{ maxHeight: 260 }}>
                    <table className="table">
                      <thead>
                        <tr>
                          <th>Work</th>
                          <th style={{ width: 120 }}>District</th>
                          <th style={{ width: 110 }}>Sanctioned</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(works.data ?? []).map((w) => (
                          <tr
                            key={w.work_id}
                            onClick={() =>
                              nav(`/cases?q=${encodeURIComponent(w.work_id!)}`)
                            }
                          >
                            <td className="text-xs mono truncate">
                              {w.work_id}
                            </td>
                            <td className="text-xs muted truncate">
                              {w.district ?? "—"}
                            </td>
                            <td className="text-xs num">
                              {rupees(w.sanctioned_amount ?? null)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat">
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}
