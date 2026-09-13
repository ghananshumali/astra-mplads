import { useQuery } from "@tanstack/react-query";
import { ArrowRight, CheckCircle2, Database, Radio, Sparkles } from "lucide-react";
import type { ReactNode } from "react";

import { api } from "../api/client";
import type {
  DataSource as DataSourcePayload,
  Freshness,
  Parity,
  ParityTile,
  RecentUpdates,
} from "../api/types";
import { Banner, Card, Chip, ErrorState, Skeleton } from "../components/ui";
import { compact, formatDate, titleCase } from "../lib/format";
import { ago, clockTime, liveState, useNow } from "../lib/live";
import "./pages.css";

export default function DataSource() {
  const src = useQuery({
    queryKey: ["data-source"],
    queryFn: api.dataSource,
    refetchInterval: 60_000,
  });
  const llm = useQuery({ queryKey: ["llm"], queryFn: api.llm });
  const live = Boolean(src.data?.live);
  const fresh = useQuery({
    queryKey: ["freshness"],
    queryFn: api.freshness,
    enabled: live,
    refetchInterval: 20_000,
  });
  const updates = useQuery({
    queryKey: ["recent-updates"],
    queryFn: () => api.recentUpdates(8),
    enabled: live,
    refetchInterval: 30_000,
  });

  if (src.isError)
    return <ErrorState error={src.error} onRetry={() => src.refetch()} />;

  const batchFresh = src.data?.freshness_vs_live_portal;

  return (
    <div className="stack gap-5">
      <header className="page-head">
        <div>
          <h1 className="page-title">Data source & provenance</h1>
          <p className="page-sub">
            Where the analysed corpus comes from, and how current it is
          </p>
        </div>
      </header>

      {src.isLoading ? (
        <Skeleton h={120} />
      ) : (
        <>
          {live ? (
            <LiveKpis data={src.data} parity={fresh.data?.parity} />
          ) : (
            <BatchKpis data={src.data} />
          )}

          {live && (fresh.data ? <LiveSync f={fresh.data} /> : <Skeleton h={160} />)}

          {live && fresh.data?.parity && <PortalParity parity={fresh.data.parity} />}

          {live && <PortalUpdates data={updates.data} loading={updates.isLoading} />}

          {!live && batchFresh && (
            <Banner tone="success" icon={<CheckCircle2 size={15} />}>
              <b>Live check against mplads.mospi.gov.in</b> — this batch holds{" "}
              {compact(batchFresh.local_recommended_works)} recommended works; the
              official portal reported{" "}
              {compact(batchFresh.live_recommended_works)} when queried at{" "}
              {formatDate(batchFresh.checked_at)}. That is{" "}
              <b>{batchFresh.coverage_pct}% coverage</b> ({batchFresh.tenure}).
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
            title={live ? "Source ledger" : "Source ledger for this batch"}
            subtitle={
              live
                ? "Where each part of the corpus comes from. Work records are read directly from the eSAKSHI portal and kept current by the poller; fund positions before 2023 come from the OpenCity open-data portal"
                : "Dual-mode ingestion: live official interfaces are attempted under a strict time budget, with the official CSV exports supplying the corpus when live access is slow or partial"
            }
            tight
          >
            <div className="table-scroll" style={{ maxHeight: live ? "none" : 380 }}>
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
                        <LedgerStatus status={p.status} />
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
                {live
                  ? "Pre-2023 rows come from the OpenCity open-data records listed in the ledger; post-2023 rows are aggregated from the eSAKSHI work records. Keeping the eras apart is what makes era-separated baselines possible."
                  : "Pre-2023 rows come from the live open-data interfaces, which is what makes the era-separated baselines real rather than hypothetical."}
              </p>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ KPIs */
function Kpi({ icon, label, value }: { icon: ReactNode; label: string; value: ReactNode }) {
  return (
    <div className="kpi">
      <span className="kpi-icon" style={{ color: "var(--navy-600)", background: "#1a437f14" }}>
        {icon}
      </span>
      <span className="kpi-body">
        <span className="kpi-label">{label}</span>
        <span className="kpi-value" style={{ color: "var(--navy-600)" }}>
          {value}
        </span>
      </span>
    </div>
  );
}

function BatchKpis({ data }: { data?: DataSourcePayload }) {
  return (
    <div className="kpi-grid">
      <Kpi
        icon={<Database size={18} />}
        label="Resolved mode"
        value={(data?.mode_resolved ?? "—").toUpperCase()}
      />
      <Kpi icon={<Database size={18} />} label="Works ingested" value={compact(data?.works ?? 0)} />
      <Kpi
        icon={<Database size={18} />}
        label="Fund-flow records"
        value={compact(data?.fundflows ?? 0)}
      />
    </div>
  );
}

function LiveKpis({ data, parity }: { data?: DataSourcePayload; parity?: Parity }) {
  // The portal's headline figure is Works Recommended, so that is what is shown
  // here; works listed only from the sanctioned report onward are in the
  // parity table below.
  const recommended = (house: "LS" | "RS") =>
    parity?.national[house]?.recommended?.stored[0] ?? data?.houses?.[house] ?? 0;
  return (
    <div className="kpi-grid">
      <Kpi icon={<Radio size={18} />} label="Source · live portal" value="eSAKSHI" />
      <Kpi
        icon={<Database size={18} />}
        label="Lok Sabha works recommended"
        value={compact(recommended("LS"))}
      />
      <Kpi
        icon={<Database size={18} />}
        label="Rajya Sabha works recommended"
        value={compact(recommended("RS"))}
      />
      <Kpi
        icon={<Database size={18} />}
        label="Fund-flow records"
        value={compact(data?.fundflows ?? 0)}
      />
    </div>
  );
}

/* ------------------------------------------------------------- live sync */
const TONE_CHIP = {
  live: { color: "#0f5233", bg: "var(--success-bg)" },
  warn: { color: "var(--warn)", bg: "var(--warn-bg)" },
  stopped: { color: "var(--text-2)", bg: "var(--surface-3)" },
} as const;

function LiveSync({ f }: { f: Freshness }) {
  const now = useNow(10_000);
  const state = liveState(f, now);
  const every = Math.round(f.poll_interval_seconds ?? 60);
  const nightly = f.reconcile_at && f.reconcile_at !== "off" ? `nightly at ${f.reconcile_at}` : "nightly";

  return (
    <Card
      title="Live sync with the portal"
      subtitle={`The poller asks mplads.mospi.gov.in for its counts every ${every} s and re-reads only the areas whose counts moved; every area is also re-read record by record ${nightly}`}
      actions={
        <Chip color={TONE_CHIP[state.tone].color} bg={TONE_CHIP[state.tone].bg}>
          <span className={`live-dot ${state.tone}`} aria-hidden />
          {state.label} · {state.detail}
        </Chip>
      }
    >
      <div className="stack gap-3">
        {!f.poller_running && (
          <Banner tone="warn">
            <b>The poller is not running</b>, so changes on the portal are not being
            picked up and the figures here are as of the last check. Start it with{" "}
            <code>python -m astra.ingestion.poller</code>, or launch everything with{" "}
            <code>run_dev.ps1</code>.
          </Banner>
        )}
        {f.stale.length > 0 && (
          <Banner tone="warn">
            <b>
              {f.stale.length} {f.stale.length === 1 ? "area has" : "areas have"} failed
              repeatedly and {f.stale.length === 1 ? "is" : "are"} not updating:
            </b>{" "}
            {f.stale.map((s) => `${titleCase(s.place)} (${s.house})`).join(", ")}. Their
            last good records are kept and shown.
          </Banner>
        )}
        <dl className="kv-grid">
          <Kv
            label="Poller"
            value={f.poller_running ? "Running" : "Not running"}
            hint={f.poller_running ? `since ${clockTime(f.poller_since)}` : undefined}
          />
          <Kv
            label="Last portal check"
            value={clockTime(f.last_check_at)}
            hint={f.last_check_at ? ago(f.last_check_at, now) : "no check yet"}
          />
          <Kv
            label="Last update stored"
            value={f.last_update ? areaLabel(f.last_update.area) : "None yet"}
            hint={f.last_update ? `${clockTime(f.last_update.at)} · ${ago(f.last_update.at, now)}` : undefined}
          />
          <Kv
            label="Last full reconciliation"
            value={clockTime(f.last_full_reconciliation)}
            hint={
              f.sweep_in_progress
                ? `another running since ${clockTime(f.sweep_started_at)}`
                : f.reconciliation_overdue
                  ? "overdue"
                  : `every area re-read ${nightly}`
            }
          />
          <Kv
            label="Areas matching the portal on every figure"
            value={
              f.parity
                ? `${compact(f.parity.exact_slices)} of ${compact(f.parity.registered_slices)}`
                : `${compact(f.reconciled_shards)} of ${compact(f.registered_shards)}`
            }
            hint="counts and rupees, all four portal figures"
          />
          <Kv
            label="Quarantined areas"
            value={compact(f.quarantined)}
            hint={f.quarantined ? "held back until they pass validation" : "none held back"}
          />
          {f.parity && (
            <Kv
              label="Works removed from the portal"
              value={compact(f.parity.removed_from_portal)}
              hint={
                f.parity.awaiting_removal
                  ? `${compact(f.parity.awaiting_removal)} no longer listed, confirming`
                  : "kept in history; restored if listed again"
              }
            />
          )}
        </dl>
      </div>
    </Card>
  );
}

function Kv({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="kv">
      <dt className="kv-label">{label}</dt>
      <dd className="kv-value">
        {value}
        {hint && <span className="kv-hint">{hint}</span>}
      </dd>
    </div>
  );
}

/* ---------------------------------------------------------- portal parity */
const TILE_LABEL: Record<ParityTile, string> = {
  recommended: "Works recommended",
  sanctioned: "Works sanctioned",
  completed: "Works completed",
  expenditure: "Expenditure on works",
};

function rupeesExact(value: number | null): string {
  if (value === null) return "—";
  return `₹${value.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function PortalParity({ parity }: { parity: Parity }) {
  const rows = (["LS", "RS"] as const).flatMap((house) =>
    (Object.keys(TILE_LABEL) as ParityTile[])
      .filter((tile) => parity.national[house]?.[tile])
      .map((tile) => ({ house, tile, fig: parity.national[house]![tile]! })),
  );
  const allExact = parity.exception_count === 0 && rows.every((r) => r.fig.exact);
  return (
    <Card
      title="Portal parity"
      subtitle="The portal's own dashboard figures beside ASTRA's, rebuilt from the stored records after every update and checked area by area"
      actions={
        <Chip
          color={allExact ? "#0f5233" : "var(--warn)"}
          bg={allExact ? "var(--success-bg)" : "var(--warn-bg)"}
        >
          {allExact
            ? `Exact in ${compact(parity.exact_slices)} of ${compact(parity.registered_slices)} areas`
            : `${compact(parity.exception_count)} of ${compact(parity.registered_slices)} areas differ`}
        </Chip>
      }
      tight
    >
      {parity.exceptions.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <Banner tone="warn">
            <b>Differences from the portal:</b>{" "}
            {parity.exceptions
              .map(
                (e) =>
                  `${titleCase(e.place)} (${e.house}): ${e.differences
                    .map((d) => `${TILE_LABEL[d.tile].toLowerCase()} ${d.measure}`)
                    .join(", ")}`,
              )
              .join("; ")}
            . Each is re-read automatically.
          </Banner>
        </div>
      )}
      <div className="table-scroll" style={{ maxHeight: "none" }}>
        <table className="table">
          <thead>
            <tr>
              <th>Figure</th>
              <th style={{ width: 96 }}>Portal</th>
              <th style={{ width: 96 }}>ASTRA</th>
              <th>Portal ₹</th>
              <th>ASTRA ₹</th>
              <th style={{ width: 80 }}>Match</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ house, tile, fig }) => (
              <tr key={`${house}-${tile}`} style={{ cursor: "default" }}>
                <td className="text-sm">
                  <span className="semibold">{TILE_LABEL[tile]}</span>{" "}
                  <span className="dim">· {house === "LS" ? "Lok Sabha" : "Rajya Sabha"}</span>
                </td>
                <td className="num text-sm">{compact(fig.portal[0])}</td>
                <td className="num text-sm">{compact(fig.stored[0])}</td>
                <td className="num text-xs">{rupeesExact(fig.portal[1])}</td>
                <td className="num text-xs">{rupeesExact(fig.stored[1])}</td>
                <td>
                  <LedgerStatus status={fig.exact ? "exact" : "differs"} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-xs dim" style={{ margin: "10px 0 0" }}>
        Works listed only from the sanctioned report onward are stored and counted
        under sanctioned, completed and expenditure, exactly as the portal counts
        them. A work the portal stops listing is removed after a second read
        confirms it, and kept in history.
        {parity.duplicate_listings > 0 &&
          ` The portal lists ${compact(parity.duplicate_listings)} work(s) more than once; they are mirrored as listed.`}
      </p>
    </Card>
  );
}

/* ----------------------------------------------------------- change feed */
/** "KANGRA (LS)" -> "Kangra · Lok Sabha"; "RAJASTHAN (RS)" -> "Rajasthan · Rajya Sabha". */
function areaLabel(area: string): string {
  const m = area.match(/^(.*?)\s*\((LS|RS)\)$/);
  if (!m) return titleCase(area);
  return `${titleCase(m[1])} · ${m[2] === "RS" ? "Rajya Sabha" : "Lok Sabha"}`;
}

const FIELD_LABEL: Record<string, string> = {
  expenditure: "Expenditure",
  total_paid: "Total paid",
  payment_count: "Payments",
  last_payment_date: "Last payment",
  payment_status: "Payment status",
  completion_date: "Completed on",
  sanction_date: "Sanctioned on",
  recommended_date: "Recommended on",
  estimated_cost: "Estimated cost",
  sanctioned_amount: "Sanctioned amount",
  status: "Status",
  vendor_name: "Vendor",
  ia_name: "Implementing agency",
  description: "Description",
  category: "Category",
  work_type: "Work type",
  district: "District",
  fy: "Financial year",
  listing: "Listed on the portal",
};
const MONEY_FIELDS = new Set(["expenditure", "total_paid", "estimated_cost", "sanctioned_amount"]);
const DATE_FIELDS = new Set(["recommended_date", "sanction_date", "completion_date", "last_payment_date"]);

function fieldValue(field: string, value: string | null): string {
  if (value === null || value === "") return "—";
  if (MONEY_FIELDS.has(field)) {
    const n = Number(value);
    return Number.isNaN(n) ? value : `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
  }
  if (DATE_FIELDS.has(field)) return formatDate(value);
  return value.length > 60 ? `${value.slice(0, 57)}…` : value;
}

function PortalUpdates({ data, loading }: { data?: RecentUpdates; loading: boolean }) {
  const now = useNow(10_000);
  if (loading) return <Skeleton h={180} />;
  const stores = data?.stores ?? [];
  const changes = data?.changes ?? [];

  return (
    <div className="grid-2">
      <Card
        title="Latest portal updates"
        subtitle="Areas re-read because their records changed on the portal, newest first. New works appear here"
        tight
      >
        {stores.length ? (
          <div className="table-scroll" style={{ maxHeight: 360 }}>
            <table className="table">
              <thead>
                <tr>
                  <th>Area</th>
                  <th>What changed</th>
                  <th style={{ width: 92 }}>When</th>
                </tr>
              </thead>
              <tbody>
                {stores.map((s, i) => {
                  // "2 new, 1 updated (3 field changes); portal count 628; shard 2:15:144"
                  const parts = s.detail.split(";").map((p) => p.trim());
                  const what = parts.find((p) => !/^(shard|portal count)\b/.test(p)) ?? parts[0];
                  return (
                    <tr
                      key={i}
                      style={{ cursor: "default" }}
                      title={parts.filter((p) => p !== what).join(" · ")}
                    >
                      <td className="semibold text-sm">{areaLabel(s.area)}</td>
                      <td className="text-xs muted">{what}</td>
                      <td className="text-xs dim" title={clockTime(s.at)}>
                        {ago(s.at, now)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm dim">No updates stored yet.</p>
        )}
      </Card>

      <Card
        title="Recently updated works"
        subtitle="Changes to existing works, old and new value exactly as observed on the portal"
        tight
      >
        {changes.length ? (
          <div className="change-list">
            {changes.map((c) => (
              <div className="change-item" key={`${c.work_id}|${c.observed_at}`}>
                <div className="row gap-2">
                  <span className="mono text-xs grow truncate" title={c.work_id}>
                    {c.work_id}
                  </span>
                  <span className="text-xs dim" title={clockTime(c.observed_at)}>
                    {ago(c.observed_at, now)}
                  </span>
                </div>
                <div className="text-sm semibold">
                  {titleCase(c.place)}
                  {c.house === "RS" ? " · Rajya Sabha" : ""}
                  {c.mp_name ? <span className="muted"> · {titleCase(c.mp_name)}</span> : null}
                </div>
                {c.description && (
                  <div className="text-xs muted truncate" title={c.description}>
                    {c.description}
                  </div>
                )}
                <ul className="change-fields">
                  {c.fields.map((fd) => (
                    <li key={fd.field}>
                      <span className="change-field">{FIELD_LABEL[fd.field] ?? fd.field}</span>
                      <span className={fd.old_value === null ? "change-empty" : "change-old"}>
                        {fieldValue(fd.field, fd.old_value)}
                      </span>
                      <ArrowRight size={11} className="dim" aria-label="changed to" />
                      <span className="change-new">{fieldValue(fd.field, fd.new_value)}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm dim">
            No edits to existing works observed yet. Newly recommended works appear
            under Latest portal updates.
          </p>
        )}
      </Card>
    </div>
  );
}

/* ---------------------------------------------------------------- ledger */
function LedgerStatus({ status }: { status: string }) {
  const good = status === "ok" || status === "selected" || status === "exact";
  const attention = status === "attention" || status === "differs";
  return (
    <Chip
      color={good ? "#0f5233" : attention ? "var(--warn)" : "var(--text-2)"}
      bg={good ? "var(--success-bg)" : attention ? "var(--warn-bg)" : "var(--surface-3)"}
    >
      {status}
    </Chip>
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
