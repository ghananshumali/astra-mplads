import { useQuery } from "@tanstack/react-query";
import { Info, MapPin } from "lucide-react";
import { useNavigate } from "react-router-dom";

import { api } from "../api/client";
import { Banner, Card, Empty, ErrorState, Skeleton } from "../components/ui";
import { RiskMap } from "../components/map/RiskMap";
import { useI18n } from "../i18n/context";
import { T } from "../i18n/T";
import { RISK_META, compact } from "../lib/format";
import { useAuthority } from "../state/AuthorityContext";
import "./pages.css";

export default function Geography() {
  const { t } = useI18n();
  const nav = useNavigate();
  const { tier, scopeFilters } = useAuthority();

  const states = useQuery({ queryKey: ["states"], queryFn: api.states });
  const districts = useQuery({
    queryKey: ["districts-geo", scopeFilters.states?.[0]],
    queryFn: () => api.districts(scopeFilters.states?.[0], 20),
  });

  return (
    <div className="stack gap-5">
      <header className="page-head">
        <div>
          <h1 className="page-title">{t("geo.title")}</h1>
          <p className="page-sub">{t("geo.sub")}</p>
        </div>
      </header>

      <Banner tone="neutral" icon={<Info size={15} />}>
        <T k="geo.banner" values={{ centroids: <b>{t("geo.centroids")}</b> }} />
      </Banner>

      <div className="geo-grid">
      {states.isError ? (
        <ErrorState error={states.error} onRetry={() => states.refetch()} />
      ) : states.isLoading ? (
        <Skeleton h={460} />
      ) : (
        <Card title={t("geo.map.title")} subtitle={t("geo.map.sub")} tight>
          <RiskMap
            rows={states.data ?? []}
            onSelectState={(st) => nav(`/cases?state=${encodeURIComponent(st)}`)}
            height={520}
          />
        </Card>
      )}

      <Card
        className="geo-table-card"
        title={
          tier === "ministry" ? t("geo.table.titleMinistry") : t("geo.table.titleScoped")
        }
        subtitle={t("geo.table.sub")}
        tight
      >
        {districts.isLoading ? (
          <Skeleton h={200} />
        ) : (districts.data ?? []).length === 0 ? (
          <Empty icon={<MapPin size={20} />} title={t("geo.table.empty")} />
        ) : (
          <div className="table-scroll" style={{ maxHeight: 340 }}>
            <table className="table">
              <thead>
                <tr>
                  <th>{t("geo.col.district")}</th>
                  <th>{t("geo.col.state")}</th>
                  <th style={{ width: 90 }}>{t("geo.col.cases")}</th>
                  <th style={{ width: 100 }}>{t("geo.col.high")}</th>
                  <th style={{ width: 96 }}>{t("geo.col.avg")}</th>
                </tr>
              </thead>
              <tbody>
                {(districts.data ?? []).map((d) => (
                  <tr
                    key={`${d.state}-${d.district}`}
                    onClick={() =>
                      nav(`/cases?district=${encodeURIComponent(d.district)}`)
                    }
                  >
                    <td className="semibold">{d.district}</td>
                    <td className="muted text-sm">{d.state}</td>
                    <td className="num">{compact(d.flags)}</td>
                    <td
                      className="num semibold"
                      style={{ color: RISK_META.high.color }}
                    >
                      {compact(d.high_risk)}
                    </td>
                    <td className="num">{d.avg_risk}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      </div>
    </div>
  );
}
