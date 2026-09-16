/** Live-sync status, shared by the header badge and the Data source page.
 *  Presentation only: every fact comes from GET /meta/freshness. */

import { useEffect, useState } from "react";

import type { Freshness } from "../api/types";
import type { I18n } from "../i18n/context";
import { houseName } from "../i18n/labels";
import { titleCase } from "./format";

type StaleCheck = Freshness["stale"][number];

/** "National check — Lok Sabha", "Goa state check (Lok Sabha)", "Kollam (LS)". */
export function staleLabel(s: StaleCheck, i18n: I18n): string {
  const house = houseName(i18n, s.house);
  if (s.scope === "national") return i18n.t("sync.nationalCheck", { house });
  if (s.scope === "state") return i18n.t("sync.stateCheck", { state: titleCase(s.place), house });
  return `${titleCase(s.place)} (${s.house})`;
}

/** Checks to name as failing. While the portal is not answering, the national
 *  checks are explained by that instead, so they are not listed twice. */
export function failingChecks(f: Freshness): StaleCheck[] {
  return f.stale.filter((s) => !(f.portal?.open && s.scope === "national"));
}

/** Re-render on an interval so "checked 20 s ago" keeps counting between fetches. */
export function useNow(everyMs = 10_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), everyMs);
    return () => window.clearInterval(id);
  }, [everyMs]);
  return now;
}

export type LiveTone = "live" | "warn" | "stopped";

export interface LiveState {
  tone: LiveTone;
  /** Short label for the badge: "Live" or "Updates paused". */
  label: string;
  /** Qualifier: "checked 20 s ago", "full check running", ... */
  detail: string;
}

/** A heartbeat older than this many intervals means the poller is stuck
 *  (portal unreachable, or a long catch-up after the laptop slept). */
const STUCK_AFTER_INTERVALS = 5;

export function liveState(f: Freshness, i18n: I18n, now = Date.now()): LiveState {
  const { t, tn, fmt } = i18n;
  if (!f.poller_running) {
    return {
      tone: "stopped",
      label: t("live.paused"),
      detail: f.last_check_at
        ? t("live.lastCheck", { ago: fmt.ago(f.last_check_at, now) })
        : t("live.notStarted"),
    };
  }
  if (f.portal?.open) {
    return { tone: "warn", label: t("live.paused"), detail: t("live.portalDown") };
  }
  const live = t("live.live");
  if (f.sweep_in_progress) {
    return { tone: "live", label: live, detail: t("live.fullCheck") };
  }
  if (f.analysis?.in_progress) {
    return { tone: "live", label: live, detail: t("live.recomputing") };
  }
  if (f.parity?.exception_count) {
    return { tone: "warn", label: live, detail: tn("live.differ", f.parity.exception_count) };
  }
  const failing = failingChecks(f);
  if (failing.length) {
    return { tone: "warn", label: live, detail: tn("live.notUpdating", failing.length) };
  }
  if (f.reconciliation_overdue) {
    return { tone: "warn", label: live, detail: t("live.nightlyOverdue") };
  }
  if (!f.last_check_at) {
    return { tone: "live", label: live, detail: t("live.firstCheck") };
  }
  const intervalMs = (f.poll_interval_seconds ?? 60) * 1000;
  const age = now - new Date(f.last_check_at).getTime();
  if (age > STUCK_AFTER_INTERVALS * intervalMs) {
    return { tone: "warn", label: live, detail: t("live.lastCheck", { ago: fmt.ago(f.last_check_at, now) }) };
  }
  return { tone: "live", label: live, detail: t("live.checked", { ago: fmt.ago(f.last_check_at, now) }) };
}

/** Multi-line hover text for the badge. */
export function liveTooltip(f: Freshness, i18n: I18n): string {
  const { t, fmt } = i18n;
  const every = Math.round(f.poll_interval_seconds ?? 60);
  if (!f.poller_running) {
    return [
      t("live.tip.notRunning"),
      t("live.tip.lastCheck", { time: fmt.clock(f.last_check_at) }),
      t("live.tip.start"),
    ].join("\n");
  }
  const n = (value: number) => value.toLocaleString("en-IN");
  return [
    f.portal?.open ? t("live.tip.portalDown", { time: fmt.clock(f.portal.next_attempt_at) }) : null,
    t("live.tip.every", { every }),
    f.rolling?.enabled ? t("live.tip.rotation", { hours: rotationHours(f.rolling.hours, i18n) }) : null,
    t("live.tip.lastCheck", { time: fmt.clock(f.last_check_at) }),
    f.last_update
      ? t("live.tip.lastUpdate", { time: fmt.clock(f.last_update.at), area: f.last_update.area })
      : t("live.tip.noUpdates"),
    t("live.tip.lastRecon", { time: fmt.clock(f.last_full_reconciliation) }),
    t("live.tip.recomputed", { time: fmt.clock(f.analysis?.last_at) }),
    f.parity
      ? t("live.tip.parity", { n: n(f.parity.exact_slices), m: n(f.parity.registered_slices) })
      : t("live.tip.count", { n: n(f.reconciled_shards), m: n(f.registered_shards) }),
  ]
    .filter(Boolean)
    .join("\n");
}

/** "08:00-20:00" -> "from 08:00 to 20:00"; "always" -> "all day". */
export function rotationHours(hours: string, i18n: I18n): string {
  const [start, end] = hours.split("-");
  return end ? i18n.t("live.hoursRange", { start, end }) : i18n.t("live.hoursAlways");
}

/** What the "Risk flags recomputed" figure needs to say beside its time. */
export function analysisHint(f: Freshness, i18n: I18n, now = Date.now()): string {
  const { t, fmt } = i18n;
  const a = f.analysis;
  if (a.in_progress) return t("analysis.started", { ago: fmt.ago(a.started_at, now) });
  if (!a.last_at) return a.changes_waiting ? t("analysis.firstDue") : t("analysis.notYet");
  if (!a.changes_waiting) return t("analysis.includesAll", { ago: fmt.ago(a.last_at, now) });
  if (!f.poller_running) return t("analysis.waitPoller");
  if (!a.every_minutes) return t("analysis.autoOff");
  const due = new Date(a.last_at).getTime() + a.every_minutes * 60_000;
  return due <= now
    ? t("analysis.nextCheck")
    : t("analysis.includedBy", { time: fmt.clock(new Date(due).toISOString()) });
}
