import { useQuery } from "@tanstack/react-query";
import { ArrowRight, CheckCircle2, Database, Radio, Sparkles } from "lucide-react";
import type { ReactNode } from "react";

import { api } from "../api/client";
import type {
  DataSource as DataSourcePayload,
  Freshness,
  OpsStatus,
  Parity,
  ParityTile,
  RecentUpdates,
} from "../api/types";
import { Banner, Card, Chip, ErrorState, Skeleton } from "../components/ui";
import { useI18n } from "../i18n/context";
import type { I18n } from "../i18n/context";
import { houseName } from "../i18n/labels";
import { T } from "../i18n/T";
import { compact, titleCase } from "../lib/format";
import { analysisHint, failingChecks, liveState, rotationHours, staleLabel, useNow } from "../lib/live";
import "./pages.css";

export default function DataSource() {
  const { t, fmt } = useI18n();
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
  const ops = useQuery({
    queryKey: ["ops"],
    queryFn: api.ops,
    refetchInterval: 60_000,
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
          <h1 className="page-title">{t("data.title")}</h1>
          <p className="page-sub">{t("data.sub")}</p>
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

          {ops.data && <Unattended ops={ops.data} />}

          {live && fresh.data?.parity && <PortalParity parity={fresh.data.parity} />}

          {live && <PortalUpdates data={updates.data} loading={updates.isLoading} />}

          {!live && batchFresh && (
            <Banner tone="success" icon={<CheckCircle2 size={15} />}>
              <T
                k="data.batch.check"
                values={{
                  title: <b>{t("data.batch.checkTitle")}</b>,
                  local: compact(batchFresh.local_recommended_works),
                  live: compact(batchFresh.live_recommended_works),
                  date: fmt.date(batchFresh.checked_at),
                  coverage: <b>{t("data.batch.coverage", { pct: batchFresh.coverage_pct })}</b>,
                  tenure: batchFresh.tenure,
                }}
              />
            </Banner>
          )}

          {llm.data && (
            <Card title={t("data.llm.title")} tight>
              <div className="row gap-3" style={{ flexWrap: "wrap" }}>
                <Chip
                  color={llm.data.configured ? "#0f5233" : "var(--text-2)"}
                  bg={llm.data.configured ? "var(--success-bg)" : "var(--surface-3)"}
                  dot
                >
                  <Sparkles size={11} />
                  {llm.data.configured ? t("data.llm.configured") : t("data.llm.notConfigured")}
                </Chip>
                <span className="text-sm muted">
                  <T
                    k="data.llm.provider"
                    values={{
                      provider: <b>{llm.data.provider}</b>,
                      model: <b className="mono">{llm.data.model}</b>,
                    }}
                  />
                  {llm.data.key_hint ? ` · ${t("data.llm.key", { hint: llm.data.key_hint })}` : ""}
                </span>
              </div>
              {!llm.data.configured && (
                <p className="text-sm dim" style={{ margin: "10px 0 0" }}>
                  <T
                    k="data.llm.noKey"
                    values={{ key: <code>GROQ_API_KEY</code>, env: <code>.env</code> }}
                  />
                </p>
              )}
            </Card>
          )}

          <Card
            title={live ? t("data.ledger.titleLive") : t("data.ledger.titleBatch")}
            subtitle={live ? t("data.ledger.subLive") : t("data.ledger.subBatch")}
            tight
          >
            <div className="table-scroll" style={{ maxHeight: live ? "none" : 380 }}>
              <table className="table">
                <thead>
                  <tr>
                    <th>{t("data.col.source")}</th>
                    <th style={{ width: 88 }}>{t("data.col.mode")}</th>
                    <th style={{ width: 92 }}>{t("data.col.rows")}</th>
                    <th style={{ width: 108 }}>{t("data.col.status")}</th>
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
            <Card title={t("data.eras.works")} tight>
              <EraList data={src.data?.work_eras} />
            </Card>
            <Card title={t("data.eras.flows")} tight>
              <EraList data={src.data?.fundflow_eras} />
              <p className="text-xs dim" style={{ margin: "10px 0 0" }}>
                {live ? t("data.eras.noteLive") : t("data.eras.noteBatch")}
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
  const { t } = useI18n();
  return (
    <div className="kpi-grid">
      <Kpi
        icon={<Database size={18} />}
        label={t("data.kpi.mode")}
        value={(data?.mode_resolved ?? "—").toUpperCase()}
      />
      <Kpi
        icon={<Database size={18} />}
        label={t("data.kpi.worksIngested")}
        value={compact(data?.works ?? 0)}
      />
      <Kpi
        icon={<Database size={18} />}
        label={t("data.kpi.fundflows")}
        value={compact(data?.fundflows ?? 0)}
      />
    </div>
  );
}

function LiveKpis({ data, parity }: { data?: DataSourcePayload; parity?: Parity }) {
  const { t } = useI18n();
  // The portal's headline figure is Works Recommended, so that is what is shown
  // here; works listed only from the sanctioned report onward are in the
  // parity table below.
  const recommended = (house: "LS" | "RS") =>
    parity?.national[house]?.recommended?.stored[0] ?? data?.houses?.[house] ?? 0;
  return (
    <div className="kpi-grid">
      <Kpi icon={<Radio size={18} />} label={t("data.kpi.source")} value="eSAKSHI" />
      <Kpi
        icon={<Database size={18} />}
        label={t("data.kpi.lsWorks")}
        value={compact(recommended("LS"))}
      />
      <Kpi
        icon={<Database size={18} />}
        label={t("data.kpi.rsWorks")}
        value={compact(recommended("RS"))}
      />
      <Kpi
        icon={<Database size={18} />}
        label={t("data.kpi.fundflows")}
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
  const i18n = useI18n();
  const { t, tn, fmt } = i18n;
  const now = useNow(10_000);
  const state = liveState(f, i18n, now);
  const failing = failingChecks(f);
  const every = Math.round(f.poll_interval_seconds ?? 60);
  const nightly =
    f.reconcile_at && f.reconcile_at !== "off"
      ? t("sync.nightlyAt", { time: f.reconcile_at })
      : t("sync.nightly");
  const rolling = f.rolling?.enabled ? f.rolling : null;
  const nOfM = (n: number, m: number) => t("sync.nOfM", { n: compact(n), m: compact(m) });

  return (
    <Card
      title={t("sync.title")}
      subtitle={
        rolling
          ? t("sync.subRotation", { every, hours: rotationHours(rolling.hours, i18n), nightly })
          : t("sync.sub", { every, nightly })
      }
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
            <T
              k="sync.notRunning"
              values={{
                bold: <b>{t("sync.notRunningBold")}</b>,
                cmd: <code>python -m astra.ingestion.poller</code>,
                script: <code>run_dev.ps1</code>,
              }}
            />
          </Banner>
        )}
        {f.poller_running && f.portal?.open && (
          <Banner tone="warn">
            <b>{t("sync.portalDownBold")}</b>{" "}
            {t("sync.portalDown", {
              since: fmt.clock(f.portal.failing_since),
              next: fmt.clock(f.portal.next_attempt_at),
            })}
            {f.portal.last_error && (
              <div className="text-sm" style={{ marginTop: 4 }}>
                {t("sync.portalError", { time: fmt.clock(f.portal.last_error_at) })}{" "}
                <code>{f.portal.last_error}</code>
              </div>
            )}
          </Banner>
        )}
        {failing.length > 0 && (
          <Banner tone="warn">
            <b>{tn("sync.stale", failing.length)}</b>{" "}
            {failing.map((s) => staleLabel(s, i18n)).join(", ")}.{" "}
            {t("sync.staleTail")}
          </Banner>
        )}
        <dl className="kv-grid">
          <Kv
            label={t("sync.poller")}
            value={f.poller_running ? t("sync.running") : t("sync.notRunningShort")}
            hint={f.poller_running ? t("sync.since", { time: fmt.clock(f.poller_since) }) : undefined}
          />
          <Kv
            label={t("sync.lastCheck")}
            value={fmt.clock(f.last_check_at)}
            hint={f.last_check_at ? fmt.ago(f.last_check_at, now) : t("sync.noCheck")}
          />
          <Kv
            label={t("sync.lastUpdate")}
            value={f.last_update ? areaLabel(f.last_update.area, i18n) : t("sync.noneYet")}
            hint={
              f.last_update
                ? `${fmt.clock(f.last_update.at)} · ${fmt.ago(f.last_update.at, now)}`
                : undefined
            }
          />
          <Kv
            label={t("sync.lastRecon")}
            value={fmt.clock(f.last_full_reconciliation)}
            hint={
              f.sweep_in_progress
                ? t("sync.anotherRunning", { time: fmt.clock(f.sweep_started_at) })
                : f.reconciliation_overdue
                  ? t("sync.overdue")
                  : t("sync.everyArea", { nightly })
            }
          />
          {f.rolling && (
            <Kv
              label={t("sync.rotating")}
              value={
                !f.rolling.enabled
                  ? t("sync.off")
                  : f.rolling.active_now
                    ? t("sync.running")
                    : f.poller_running
                      ? t("sync.paused")
                      : t("sync.notRunningShort")
              }
              hint={
                f.rolling.enabled
                  ? [
                      f.rolling.active_now
                        ? null
                        : t("sync.runsHours", { hours: rotationHours(f.rolling.hours, i18n) }),
                      t("sync.oldestRead", { ago: fmt.ago(f.oldest_shard_fetch, now) }),
                    ]
                      .filter(Boolean)
                      .join(" · ")
                  : t("sync.rotationOff")
              }
            />
          )}
          <Kv
            label={t("sync.recomputed")}
            value={f.analysis.in_progress ? t("sync.recomputingNow") : fmt.clock(f.analysis.last_at)}
            hint={analysisHint(f, i18n, now)}
          />
          {f.photos && (
            <Kv
              label={t("sync.photos")}
              value={
                !f.photos.enabled
                  ? t("sync.off")
                  : f.photos.active_now
                    ? t("sync.running")
                    : f.poller_running
                      ? t("sync.paused")
                      : t("sync.notRunningShort")
              }
              hint={
                f.photos.enabled
                  ? [
                      t("sync.photosChecked", {
                        n: compact(f.photos.checked),
                        m: compact(f.photos.held_works),
                      }),
                      f.photos.active_now
                        ? null
                        : t("sync.runsHours", { hours: rotationHours(f.photos.hours, i18n) }),
                      f.photos.last_at ? t("sync.photosLast", { time: fmt.clock(f.photos.last_at) }) : null,
                    ]
                      .filter(Boolean)
                      .join(" · ")
                  : t("sync.photosOff")
              }
            />
          )}
          <Kv
            label={t("sync.areasMatching")}
            value={
              f.parity
                ? nOfM(f.parity.exact_slices, f.parity.registered_slices)
                : nOfM(f.reconciled_shards, f.registered_shards)
            }
            hint={t("sync.areasMatchingHint")}
          />
          <Kv
            label={t("sync.quarantined")}
            value={compact(f.quarantined)}
            hint={f.quarantined ? t("sync.quarantinedHint") : t("sync.noneHeld")}
          />
          {f.parity && (
            <Kv
              label={t("sync.removed")}
              value={compact(f.parity.removed_from_portal)}
              hint={
                f.parity.awaiting_removal
                  ? t("sync.confirming", { count: compact(f.parity.awaiting_removal) })
                  : t("sync.keptHistory")
              }
            />
          )}
        </dl>
      </div>
    </Card>
  );
}

/* ------------------------------------------------------- unattended running */
function Unattended({ ops }: { ops: OpsStatus }) {
  const { t, tn, fmt } = useI18n();
  const open = ops.conditions.filter((c) => c.notified_at);
  const tone = !ops.supervisor_running ? "stopped" : open.length ? "warn" : "live";
  const services = Object.entries(ops.services ?? {});
  const backup = ops.backup;
  return (
    <Card
      title={t("ops.title")}
      subtitle={t("ops.sub")}
      actions={
        <Chip color={TONE_CHIP[tone].color} bg={TONE_CHIP[tone].bg}>
          <span className={`live-dot ${tone}`} aria-hidden />
          {!ops.supervisor_running
            ? t("ops.notRunning")
            : open.length
              ? tn("ops.problems", open.length)
              : t("ops.allWell")}
        </Chip>
      }
    >
      <div className="stack gap-3">
        {!ops.supervisor_running && (
          <p className="text-sm muted" style={{ margin: 0 }}>
            <T
              k="ops.howTo"
              values={{
                cmd: <code>python -m astra.ops.supervisor</code>,
                script: <code>scripts\install_autostart.ps1</code>,
              }}
            />
          </p>
        )}
        {open.map((c) => (
          <Banner key={c.key} tone="warn">
            <b>{c.title}.</b> {c.detail}{" "}
            <span className="dim">{t("ops.since", { since: fmt.ago(c.first_seen) })}</span>
          </Banner>
        ))}
        <dl className="kv-grid">
          {ops.supervisor_running && (
            <Kv
              label={t("ops.processes")}
              value={
                services.length
                  ? services
                      .map(([name, s]) => {
                        const label =
                          name === "api"
                            ? t("ops.svc.api")
                            : name === "poller"
                              ? t("ops.svc.poller")
                              : name === "web"
                                ? t("ops.svc.web")
                                : name;
                        return s.running && s.healthy !== false
                          ? t("ops.svcUp", { name: label })
                          : t("ops.svcDown", { name: label });
                      })
                      .join(" · ")
                  : "—"
              }
              hint={
                services.some(([, s]) => s.restarts_last_hour)
                  ? tn(
                      "ops.restarts",
                      services.reduce((n, [, s]) => n + s.restarts_last_hour, 0),
                    )
                  : undefined
              }
            />
          )}
          <Kv
            label={t("ops.backup")}
            value={backup.newest ? fmt.ago(backup.newest.taken_at) : t("ops.noBackup")}
            hint={
              backup.newest
                ? tn("ops.kept", backup.kept, {
                    size: `${(backup.newest.bytes / 1024 ** 3).toFixed(1)} GB`,
                  })
                : undefined
            }
          />
          {backup.last_error &&
            (!backup.last_ok_at || (backup.last_error_at ?? "") > backup.last_ok_at) && (
              <Kv label={t("ops.backupFailed")} value={backup.last_error} />
            )}
          <Kv
            label={t("ops.lastAlert")}
            value={ops.last_alert ? ops.last_alert.title : t("ops.noAlerts")}
            hint={ops.last_alert ? fmt.ago(ops.last_alert.at) : undefined}
          />
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
const TILES: ParityTile[] = ["recommended", "sanctioned", "completed", "expenditure"];

function rupeesExact(value: number | null): string {
  if (value === null) return "—";
  return `₹${value.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function PortalParity({ parity }: { parity: Parity }) {
  const i18n = useI18n();
  const { t, tOr } = i18n;
  const rows = (["LS", "RS"] as const).flatMap((house) =>
    TILES.filter((tile) => parity.national[house]?.[tile]).map((tile) => ({
      house,
      tile,
      fig: parity.national[house]![tile]!,
    })),
  );
  const allExact = parity.exception_count === 0 && rows.every((r) => r.fig.exact);
  const counts = { n: compact(parity.exact_slices), m: compact(parity.registered_slices) };
  const checkedAt = parity.national_checked_at?.LS ?? parity.national_checked_at?.RS;
  return (
    <Card
      title={t("parity.title")}
      subtitle={t("parity.sub")}
      actions={
        <Chip
          color={allExact ? "#0f5233" : "var(--warn)"}
          bg={allExact ? "var(--success-bg)" : "var(--warn-bg)"}
        >
          {allExact
            ? t("parity.exact", counts)
            : parity.exception_count
              ? t("parity.differ", { n: compact(parity.exception_count), m: counts.m })
              : t("parity.nationalDiffers")}
        </Chip>
      }
      tight
    >
      {parity.exceptions.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <Banner tone="warn">
            <b>{t("parity.diffs")}</b>{" "}
            {parity.exceptions
              .map(
                (e) =>
                  `${titleCase(e.place)} (${e.house}): ${e.differences
                    .map((d) => `${t(`tile.${d.tile}`)} · ${tOr(`measure.${d.measure}`, d.measure)}`)
                    .join(", ")}`,
              )
              .join("; ")}
            . {t("parity.reread")}
          </Banner>
        </div>
      )}
      <div className="table-scroll" style={{ maxHeight: "none" }}>
        <table className="table">
          <thead>
            <tr>
              <th>{t("parity.col.figure")}</th>
              <th style={{ width: 96 }}>{t("parity.col.portal")}</th>
              <th style={{ width: 96 }}>{t("parity.col.astra")}</th>
              <th>{t("parity.col.portalRs")}</th>
              <th>{t("parity.col.astraRs")}</th>
              <th style={{ width: 80 }}>{t("parity.col.match")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ house, tile, fig }) => (
              <tr key={`${house}-${tile}`} style={{ cursor: "default" }}>
                <td className="text-sm">
                  <span className="semibold">{t(`tile.${tile}`)}</span>{" "}
                  <span className="dim">· {houseName(i18n, house)}</span>
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
        {checkedAt && `${t("parity.liveNote", { time: i18n.fmt.clock(checkedAt) })} `}
        {t("parity.note")}
        {parity.duplicate_listings > 0 &&
          ` ${t("parity.dupNote", { count: compact(parity.duplicate_listings) })}`}
      </p>
    </Card>
  );
}

/* ----------------------------------------------------------- change feed */
/** "KANGRA (LS)" -> "Kangra · Lok Sabha"; "RAJASTHAN (RS)" -> "Rajasthan · Rajya Sabha". */
function areaLabel(area: string, i18n: I18n): string {
  const m = area.match(/^(.*?)\s*\((LS|RS)\)$/);
  if (!m) return titleCase(area);
  return `${titleCase(m[1])} · ${houseName(i18n, m[2])}`;
}

const MONEY_FIELDS = new Set(["expenditure", "total_paid", "estimated_cost", "sanctioned_amount"]);
const DATE_FIELDS = new Set(["recommended_date", "sanction_date", "completion_date", "last_payment_date"]);

function fieldValue(field: string, value: string | null, i18n: I18n): string {
  if (value === null || value === "") return "—";
  if (MONEY_FIELDS.has(field)) {
    const n = Number(value);
    return Number.isNaN(n) ? value : `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
  }
  if (DATE_FIELDS.has(field)) return i18n.fmt.date(value);
  return value.length > 60 ? `${value.slice(0, 57)}…` : value;
}

/** The poller's "2 new, 1 updated (3 field changes)" in the chosen language;
 *  any other wording is shown as the poller wrote it. */
function changeSummary(what: string, i18n: I18n): string {
  const m = /^(\d+) new, (\d+) updated,? \(?(\d+) field changes?\)?$/.exec(what);
  if (!m) return what;
  return i18n.tn("updates.what", Number(m[3]), {
    new: Number(m[1]).toLocaleString("en-IN"),
    updated: Number(m[2]).toLocaleString("en-IN"),
  });
}

function PortalUpdates({ data, loading }: { data?: RecentUpdates; loading: boolean }) {
  const i18n = useI18n();
  const { t, tOr, fmt } = i18n;
  const now = useNow(10_000);
  if (loading) return <Skeleton h={180} />;
  const stores = data?.stores ?? [];
  const changes = data?.changes ?? [];

  return (
    <div className="grid-2">
      <Card title={t("updates.title")} subtitle={t("updates.sub")} tight>
        {stores.length ? (
          <div className="table-scroll" style={{ maxHeight: 360 }}>
            <table className="table">
              <thead>
                <tr>
                  <th>{t("updates.col.area")}</th>
                  <th>{t("updates.col.what")}</th>
                  <th style={{ width: 92 }}>{t("updates.col.when")}</th>
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
                      <td className="semibold text-sm">{areaLabel(s.area, i18n)}</td>
                      <td className="text-xs muted">{changeSummary(what, i18n)}</td>
                      <td className="text-xs dim" title={fmt.clock(s.at)}>
                        {fmt.ago(s.at, now)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm dim">{t("updates.none")}</p>
        )}
      </Card>

      <Card title={t("updates.works.title")} subtitle={t("updates.works.sub")} tight>
        {changes.length ? (
          <div className="change-list">
            {changes.map((c) => (
              <div className="change-item" key={`${c.work_id}|${c.observed_at}`}>
                <div className="row gap-2">
                  <span className="mono text-xs grow truncate" title={c.work_id}>
                    {c.work_id}
                  </span>
                  <span className="text-xs dim" title={fmt.clock(c.observed_at)}>
                    {fmt.ago(c.observed_at, now)}
                  </span>
                </div>
                <div className="text-sm semibold">
                  {titleCase(c.place)}
                  {c.house === "RS" ? ` · ${houseName(i18n, "RS")}` : ""}
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
                      <span className="change-field">{tOr(`field.${fd.field}`, fd.field)}</span>
                      <span className={fd.old_value === null ? "change-empty" : "change-old"}>
                        {fieldValue(fd.field, fd.old_value, i18n)}
                      </span>
                      <ArrowRight
                        size={11}
                        className="dim flip-rtl"
                        aria-label={t("updates.changedTo")}
                      />
                      <span className="change-new">{fieldValue(fd.field, fd.new_value, i18n)}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm dim">{t("updates.works.none")}</p>
        )}
      </Card>
    </div>
  );
}

/* ---------------------------------------------------------------- ledger */
function LedgerStatus({ status }: { status: string }) {
  const { tOr } = useI18n();
  const good = status === "ok" || status === "selected" || status === "exact";
  const attention = status === "attention" || status === "differs";
  return (
    <Chip
      color={good ? "#0f5233" : attention ? "var(--warn)" : "var(--text-2)"}
      bg={good ? "var(--success-bg)" : attention ? "var(--warn-bg)" : "var(--surface-3)"}
    >
      {tOr(`ledger.${status}`, status)}
    </Chip>
  );
}

function EraList({ data }: { data?: Record<string, number> }) {
  const { t } = useI18n();
  const entries = Object.entries(data ?? {});
  if (!entries.length) return <p className="text-sm dim">{t("data.noData")}</p>;
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
          <span className="num text-sm semibold" style={{ width: 66, textAlign: "end" }}>
            {compact(n)}
          </span>
        </div>
      ))}
    </div>
  );
}
