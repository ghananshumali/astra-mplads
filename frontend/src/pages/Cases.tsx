import { useQuery } from "@tanstack/react-query";
import {
  ArrowDown,
  ArrowUp,
  ChevronLeft,
  ChevronRight,
  Filter,
  Search,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import type { CaseFilters, ReviewStatus } from "../api/types";
import CaseDetail from "../components/case/CaseDetail";
import {
  Card,
  Chip,
  Empty,
  ErrorState,
  RiskBadge,
  SkeletonRows,
  StatusChip,
} from "../components/ui";
import { AGENT_META, STATUS_META, TIERS, compact } from "../lib/format";
import { useAuthority } from "../state/AuthorityContext";
import "./pages.css";

const PAGE = 25;

/** Debounce so typing in search does not fire a request per keystroke. */
function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export default function Cases() {
  const nav = useNavigate();
  const { flagId } = useParams();
  const [params, setParams] = useSearchParams();
  const { tier, scopeFilters, scopeLabel } = useAuthority();

  const [search, setSearch] = useState("");
  const debounced = useDebounced(search);
  const [page, setPage] = useState(0);
  const [order, setOrder] = useState("risk");
  const [showFilters, setShowFilters] = useState(false);

  // filters seeded from the URL so overview drill-downs land pre-filtered
  const [minScore, setMinScore] = useState(Number(params.get("min") ?? 0));
  const [statuses, setStatuses] = useState<ReviewStatus[]>(
    (params.get("status")?.split(",").filter(Boolean) as ReviewStatus[]) ?? [],
  );
  const [rules, setRules] = useState<string[]>(
    params.get("rule")?.split(",").filter(Boolean) ?? [],
  );
  const [stateFilter, setStateFilter] = useState(params.get("state") ?? "");
  const [districtFilter, setDistrictFilter] = useState(
    params.get("district") ?? "",
  );

  useEffect(() => setPage(0), [debounced, minScore, statuses, rules, stateFilter, districtFilter, tier]);

  const facets = useQuery({
    queryKey: ["facets"],
    queryFn: api.facets,
    staleTime: 5 * 60_000,
  });

  const filters: CaseFilters = useMemo(() => {
    const f: CaseFilters = {
      ...scopeFilters,
      min_score: minScore || undefined,
      statuses: statuses.length ? statuses : undefined,
      rule_ids: rules.length ? rules : undefined,
      search: debounced || undefined,
      order,
      limit: PAGE,
      offset: page * PAGE,
    };
    // Ministry may narrow by state/district; scoped tiers cannot widen theirs.
    if (tier === "ministry") {
      if (stateFilter) f.states = [stateFilter];
      if (districtFilter) f.districts = [districtFilter];
    }
    return f;
  }, [
    scopeFilters, minScore, statuses, rules, debounced, order, page, tier,
    stateFilter, districtFilter,
  ]);

  const list = useQuery({
    queryKey: ["cases", filters],
    queryFn: () => api.cases(filters),
    placeholderData: (prev) => prev,
  });

  const total = list.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE));
  const activeFilters =
    (minScore ? 1 : 0) + statuses.length + rules.length +
    (stateFilter ? 1 : 0) + (districtFilter ? 1 : 0);

  const clearAll = () => {
    setMinScore(0);
    setStatuses([]);
    setRules([]);
    setStateFilter("");
    setDistrictFilter("");
    setSearch("");
    setParams({});
  };

  const sortBy = (key: string) => {
    setOrder((o) =>
      key === "risk" ? (o === "risk" ? "risk_asc" : "risk") : key,
    );
  };

  return (
    <div className={`cases-layout${flagId ? "" : " browsing"}`}>
      <div className="cases-list stack gap-4">
        <header className="page-head" style={{ marginBottom: 0 }}>
          <div>
            <h1 className="page-title">Risk cases</h1>
            <p className="page-sub">
              {TIERS[tier].short} scope · {scopeLabel}
            </p>
          </div>
        </header>

        <Card tight>
          <div className="row gap-3" style={{ flexWrap: "wrap" }}>
            <div className="search-wrap grow" style={{ minWidth: 200 }}>
              <Search size={14} />
              <input
                className="input"
                placeholder="Search work code, title or district…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
            <button
              className={`btn btn-sm${showFilters ? " btn-primary" : ""}`}
              onClick={() => setShowFilters((f) => !f)}
            >
              <Filter size={13} /> Filters
              {activeFilters > 0 && (
                <span className="tab-count">{activeFilters}</span>
              )}
            </button>
            {activeFilters > 0 && (
              <button className="btn btn-sm btn-ghost" onClick={clearAll}>
                <X size={13} /> Clear
              </button>
            )}
          </div>

          {showFilters && (
            <div className="filter-panel fade-in">
              <div className="filter-field">
                <label className="field-label">Minimum risk score</label>
                <div className="row gap-3">
                  <input
                    type="range"
                    min={0}
                    max={100}
                    step={5}
                    value={minScore}
                    onChange={(e) => setMinScore(Number(e.target.value))}
                    style={{ flex: 1 }}
                  />
                  <span className="num semibold" style={{ width: 28 }}>
                    {minScore}
                  </span>
                </div>
              </div>

              <div className="filter-field">
                <label className="field-label">Review status</label>
                <div className="row gap-2" style={{ flexWrap: "wrap" }}>
                  {(Object.keys(STATUS_META) as ReviewStatus[]).map((s) => (
                    <button
                      key={s}
                      className={`pill${statuses.includes(s) ? " on" : ""}`}
                      onClick={() =>
                        setStatuses((cur) =>
                          cur.includes(s)
                            ? cur.filter((x) => x !== s)
                            : [...cur, s],
                        )
                      }
                    >
                      {STATUS_META[s].short}
                    </button>
                  ))}
                </div>
              </div>

              <div className="filter-field">
                <label className="field-label">Detection type</label>
                <div className="row gap-2" style={{ flexWrap: "wrap" }}>
                  {(facets.data?.rules ?? [])
                    .filter((r) => r.count > 0)
                    .map((r) => (
                      <button
                        key={r.rule_id}
                        className={`pill${rules.includes(r.rule_id) ? " on" : ""}`}
                        title={r.title}
                        onClick={() =>
                          setRules((cur) =>
                            cur.includes(r.rule_id)
                              ? cur.filter((x) => x !== r.rule_id)
                              : [...cur, r.rule_id],
                          )
                        }
                      >
                        {r.title.length > 34
                          ? `${r.title.slice(0, 34)}…`
                          : r.title}
                      </button>
                    ))}
                </div>
              </div>

              {tier === "ministry" && (
                <div className="row gap-3" style={{ flexWrap: "wrap" }}>
                  <div className="filter-field grow">
                    <label className="field-label">State</label>
                    <select
                      className="select"
                      value={stateFilter}
                      onChange={(e) => setStateFilter(e.target.value)}
                    >
                      <option value="">All states</option>
                      {(facets.data?.states ?? []).map((s) => (
                        <option key={s}>{s}</option>
                      ))}
                    </select>
                  </div>
                  <div className="filter-field grow">
                    <label className="field-label">District</label>
                    <select
                      className="select"
                      value={districtFilter}
                      onChange={(e) => setDistrictFilter(e.target.value)}
                    >
                      <option value="">All districts</option>
                      {(facets.data?.districts ?? []).map((d) => (
                        <option key={d}>{d}</option>
                      ))}
                    </select>
                  </div>
                </div>
              )}
            </div>
          )}
        </Card>

        <Card tight className="grow">
          {list.isError ? (
            <ErrorState error={list.error} onRetry={() => list.refetch()} />
          ) : list.isLoading ? (
            <SkeletonRows rows={8} />
          ) : total === 0 ? (
            <Empty
              title="No cases match these filters"
              hint="Widen the filters, clear the search, or switch authority scope."
              action={
                activeFilters > 0 ? (
                  <button className="btn btn-sm" onClick={clearAll}>
                    Clear filters
                  </button>
                ) : undefined
              }
            />
          ) : (
            <>
              <div className="table-scroll">
                <table className="table">
                  <thead>
                    <tr>
                      <th
                        className="sortable"
                        onClick={() => sortBy("risk")}
                        style={{ width: 96 }}
                      >
                        <span className="row gap-1">
                          Risk
                          {order === "risk" ? (
                            <ArrowDown size={11} />
                          ) : order === "risk_asc" ? (
                            <ArrowUp size={11} />
                          ) : null}
                        </span>
                      </th>
                      <th>Case</th>
                      <th
                        className="sortable"
                        style={{ width: 190 }}
                        onClick={() => sortBy("state")}
                      >
                        Location
                      </th>
                      <th style={{ width: 148 }}>Signals</th>
                      <th style={{ width: 110 }}>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.data!.cases.map((c) => (
                      <tr
                        key={c.flag_id}
                        className={flagId === c.flag_id ? "selected" : ""}
                        onClick={() => nav(`/cases/${c.flag_id}`)}
                      >
                        <td>
                          <RiskBadge score={c.risk_score} size="sm" />
                        </td>
                        <td>
                          <div className="semibold clamp-2">
                            {c.display_title}
                          </div>
                          <div className="text-xs dim truncate">
                            {c.primary_signal}
                          </div>
                        </td>
                        <td className="text-sm muted">
                          <div className="truncate">{c.district ?? "—"}</div>
                          <div className="text-xs dim truncate">
                            {c.state ?? "—"}
                          </div>
                        </td>
                        <td>
                          <div className="row gap-1" style={{ flexWrap: "wrap" }}>
                            {c.agents.map((a) => (
                              <span
                                key={a}
                                className="agent-dot"
                                title={AGENT_META[a]?.label ?? a}
                                style={{
                                  background: AGENT_META[a]?.color ?? "var(--text-3)",
                                }}
                              />
                            ))}
                            <span className="text-xs dim">
                              {c.finding_count}
                            </span>
                          </div>
                        </td>
                        <td>
                          <StatusChip status={c.review_status} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="pager">
                <span className="text-sm muted">
                  <b>{compact(total)}</b> cases · page {page + 1} of {pages}
                </span>
                <div className="row gap-2">
                  <button
                    className="btn btn-sm"
                    disabled={page === 0}
                    onClick={() => setPage((p) => Math.max(0, p - 1))}
                  >
                    <ChevronLeft size={13} /> Prev
                  </button>
                  <button
                    className="btn btn-sm"
                    disabled={page + 1 >= pages}
                    onClick={() => setPage((p) => p + 1)}
                  >
                    Next <ChevronRight size={13} />
                  </button>
                </div>
              </div>
            </>
          )}
        </Card>
      </div>

      <aside className={`cases-detail${flagId ? " open" : ""}`}>
        {flagId ? (
          <CaseDetail flagId={flagId} onClose={() => nav("/cases")} />
        ) : (
          <Card>
            <Empty
              title="Select a case to investigate"
              hint="Choose a case from the list to see why it was flagged, the evidence behind it, and the actions available to your authority."
            />
          </Card>
        )}
      </aside>
    </div>
  );
}

export { Chip };
