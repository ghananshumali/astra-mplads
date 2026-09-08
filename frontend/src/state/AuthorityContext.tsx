/** Authority (RBAC role) context.
 *
 * The tier drives BOTH the data scope and the synthesis the backend returns.
 * Scope selections are remembered per tier, so switching Ministry → District →
 * Ministry restores what the user had chosen rather than resetting.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { ReactNode } from "react";

import type { CaseFilters, Tier } from "../api/types";

interface Scope {
  state?: string;
  district?: string;
  constituency?: string;
}

interface AuthorityValue {
  tier: Tier;
  setTier: (t: Tier) => void;
  scope: Scope;
  setScope: (s: Scope) => void;
  /** Filters that express "what this authority is allowed to look at". */
  scopeFilters: Pick<CaseFilters, "states" | "districts" | "constituencies">;
  scopeLabel: string;
}

const Ctx = createContext<AuthorityValue | null>(null);
const STORAGE = "astra.authority";

export function AuthorityProvider({ children }: { children: ReactNode }) {
  const [tier, setTierRaw] = useState<Tier>("ministry");
  const [scopes, setScopes] = useState<Record<Tier, Scope>>({
    ministry: {},
    state: {},
    district: {},
    mp: {},
  });

  useEffect(() => {
    try {
      const raw = localStorage.getItem(STORAGE);
      if (raw) {
        const saved = JSON.parse(raw);
        if (saved.tier) setTierRaw(saved.tier);
        if (saved.scopes) setScopes((s) => ({ ...s, ...saved.scopes }));
      }
    } catch {
      /* first run */
    }
  }, []);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE, JSON.stringify({ tier, scopes }));
    } catch {
      /* storage unavailable — not fatal */
    }
  }, [tier, scopes]);

  const setTier = useCallback((t: Tier) => setTierRaw(t), []);
  const setScope = useCallback(
    (s: Scope) => setScopes((prev) => ({ ...prev, [tier]: s })),
    [tier],
  );

  const scope = scopes[tier] ?? {};

  const scopeFilters = useMemo(() => {
    if (tier === "state" && scope.state) return { states: [scope.state] };
    if (tier === "district" && scope.district)
      return { districts: [scope.district] };
    if (tier === "mp" && scope.constituency)
      return { constituencies: [scope.constituency] };
    return {};
  }, [tier, scope]);

  const scopeLabel = useMemo(() => {
    if (tier === "ministry") return "All states";
    if (tier === "state") return scope.state ?? "Select a state";
    if (tier === "district") return scope.district ?? "Select a district";
    return scope.constituency ?? "Select a constituency";
  }, [tier, scope]);

  const value = useMemo(
    () => ({ tier, setTier, scope, setScope, scopeFilters, scopeLabel }),
    [tier, setTier, scope, setScope, scopeFilters, scopeLabel],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuthority() {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuthority must be used inside AuthorityProvider");
  return v;
}
