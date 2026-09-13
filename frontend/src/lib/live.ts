/** Live-sync status, shared by the header badge and the Data source page.
 *  Presentation only: every fact comes from GET /meta/freshness. */

import { useEffect, useState } from "react";

import type { Freshness } from "../api/types";

/** Re-render on an interval so "checked 20 s ago" keeps counting between fetches. */
export function useNow(everyMs = 10_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), everyMs);
    return () => window.clearInterval(id);
  }, [everyMs]);
  return now;
}

/** "just now", "40 s ago", "3 min ago", "2 h ago", "4 d ago". */
export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, Math.floor((now - t) / 1000));
  if (s < 10) return "just now";
  if (s < 60) return `${Math.floor(s / 10) * 10} s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} h ago`;
  return `${Math.floor(h / 24)} d ago`;
}

/** "13 Sep, 11:42" in Indian locale. */
export function clockTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
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

export function liveState(f: Freshness, now = Date.now()): LiveState {
  const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
  if (!f.poller_running) {
    return {
      tone: "stopped",
      label: "Updates paused",
      detail: f.last_check_at
        ? `last check ${ago(f.last_check_at, now)}`
        : "poller not started",
    };
  }
  if (f.sweep_in_progress) {
    return { tone: "live", label: "Live", detail: "full check running" };
  }
  if (f.parity?.exception_count) {
    return {
      tone: "warn",
      label: "Live",
      detail: `${plural(f.parity.exception_count, "area")} differ from portal`,
    };
  }
  if (f.stale.length) {
    return {
      tone: "warn",
      label: "Live",
      detail: `${plural(f.stale.length, "area")} not updating`,
    };
  }
  if (f.reconciliation_overdue) {
    return { tone: "warn", label: "Live", detail: "nightly check overdue" };
  }
  if (!f.last_check_at) {
    return { tone: "live", label: "Live", detail: "first check running" };
  }
  const intervalMs = (f.poll_interval_seconds ?? 60) * 1000;
  const age = now - new Date(f.last_check_at).getTime();
  if (age > STUCK_AFTER_INTERVALS * intervalMs) {
    return { tone: "warn", label: "Live", detail: `last check ${ago(f.last_check_at, now)}` };
  }
  return { tone: "live", label: "Live", detail: `checked ${ago(f.last_check_at, now)}` };
}

/** Multi-line hover text for the badge. */
export function liveTooltip(f: Freshness): string {
  const every = Math.round(f.poll_interval_seconds ?? 60);
  if (!f.poller_running) {
    return [
      "The poller is not running, so portal changes are not being picked up.",
      `Last portal check: ${clockTime(f.last_check_at)}`,
      "Start it with: python -m astra.ingestion.poller",
    ].join("\n");
  }
  return [
    `The eSAKSHI portal is checked every ${every} s; only areas whose counts moved are re-read.`,
    `Last portal check: ${clockTime(f.last_check_at)}`,
    f.last_update
      ? `Last update stored: ${clockTime(f.last_update.at)} — ${f.last_update.area}`
      : "No updates stored yet",
    `Last full reconciliation: ${clockTime(f.last_full_reconciliation)}`,
    f.parity
      ? `${f.parity.exact_slices.toLocaleString("en-IN")} of ${f.parity.registered_slices.toLocaleString("en-IN")} areas match the portal on every figure`
      : `${f.reconciled_shards.toLocaleString("en-IN")} of ${f.registered_shards.toLocaleString("en-IN")} areas match the portal's own count`,
  ].join("\n");
}
