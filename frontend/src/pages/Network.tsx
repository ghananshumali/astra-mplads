import { useQuery } from "@tanstack/react-query";
import { Building2, Info } from "lucide-react";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../api/client";
import type { CaseSummary } from "../api/types";
import { Banner, Card, Empty, ErrorState, RiskBadge, Skeleton } from "../components/ui";
import { useI18n } from "../i18n/context";
import { compact, titleCase } from "../lib/format";
import { useAuthority } from "../state/AuthorityContext";
import "./pages.css";

/** Vendor, implementing-agency and district-authority cases are agency-level flags. */
export default function Network() {
  const { t, fmt, language } = useI18n();
  const lang = language.code;
  const nav = useNavigate();
  const { scopeFilters } = useAuthority();
  const [picked, setPicked] = useState<CaseSummary | null>(null);

  const list = useQuery({
    queryKey: ["network-cases", scopeFilters, lang],
    queryFn: () =>
      api.cases(
        {
          ...scopeFilters,
          entity_types: ["agency"],
          // network cases only: district authorities' own sanction and cost
          // cases are agency-level too, and would crowd them out
          rule_ids: ["N-NET-01"],
          order: "risk",
          limit: 200,
        },
        lang,
      ),
  });

  const works = useQuery({
    queryKey: ["agency-works", picked?.entity_id],
    queryFn: () => api.agencyWorks(picked!.entity_id, 40),
    enabled: !!picked,
  });

  const detail = useQuery({
    queryKey: ["case", picked?.flag_id, "district", lang],
    queryFn: () => api.caseDetail(picked!.flag_id, "district", lang),
    enabled: !!picked,
  });

  const netFinding = detail.data?.findings.find((f) => f.agent === "network");
  const d = (netFinding?.details ?? {}) as Record<string, unknown>;
  const actorName = useMemo(() => {
    if (!picked) return "";
    if (typeof d.actor === "string" && d.actor) return titleCase(d.actor);
    return picked.display_title ?? picked.entity_id;
  }, [picked, d.actor]);

  return (
    <div className="stack gap-5">
      <header className="page-head">
        <div>
          <h1 className="page-title">{t("net.title")}</h1>
          <p className="page-sub">{t("net.sub")}</p>
        </div>
      </header>

      <Banner tone="neutral" icon={<Info size={15} />}>
        {t("net.banner")}
      </Banner>

      <div className="grid-2">
        <Card title={t("net.list.title")} subtitle={t("net.list.sub")} tight>
          {list.isError ? (
            <ErrorState error={list.error} onRetry={() => list.refetch()} />
          ) : list.isLoading ? (
            <Skeleton h={280} />
          ) : (list.data?.cases ?? []).length === 0 ? (
            <Empty
              icon={<Building2 size={20} />}
              title={t("net.empty")}
              hint={t("net.emptyHint")}
            />
          ) : (
            <div className="table-scroll" style={{ maxHeight: 420 }}>
              <table className="table">
                <thead>
                  <tr>
                    <th style={{ width: 84 }}>{t("net.col.risk")}</th>
                    <th>{t("net.col.actor")}</th>
                    <th style={{ width: 92 }}>{t("net.col.state")}</th>
                  </tr>
                </thead>
                <tbody>
                  {list.data!.cases.map((c) => (
                    <tr
                      key={c.flag_id}
                      className={picked?.flag_id === c.flag_id ? "selected" : ""}
                      onClick={() => setPicked(c)}
                    >
                      <td>
                        <RiskBadge score={c.risk_score} size="sm" />
                      </td>
                      <td>
                        <div className="semibold clamp-2">
                          {c.display_title}
                        </div>
                        <div className="text-xs dim clamp-2">
                          {c.primary_signal}
                        </div>
                      </td>
                      <td className="text-xs muted truncate">{c.state ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <Card
          title={picked ? actorName : t("net.detail")}
          subtitle={picked ? t("net.detailSub") : t("net.pick")}
          tight
        >
          {!picked ? (
            <Empty title={t("net.nothing")} hint={t("net.nothingHint")} />
          ) : detail.isLoading ? (
            <Skeleton h={220} />
          ) : (
            <div className="stack gap-4">
              {netFinding && (
                <>
                  <p className="text-sm" style={{ margin: 0 }}>
                    {netFinding.explained?.plain ?? netFinding.summary}
                  </p>
                  {typeof d.vendor_id === "string" && (
                    <div className="text-xs muted">
                      {t("net.vendorId", { id: d.vendor_id })}
                    </div>
                  )}
                  <div className="dup-stats">
                    <Metric label={t("net.works")} value={compact(Number(d.works ?? 0))} />
                    <Metric label={t("net.districts")} value={String(d.districts ?? "—")} />
                    <Metric label={t("net.states")} value={String(d.states ?? "—")} />
                    <Metric
                      label={t("net.above")}
                      value={`${Math.round(Number(d.overrun_share ?? 0) * 100)}%`}
                    />
                  </div>
                  {Boolean(d.likely_national_supplier) && (
                    <Banner tone="info">{t("net.national")}</Banner>
                  )}
                  {Array.isArray(d.district_list) && (
                    <div>
                      <div className="field-label">{t("net.covered")}</div>
                      <div className="text-sm muted">
                        {(d.district_list as string[]).join(", ")}
                      </div>
                    </div>
                  )}
                </>
              )}

              <div>
                <div className="field-label">{t("net.portfolio")}</div>
                {works.isLoading ? (
                  <Skeleton h={140} />
                ) : (works.data ?? []).length === 0 ? (
                  <p className="text-sm dim">{t("net.noWorks")}</p>
                ) : (
                  <div className="table-scroll" style={{ maxHeight: 260 }}>
                    <table className="table">
                      <thead>
                        <tr>
                          <th>{t("net.col.work")}</th>
                          <th style={{ width: 120 }}>{t("net.col.district")}</th>
                          <th style={{ width: 110 }}>{t("net.col.sanctioned")}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(works.data ?? []).map((w) => (
                          <tr
                            key={w.work_id}
                            onClick={() =>
                              nav(`/cases?q=${encodeURIComponent(w.work_id!)}`)
                            }
                          >
                            <td className="text-xs mono truncate">
                              {w.work_id}
                            </td>
                            <td className="text-xs muted truncate">
                              {w.district ?? "—"}
                            </td>
                            <td className="text-xs num">
                              {fmt.rupees(w.sanctioned_amount ?? null)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat">
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}
