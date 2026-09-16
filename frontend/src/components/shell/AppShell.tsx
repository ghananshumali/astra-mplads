import {
  Activity,
  Building2,
  Check,
  ChevronDown,
  Database,
  Landmark,
  Languages,
  LayoutDashboard,
  Map,
  Network,
  ScrollText,
  ShieldAlert,
  Sparkles,
  UserSquare2,
} from "lucide-react";
import { NavLink, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import type { RefObject } from "react";

import { api } from "../../api/client";
import type { Freshness, Tier } from "../../api/types";
import { useI18n } from "../../i18n/context";
import type { MessageKey } from "../../i18n/context";
import { LANGUAGES } from "../../i18n/languages";
import { TIER_ORDER, compact } from "../../lib/format";
import { liveState, liveTooltip, useNow } from "../../lib/live";
import { useAuthority } from "../../state/AuthorityContext";
import { Spinner } from "../ui";
import "./shell.css";

const TIER_ICON: Record<Tier, typeof Landmark> = {
  ministry: Landmark,
  state: Map,
  district: Building2,
  mp: UserSquare2,
};

const NAV: { to: string; label: MessageKey; icon: typeof Landmark; end?: boolean }[] = [
  { to: "/", label: "nav.overview", icon: LayoutDashboard, end: true },
  { to: "/cases", label: "nav.cases", icon: ShieldAlert },
  { to: "/geography", label: "nav.geography", icon: Map },
  { to: "/network", label: "nav.network", icon: Network },
  { to: "/pipeline", label: "nav.pipeline", icon: Activity },
  { to: "/data", label: "nav.data", icon: Database },
];

/** Close a popover on an outside click or Escape. */
function useDismiss(ref: RefObject<HTMLDivElement | null>, close: () => void) {
  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [ref, close]);
}

/* ---------------------------------------------------------- language menu */
function LanguageSwitcher() {
  const { language, setLanguage, t } = useI18n();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(ref, close);

  return (
    <div className="auth-switch lang-switch" ref={ref}>
      <button
        className="lang-trigger"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t("lang.button", { name: language.native })}
        title={t("lang.menuTitle")}
      >
        <Languages size={14} />
        <span lang={language.code}>{language.native}</span>
        <ChevronDown size={13} className="dim" />
      </button>

      {open && (
        <div className="auth-menu lang-menu fade-in" role="menu">
          <div className="auth-menu-head">{t("lang.menuTitle")}</div>
          <div className="lang-list">
            {LANGUAGES.map((l) => (
              <button
                key={l.code}
                role="menuitemradio"
                aria-checked={l.code === language.code}
                className={`auth-option lang-option${l.code === language.code ? " active" : ""}`}
                onClick={() => {
                  setLanguage(l.code);
                  setOpen(false);
                }}
              >
                <span className="grow">
                  <span className="semibold" lang={l.code} dir={l.dir}>
                    {l.native}
                  </span>
                  {l.english !== l.native && (
                    <span className="auth-option-lens">{l.english}</span>
                  )}
                </span>
                {l.code === language.code && <Check size={14} />}
              </button>
            ))}
          </div>
          <div className="auth-menu-foot">{t("lang.draftNote")}</div>
        </div>
      )}
    </div>
  );
}

