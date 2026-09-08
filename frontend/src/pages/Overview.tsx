import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowRight,
  ClipboardCheck,
  FileSearch,
  Layers,
  ShieldAlert,
} from "lucide-react";
import {
  Bar,
  BarChart,
  Cell,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useNavigate } from "react-router-dom";

import { api } from "../api/client";
import type { DistrictRow, StateRow } from "../api/types";
import {
  Banner,
  Card,
  Empty,
  ErrorState,
  Skeleton,
  Tip,
} from "../components/ui";
import { RISK_META, TIERS, compact } from "../lib/format";
import { useAuthority } from "../state/AuthorityContext";
import "./pages.css";

/* ------------------------------------------------------------------ KPI */
function Kpi({
  label,
  value,
  hint,
  icon,
  tone,
  onClick,
  loading,
}: {
  label: string;
  value: string | number;
  hint?: string;
  icon: React.ReactNode;
  tone?: "danger" | "warn" | "info" | "success";
  onClick?: () => void;
  loading?: boolean;
}) {
  const color =
    tone === "danger"
      ? "var(--risk-high)"
      : tone === "warn"
        ? "var(--risk-medium)"
        : tone === "success"
          ? "var(--success)"
          : "var(--navy-600)";
  return (
    <button
      className={`kpi${onClick ? " clickable" : ""}`}
      onClick={onClick}
      disabled={!onClick}
      type="button"
    >
      <span className="kpi-icon" style={{ color, background: `${color}14` }}>
        {icon}
      </span>
      <span className="kpi-body">
        <span className="kpi-label">
          {label}
          {hint && (
            <Tip text={hint}>
              <span className="kpi-hint">?</span>
            </Tip>
          )}
        </span>
        {loading ? (
          <Skeleton h={26} w={72} style={{ marginTop: 4 }} />
        ) : (
          <span className="kpi-value" style={{ color }}>
            {typeof value === "number" ? compact(value) : value}
          </span>
        )}
      </span>
      {onClick && <ArrowRight size={14} className="kpi-go" />}
    </button>
  );
}

