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
import { useI18n } from "../i18n/context";
import { agentText, ruleTitle } from "../i18n/labels";
import { T } from "../i18n/T";
import { AGENT_COLOR, REVIEW_STATUSES, compact } from "../lib/format";
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
  const i18n = useI18n();
  const { t } = i18n;
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

  const lang = i18n.language.code;
  const list = useQuery({
    queryKey: ["cases", filters, lang],
    queryFn: () => api.cases(filters, lang),
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
            <h1 className="page-title">{t("cases.title")}</h1>
            <p className="page-sub">
              {t("cases.scope", { tier: t(`tier.${tier}.short`), scope: scopeLabel })}
            </p>
          </div>
        </header>

        <Card tight>
          <div className="row gap-3" style={{ flexWrap: "wrap" }}>
            <div className="search-wrap grow" style={{ minWidth: 200 }}>
              <Search size={14} />
              <input
                className="input"
                placeholder={t("cases.search")}
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
            <button
              className={`btn btn-sm${showFilters ? " btn-primary" : ""}`}
              onClick={() => setShowFilters((f) => !f)}
            >
              <Filter size={13} /> {t("cases.filters")}
              {activeFilters > 0 && (
                <span className="tab-count">{activeFilters}</span>
              )}
            </button>
            {activeFilters > 0 && (
              <button className="btn btn-sm btn-ghost" onClick={clearAll}>
                <X size={13} /> {t("cases.clear")}
              </button>
            )}
          </div>

          {showFilters && (
            <div className="filter-panel fade-in">
              <div className="filter-field">
                <label className="field-label">{t("cases.minScore")}</label>
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
                <label className="field-label">{t("cases.reviewStatus")}</label>
                <div className="row gap-2" style={{ flexWrap: "wrap" }}>
                  {REVIEW_STATUSES.map((s) => (
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
                      {t(`status.${s}.short`)}
                    </button>
                  ))}
                </div>
              </div>

              <div className="filter-field">
                <label className="field-label">{t("cases.detectionType")}</label>
                <div className="row gap-2" style={{ flexWrap: "wrap" }}>
                  {(facets.data?.rules ?? [])
                    .filter((r) => r.count > 0)
                    .map((r) => {
                      const title = ruleTitle(i18n, r.rule_id, r.title);
                      return (
                        <button
                          key={r.rule_id}
                          className={`pill${rules.includes(r.rule_id) ? " on" : ""}`}
                          title={title}
                          onClick={() =>
                            setRules((cur) =>
                              cur.includes(r.rule_id)
                                ? cur.filter((x) => x !== r.rule_id)
                                : [...cur, r.rule_id],
                            )
                          }
                        >
                          {title.length > 34 ? `${title.slice(0, 34)}…` : title}
                        </button>
                      );
                    })}
                </div>
              </div>

              {tier === "ministry" && (
                <div className="row gap-3" style={{ flexWrap: "wrap" }}>
                  <div className="filter-field grow">
                    <label className="field-label">{t("cases.state")}</label>
                    <select
                      className="select"
                      value={stateFilter}
                      onChange={(e) => setStateFilter(e.target.value)}
                    >
                      <option value="">{t("scope.allStates")}</option>
                      {(facets.data?.states ?? []).map((s) => (
                        <option key={s}>{s}</option>
                      ))}
                    </select>
                  </div>
                  <div className="filter-field grow">
                    <label className="field-label">{t("cases.district")}</label>
                    <select
                      className="select"
                      value={districtFilter}
                      onChange={(e) => setDistrictFilter(e.target.value)}
                    >
                      <option value="">{t("cases.allDistricts")}</option>
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
              title={t("cases.empty")}
              hint={t("cases.emptyHint")}
              action={
                activeFilters > 0 ? (
                  <button className="btn btn-sm" onClick={clearAll}>
                    {t("cases.clearFilters")}
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
                          {t("cases.col.risk")}
                          {order === "risk" ? (
                            <ArrowDown size={11} />
                          ) : order === "risk_asc" ? (
                            <ArrowUp size={11} />
                          ) : null}
                        </span>
                      </th>
                      <th>{t("cases.col.case")}</th>
                      <th
                        className="sortable"
                        style={{ width: 190 }}
                        onClick={() => sortBy("state")}
                      >
                        {t("cases.col.location")}
                      </th>
                      <th style={{ width: 148 }}>{t("cases.col.signals")}</th>
                      <th style={{ width: 110 }}>{t("cases.col.status")}</th>
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
                                title={agentText(i18n, a, "label")}
                                style={{
                                  background: AGENT_COLOR[a] ?? "var(--text-3)",
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
                  <T
                    k="cases.pager"
                    values={{
                      total: <b>{compact(total)}</b>,
                      page: compact(page + 1),
                      pages: compact(pages),
                    }}
                  />
                </span>
                <div className="row gap-2">
                  <button
                    className="btn btn-sm"
                    disabled={page === 0}
                    onClick={() => setPage((p) => Math.max(0, p - 1))}
                  >
                    <ChevronLeft size={13} className="flip-rtl" /> {t("cases.prev")}
                  </button>
                  <button
                    className="btn btn-sm"
                    disabled={page + 1 >= pages}
                    onClick={() => setPage((p) => p + 1)}
                  >
                    {t("cases.next")} <ChevronRight size={13} className="flip-rtl" />
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
            <Empty title={t("cases.select")} hint={t("cases.selectHint")} />
          </Card>
        )}
      </aside>
    </div>
  );
}

export { Chip };