/* --------------------------------------------------------- authority menu */
function AuthoritySwitcher() {
  const { t } = useI18n();
  const { tier, setTier, scope, setScope, scopeLabel } = useAuthority();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const { data: facets } = useQuery({
    queryKey: ["facets"],
    queryFn: api.facets,
    staleTime: 5 * 60_000,
  });
  const close = useCallback(() => setOpen(false), []);
  useDismiss(ref, close);

  const options =
    tier === "state"
      ? facets?.states
      : tier === "district"
        ? facets?.districts
        : tier === "mp"
          ? facets?.constituencies
          : undefined;

  const Icon = TIER_ICON[tier];

  return (
    <div className="auth-switch" ref={ref}>
      <button
        className="auth-trigger"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <span className="auth-avatar">
          <Icon size={15} />
        </span>
        <span className="auth-text">
          <span className="auth-role">{t(`tier.${tier}.label`)}</span>
          <span className="auth-scope">{scopeLabel}</span>
        </span>
        <ChevronDown size={14} className="dim" />
      </button>

      {open && (
        <div className="auth-menu fade-in">
          <div className="auth-menu-head">{t("auth.signedInAs")}</div>
          {TIER_ORDER.map((tr) => {
            const TI = TIER_ICON[tr];
            return (
              <button
                key={tr}
                className={`auth-option${tr === tier ? " active" : ""}`}
                onClick={() => {
                  setTier(tr);
                  if (tr === "ministry") setOpen(false);
                }}
              >
                <TI size={15} />
                <span className="grow">
                  <span className="semibold">{t(`tier.${tr}.label`)}</span>
                  <span className="auth-option-lens">{t(`tier.${tr}.lens`)}</span>
                </span>
              </button>
            );
          })}

          {options && (
            <div className="auth-scope-pick">
              <label className="field-label">
                {tier === "state"
                  ? t("auth.yourState")
                  : tier === "district"
                    ? t("auth.yourDistrict")
                    : t("auth.yourConstituency")}
              </label>
              <select
                className="select"
                value={
                  tier === "state"
                    ? (scope.state ?? "")
                    : tier === "district"
                      ? (scope.district ?? "")
                      : (scope.constituency ?? "")
                }
                onChange={(e) => {
                  const v = e.target.value || undefined;
                  setScope(
                    tier === "state"
                      ? { state: v }
                      : tier === "district"
                        ? { district: v }
                        : { constituency: v },
                  );
                }}
              >
                <option value="">{t("auth.all", { count: compact(options.length) })}</option>
                {options.map((o) => (
                  <option key={o} value={o}>
                    {o}
                  </option>
                ))}
              </select>
              <button
                className="btn btn-primary btn-sm btn-block"
                style={{ marginTop: 8 }}
                onClick={() => setOpen(false)}
              >
                {t("auth.apply")}
              </button>
            </div>
          )}
          <div className="auth-menu-foot">{t("auth.demoNote")}</div>
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------ data badge */
function DataBadge() {
  const { t } = useI18n();
  const { data, isLoading } = useQuery({
    queryKey: ["data-source"],
    queryFn: api.dataSource,
    staleTime: 5 * 60_000,
  });
  const { data: llm } = useQuery({
    queryKey: ["llm"],
    queryFn: api.llm,
    staleTime: 5 * 60_000,
  });

  // Polled only for the live corpus; a CSV batch has nothing to watch.
  const { data: freshness } = useQuery({
    queryKey: ["freshness"],
    queryFn: api.freshness,
    enabled: Boolean(data?.live),
    refetchInterval: 20_000,
    retry: false,
  });

  if (isLoading) return <Spinner size={14} />;
  const mode = (data?.mode_resolved ?? "—").toUpperCase();
  const fresh = data?.freshness_vs_live_portal;

  return (
    <div className="row gap-3">
      {llm && (
        <span
          className="head-badge"
          title={
            llm.configured ? t("badge.aiActive", { model: llm.model }) : t("badge.noKey")
          }
        >
          <Sparkles size={12} />
          {llm.configured ? t("badge.ai") : t("badge.deterministic")}
        </span>
      )}
      {data?.live && freshness ? (
        <LiveBadge f={freshness} />
      ) : (
        <span
          className="head-badge"
          title={
            fresh
              ? t("badge.coverage", {
                  local: compact(fresh.local_recommended_works),
                  live: compact(fresh.live_recommended_works),
                })
              : t("badge.dataMode")
          }
        >
          <Database size={12} />
          {mode}
          {fresh && <b style={{ marginInlineStart: 4 }}>{fresh.coverage_pct}%</b>}
        </span>
      )}
    </div>
  );
}

/** "● Live · checked 20 s ago" — links to the Data source page for detail. */
function LiveBadge({ f }: { f: Freshness }) {
  const i18n = useI18n();
  const now = useNow(10_000);
  const state = liveState(f, i18n, now);
  return (
    <NavLink to="/data" className={`head-badge live-badge ${state.tone}`} title={liveTooltip(f, i18n)}>
      <span className={`live-dot ${state.tone}`} aria-hidden />
      <b>{state.label}</b>
      <span className="live-badge-detail">· {state.detail}</span>
    </NavLink>
  );
}

/* ------------------------------------------------------------------ shell */
export function AppShell({ children }: { children: React.ReactNode }) {
  const { t } = useI18n();
  const { pathname } = useLocation();
  const { tier } = useAuthority();

  return (
    <div className="shell">
      <header className="head">
        <div className="brand">
          <span className="brand-mark" aria-hidden>
            <ScrollText size={16} />
          </span>
          <span className="brand-text">
            <span className="brand-name">ASTRA</span>
            <span className="brand-sub">{t("brand.sub")}</span>
          </span>
        </div>
        <div className="grow" />
        <DataBadge />
        <LanguageSwitcher />
        <AuthoritySwitcher />
      </header>

      <div className="body">
        <nav className="side" aria-label={t("nav.main")}>
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              className={({ isActive }) =>
                `side-link${isActive ? " active" : ""}`
              }
            >
              <n.icon size={16} />
              <span>{t(n.label)}</span>
            </NavLink>
          ))}
          <div className="side-foot">
            <div className="side-foot-title">
              {t("shell.view", { tier: t(`tier.${tier}.short`) })}
            </div>
            <p>{t(`tier.${tier}.lens`)}</p>
          </div>
        </nav>

        <main className="main" key={pathname}>
          {children}
        </main>
      </div>
    </div>
  );
}
