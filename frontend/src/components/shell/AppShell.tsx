import {
  Activity,
  Building2,
  ChevronDown,
  Database,
  Landmark,
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
import { useEffect, useRef, useState } from "react";

import { api } from "../../api/client";
import type { Tier } from "../../api/types";
import { TIERS } from "../../lib/format";
import { useAuthority } from "../../state/AuthorityContext";
import { Spinner } from "../ui";
import "./shell.css";

const TIER_ICON: Record<Tier, typeof Landmark> = {
  ministry: Landmark,
  state: Map,
  district: Building2,
  mp: UserSquare2,
};

const NAV = [
  { to: "/", label: "Overview", icon: LayoutDashboard, end: true },
  { to: "/cases", label: "Risk cases", icon: ShieldAlert },
  { to: "/geography", label: "Geography", icon: Map },
  { to: "/network", label: "Vendor network", icon: Network },
  { to: "/pipeline", label: "Pipeline", icon: Activity },
  { to: "/data", label: "Data source", icon: Database },
];

/* --------------------------------------------------------- authority menu */
function AuthoritySwitcher() {
  const { tier, setTier, scope, setScope, scopeLabel } = useAuthority();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const { data: facets } = useQuery({
    queryKey: ["facets"],
    queryFn: api.facets,
    staleTime: 5 * 60_000,
  });

  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

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
          <span className="auth-role">{TIERS[tier].label}</span>
          <span className="auth-scope">{scopeLabel}</span>
        </span>
        <ChevronDown size={14} className="dim" />
      </button>

      {open && (
        <div className="auth-menu fade-in">
          <div className="auth-menu-head">Signed in as</div>
          {(Object.keys(TIERS) as Tier[]).map((t) => {
            const TI = TIER_ICON[t];
            return (
              <button
                key={t}
                className={`auth-option${t === tier ? " active" : ""}`}
                onClick={() => {
                  setTier(t);
                  if (t === "ministry") setOpen(false);
                }}
              >
                <TI size={15} />
                <span className="grow">
                  <span className="semibold">{TIERS[t].label}</span>
                  <span className="auth-option-lens">{TIERS[t].lens}</span>
                </span>
              </button>
            );
          })}

          {options && (
            <div className="auth-scope-pick">
              <label className="field-label">
                {tier === "state"
                  ? "Your state"
                  : tier === "district"
                    ? "Your district"
                    : "Your constituency"}
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
                <option value="">All ({options.length})</option>
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
                Apply
              </button>
            </div>
          )}
          <div className="auth-menu-foot">
            Demo role switcher. Production roadmap: eSAKSHI SSO with full RBAC
            and audit logging.
          </div>
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------ data badge */
function DataBadge() {
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

  if (isLoading) return <Spinner size={14} />;
  const mode = (data?.mode_resolved ?? "—").toUpperCase();
  const fresh = data?.freshness_vs_live_portal;

  return (
    <div className="row gap-3">
      {llm && (
        <span
          className="head-badge"
          title={
            llm.configured
              ? `AI synthesis active · ${llm.model}`
              : "No GROQ_API_KEY — deterministic synthesis in use"
          }
        >
          <Sparkles size={12} />
          {llm.configured ? "AI synthesis" : "Deterministic"}
        </span>
      )}
      <span
        className="head-badge"
        title={
          fresh
            ? `Local corpus holds ${fresh.local_recommended_works.toLocaleString()} recommended works; the live MoSPI portal reported ${fresh.live_recommended_works.toLocaleString()}.`
            : "Data mode"
        }
      >
        <Database size={12} />
        {mode}
        {fresh && <b style={{ marginLeft: 4 }}>{fresh.coverage_pct}%</b>}
      </span>
    </div>
  );
}

/* ------------------------------------------------------------------ shell */
export function AppShell({ children }: { children: React.ReactNode }) {
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
            <span className="brand-sub">MPLADS Risk Intelligence</span>
          </span>
        </div>
        <div className="grow" />
        <DataBadge />
        <AuthoritySwitcher />
      </header>

      <div className="body">
        <nav className="side" aria-label="Main">
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
              <span>{n.label}</span>
            </NavLink>
          ))}
          <div className="side-foot">
            <div className="side-foot-title">{TIERS[tier].short} view</div>
            <p>{TIERS[tier].lens}</p>
          </div>
        </nav>

        <main className="main" key={pathname}>
          {children}
        </main>
      </div>
    </div>
  );
}
