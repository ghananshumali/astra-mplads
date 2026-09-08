import { useQuery } from "@tanstack/react-query";
import { Info, MapPin } from "lucide-react";
import { useNavigate } from "react-router-dom";

import { api } from "../api/client";
import { Banner, Card, Empty, ErrorState, Skeleton } from "../components/ui";
import { RiskMap } from "../components/map/RiskMap";
import { RISK_META, compact } from "../lib/format";
import { useAuthority } from "../state/AuthorityContext";
import "./pages.css";

export default function Geography() {
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
          <h1 className="page-title">Geographic intelligence</h1>
          <p className="page-sub">
            Where risk concentrates across states and districts
          </p>
        </div>
      </header>

      <Banner tone="neutral" icon={<Info size={15} />}>
        Markers are positioned at <b>state centroids</b> and sized by high-risk
        case count. The eSAKSHI exports for this batch carry no asset
        coordinates, so no work-level position is shown or implied.
      </Banner>

      <div className="geo-grid">
      {states.isError ? (
        <ErrorState error={states.error} onRetry={() => states.refetch()} />
      ) : states.isLoading ? (
        <Skeleton h={460} />
      ) : (
        <Card
          title="State-level risk concentration"
          subtitle="Click a marker to inspect that state"
          tight
        >
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
          tier === "ministry"
            ? "Districts with the highest concentration"
            : "Districts in your scope"
        }
        subtitle="Click a row to open that district's cases"
        tight
      >
        {districts.isLoading ? (
          <Skeleton h={200} />
        ) : (districts.data ?? []).length === 0 ? (
          <Empty icon={<MapPin size={20} />} title="No district data in scope" />
        ) : (
          <div className="table-scroll" style={{ maxHeight: 340 }}>
            <table className="table">
              <thead>
                <tr>
                  <th>District</th>
                  <th>State</th>
                  <th style={{ width: 90 }}>Cases</th>
                  <th style={{ width: 100 }}>High risk</th>
                  <th style={{ width: 96 }}>Avg risk</th>
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