/* ------------------------------------------------------------- overview */
export default function Overview() {
  const nav = useNavigate();
  const { tier, scopeFilters, scopeLabel } = useAuthority();

  const stats = useQuery({
    queryKey: ["stats", scopeFilters],
    queryFn: () => api.stats(scopeFilters),
  });
  const states = useQuery({
    queryKey: ["states"],
    queryFn: api.states,
    enabled: tier === "ministry",
  });
  const districts = useQuery({
    queryKey: ["districts", scopeFilters.states?.[0]],
    queryFn: () => api.districts(scopeFilters.states?.[0], 12),
    enabled: tier === "state" || tier === "ministry",
  });
  const detections = useQuery({
    queryKey: ["detections", scopeFilters],
    queryFn: () => api.detections(scopeFilters),
  });
  const topCases = useQuery({
    queryKey: ["top-cases", scopeFilters],
    queryFn: () =>
      api.cases({ ...scopeFilters, order: "risk", limit: 6, min_score: 40 }),
  });

  const s = stats.data;
  const goCases = (extra: Record<string, string> = {}) => {
    const p = new URLSearchParams(extra);
    nav(`/cases${p.toString() ? `?${p}` : ""}`);
  };

  const bandData = s
    ? [
        { name: "High", value: s.high, fill: RISK_META.high.color },
        { name: "Medium", value: s.medium, fill: RISK_META.medium.color },
        { name: "Low", value: s.low, fill: RISK_META.low.color },
      ].filter((d) => d.value > 0)
    : [];

  return (
    <div className="stack gap-5">
      <header className="page-head">
        <div>
          <h1 className="page-title">{TIERS[tier].short} overview</h1>
          <p className="page-sub">
            {TIERS[tier].lens} · <span className="semibold">{scopeLabel}</span>
          </p>
        </div>
      </header>

      {stats.isError ? (
        <ErrorState error={stats.error} onRetry={() => stats.refetch()} />
      ) : (
        <>
          <div className="kpi-grid">
            <Kpi
              label="Works analysed"
              value={s?.corpus.works ?? 0}
              hint="Official MPLADS work records in the analysed corpus"
              icon={<Layers size={18} />}
              loading={stats.isLoading}
            />
            <Kpi
              label="Risk cases"
              value={s?.total ?? 0}
              hint="Cases raised by the agents within your authority scope"
              icon={<FileSearch size={18} />}
              onClick={() => goCases()}
              loading={stats.isLoading}
            />
            <Kpi
              label="High risk"
              value={s?.high ?? 0}
              hint="Composite risk score of 70 or above"
              icon={<ShieldAlert size={18} />}
              tone="danger"
              onClick={() => goCases({ min: "70" })}
              loading={stats.isLoading}
            />
            <Kpi
              label="Under review"
              value={s?.under_review ?? 0}
              icon={<ClipboardCheck size={18} />}
              tone="warn"
              onClick={() => goCases({ status: "under_review" })}
              loading={stats.isLoading}
            />
            <Kpi
              label="Reviewed / closed"
              value={s?.closed ?? 0}
              icon={<ClipboardCheck size={18} />}
              tone="success"
              onClick={() => goCases({ status: "confirmed,false_positive" })}
              loading={stats.isLoading}
            />
          </div>

          {s && s.total === 0 && (
            <Banner tone="neutral">
              No risk cases fall within <b>{scopeLabel}</b>. Choose a different
              scope from the authority menu, or switch to the Ministry view to
              see the national picture.
            </Banner>
          )}

          <div className="grid-2">
            {/* --------------------------------------------- geography */}
            {tier === "ministry" && (
              <Card
                title="States by high-risk cases"
                subtitle="Ranked by high-risk cases · click a bar to drill in"
              >
                {states.isLoading ? (
                  <Skeleton h={260} />
                ) : (
                  <ResponsiveContainer width="100%" height={280}>
                    <BarChart
                      data={[...(states.data ?? [])]
                        .sort((a, b) => b.high_risk - a.high_risk)
                        .slice(0, 10)}
                      layout="vertical"
                      margin={{ left: 8, right: 16, top: 4, bottom: 4 }}
                    >
                      <CartesianGrid
                        horizontal={false}
                        stroke="var(--chart-grid)"
                      />
                      <XAxis
                        type="number"
                        tick={{ fontSize: 11, fill: "var(--text-3)" }}
                        axisLine={false}
                        tickLine={false}
                      />
                      <YAxis
                        type="category"
                        dataKey="state"
                        width={124}
                        tick={{ fontSize: 11, fill: "var(--text-2)" }}
                        axisLine={false}
                        tickLine={false}
                      />
                      <Tooltip
                        cursor={{ fill: "var(--navy-50)" }}
                        contentStyle={tooltipStyle}
                        formatter={((v: unknown, _n: unknown, item: unknown) => [
                          `${v} high risk of ${
                            rowOf<StateRow>(item)?.flags ?? "?"
                          } cases`,
                          "State",
                        ]) as never}
                      />
                      <Bar
                        dataKey="high_risk"
                        name="High risk"
                        fill={RISK_META.high.color}
                        radius={[0, 3, 3, 0]}
                        cursor="pointer"
                        onClick={(d: unknown) => {
                          const st = barKey<string>(d, "state");
                          if (st) goCases({ state: st, min: "70" });
                        }}
                      />
                    </BarChart>
                  </ResponsiveContainer>
                )}
              </Card>
            )}

            {(tier === "state" || tier === "district" || tier === "mp") && (
              <Card
                title={
                  tier === "state"
                    ? "Districts by high-risk cases"
                    : "Risk distribution in your scope"
                }
                subtitle={
                  tier === "state"
                    ? "Cross-district comparison for your state"
                    : "Where your open cases sit on the risk scale"
                }
              >
                {tier === "state" ? (
                  districts.isLoading ? (
                    <Skeleton h={260} />
                  ) : (districts.data ?? []).length === 0 ? (
                    <Empty title="No district cases in this state" />
                  ) : (
                    <ResponsiveContainer width="100%" height={280}>
                      <BarChart
                        data={districts.data ?? []}
                        layout="vertical"
                        margin={{ left: 8, right: 16 }}
                      >
                        <CartesianGrid
                          horizontal={false}
                          stroke="var(--chart-grid)"
                        />
                        <XAxis
                          type="number"
                          tick={{ fontSize: 11, fill: "var(--text-3)" }}
                          axisLine={false}
                          tickLine={false}
                        />
                        <YAxis
                          type="category"
                          dataKey="district"
                          width={130}
                          tick={{ fontSize: 11, fill: "var(--text-2)" }}
                          axisLine={false}
                          tickLine={false}
                        />
                        <Tooltip
                          cursor={{ fill: "var(--navy-50)" }}
                          contentStyle={tooltipStyle}
                          formatter={((v: unknown, _n: unknown, item: unknown) => [
                            `${v} of ${
                              rowOf<DistrictRow>(item)?.flags ?? "?"
                            } cases`,
                            "High risk",
                          ]) as never}
                        />
                        <Bar
                          dataKey="flags"
                          name="Cases"
                          fill="var(--navy-300)"
                          radius={[0, 3, 3, 0]}
                          cursor="pointer"
                          onClick={(d: unknown) => {
                            const dis = barKey<string>(d, "district");
                            if (dis) goCases({ district: dis });
                          }}
                        />
                      </BarChart>
                    </ResponsiveContainer>
                  )
                ) : bandData.length === 0 ? (
                  <Empty title="No cases in scope" />
                ) : (
                  <ResponsiveContainer width="100%" height={280}>
                    <BarChart data={bandData} margin={{ left: 0, right: 8 }}>
                      <CartesianGrid vertical={false} stroke="var(--chart-grid)" />
                      <XAxis
                        dataKey="name"
                        tick={{ fontSize: 12, fill: "var(--text-2)" }}
                        axisLine={false}
                        tickLine={false}
                      />
                      <YAxis
                        tick={{ fontSize: 11, fill: "var(--text-3)" }}
                        axisLine={false}
                        tickLine={false}
                      />
                      <Tooltip
                        cursor={{ fill: "var(--navy-50)" }}
                        contentStyle={tooltipStyle}
                      />
                      <Bar dataKey="value" name="Cases" radius={[4, 4, 0, 0]}>
                        {bandData.map((d) => (
                          <Cell key={d.name} fill={d.fill} cursor="pointer" />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                )}
              </Card>
            )}

            {/* --------------------------------------------- detections */}
            <Card
              title="What is being detected"
              subtitle="Click a detection type to filter the case list"
            >
              {detections.isLoading ? (
                <Skeleton h={260} />
              ) : (detections.data ?? []).length === 0 ? (
                <Empty title="No detections in scope" />
              ) : (
                <div className="det-list">
                  {(detections.data ?? []).slice(0, 8).map((d) => {
                    const max = Math.max(
                      ...(detections.data ?? []).map((x) => x.count),
                      1,
                    );
                    return (
                      <button
                        key={d.rule_id}
                        className="det-row"
                        onClick={() => goCases({ rule: d.rule_id })}
                      >
                        <span className="det-name" title={d.title}>
                          {d.title}
                        </span>
                        <span className="det-bar">
                          <span
                            style={{ width: `${(d.count / max) * 100}%` }}
                          />
                        </span>
                        <span className="det-count num">{compact(d.count)}</span>
                      </button>
                    );
                  })}
                </div>
              )}
            </Card>
          </div>

          {/* --------------------------------------------- priority queue */}
          <Card
            title="Needs attention first"
            subtitle="Highest-scoring open cases within your authority"
            actions={
              <button className="btn btn-sm" onClick={() => goCases()}>
                View all cases <ArrowRight size={13} />
              </button>
            }
            tight
          >
            {topCases.isLoading ? (
              <Skeleton h={180} />
            ) : (topCases.data?.cases ?? []).length === 0 ? (
              <Empty
                icon={<AlertTriangle size={20} />}
                title="No elevated-risk cases in scope"
                hint="Nothing above the medium-risk threshold needs your attention right now."
              />
            ) : (
              <div className="queue">
                {topCases.data!.cases.map((c) => (
                  <button
                    key={c.flag_id}
                    className="queue-row"
                    onClick={() => nav(`/cases/${c.flag_id}`)}
                  >
                    <span
                      className="queue-score"
                      style={{
                        background:
                          RISK_META[
                            c.risk_score >= 70
                              ? "high"
                              : c.risk_score >= 40
                                ? "medium"
                                : "low"
                          ].bg,
                        color:
                          RISK_META[
                            c.risk_score >= 70
                              ? "high"
                              : c.risk_score >= 40
                                ? "medium"
                                : "low"
                          ].color,
                      }}
                    >
                      {c.risk_score.toFixed(0)}
                    </span>
                    <span className="queue-main">
                      <span className="queue-title truncate">
                        {c.display_title}
                      </span>
                      <span className="queue-meta">
                        {[c.district, c.state].filter(Boolean).join(" · ")}
                        {c.primary_signal ? ` — ${c.primary_signal}` : ""}
                      </span>
                    </span>
                    <ArrowRight size={14} className="dim" />
                  </button>
                ))}
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  );
}

/** Recharts tooltip items are loosely typed across versions; read the row safely. */
function rowOf<T>(item: unknown): T | undefined {
  if (!item || typeof item !== "object") return undefined;
  return (item as { payload?: T }).payload;
}

/** Recharts hands click payloads back loosely typed; read the datum safely. */
function barKey<T>(datum: unknown, key: string): T | undefined {
  if (!datum || typeof datum !== "object") return undefined;
  const d = datum as Record<string, unknown>;
  const direct = d[key];
  if (direct !== undefined) return direct as T;
  const payload = d.payload as Record<string, unknown> | undefined;
  return payload?.[key] as T | undefined;
}

export const tooltipStyle = {
  background: "var(--surface)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  fontSize: 12,
  boxShadow: "var(--shadow)",
  color: "var(--text)",
};
