/** State-level risk map.
 *
 * Uses Leaflet rather than a WebGL map library. Leaflet paints raster tiles as
 * plain <img> elements on the main thread, with no web worker and no vector
 * decoding — the pipeline that failed to initialise here and left the basemap
 * blank. Fewer moving parts is the right trade for a map that has to work on
 * whatever machine a live demonstration runs on.
 *
 * Data integrity: markers sit at STATE CENTROIDS and are sized by high-risk
 * case count. The eSAKSHI exports carry no per-work coordinates, so no
 * work-level position is shown or implied anywhere in this component.
 */
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { useEffect, useRef, useState } from "react";

import type { StateRow } from "../../api/types";
import { useI18n } from "../../i18n/context";
import { STATE_CENTROIDS } from "../../lib/centroids";
import { RISK_META, compact } from "../../lib/format";
import "./map.css";

const INDIA_CENTER: [number, number] = [22.6, 80.0];
const INDIA_BOUNDS: [[number, number], [number, number]] = [
  [5.5, 66.0],
  [37.5, 98.5],
];

export interface RiskMapProps {
  rows: StateRow[];
  onSelectState?: (state: string) => void;
  height?: number;
}

/** Escape text placed into Leaflet's HTML tooltips and popups. */
function esc(text: string): string {
  return text.replace(/[&<>"']/g, (ch) => `&#${ch.charCodeAt(0)};`);
}

export function RiskMap({ rows, onSelectState, height = 460 }: RiskMapProps) {
  const { t } = useI18n();
  const holder = useRef<HTMLDivElement | null>(null);
  const map = useRef<L.Map | null>(null);
  const layer = useRef<L.LayerGroup | null>(null);
  const [tileError, setTileError] = useState(false);
  const [ready, setReady] = useState(false);
  // keep the latest callback without re-creating markers on every render
  const onSelect = useRef(onSelectState);
  onSelect.current = onSelectState;

  // ---- create the map once
  useEffect(() => {
    if (!holder.current || map.current) return;

    const m = L.map(holder.current, {
      center: INDIA_CENTER,
      zoom: 4,
      minZoom: 3.4,
      maxZoom: 9,
      zoomControl: true,
      attributionControl: true,
      scrollWheelZoom: false, // page scroll wins; use +/- or double-click
      maxBounds: L.latLngBounds(INDIA_BOUNDS).pad(0.25),
    });

    // Esri's light-grey canvas is muted, so the red risk bubbles carry the
    // colour. Both sources below are keyless: CARTO's raster endpoint now
    // returns "API KEY REQUIRED" watermarked tiles, which is why it is not used.
    const base = L.tileLayer(
      "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/" +
        "World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
      {
        maxZoom: 16,
        attribution: "Esri, HERE, Garmin, &copy; OpenStreetMap contributors",
      },
    );
    // place and boundary labels ride above the basemap but below the markers
    const labels = L.tileLayer(
      "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/" +
        "World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}",
      { maxZoom: 16, pane: "shadowPane" },
    );

    let swapped = false;
    base.on("tileerror", () => {
      // One automatic fallback to OpenStreetMap before surfacing an error, so a
      // single unreachable tile host does not blank the map during a demo.
      if (swapped) {
        setTileError(true);
        return;
      }
      swapped = true;
      m.removeLayer(base);
      m.removeLayer(labels);
      const osm = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 18,
        attribution: "&copy; OpenStreetMap contributors",
      });
      osm.on("tileerror", () => setTileError(true));
      osm.on("load", () => setReady(true));
      osm.addTo(m);
      osm.bringToBack();
    });
    base.on("load", () => setReady(true));
    base.addTo(m);
    labels.addTo(m);

    layer.current = L.layerGroup().addTo(m);
    map.current = m;

    // Leaflet mis-measures if the container was hidden or resized at mount.
    const invalidate = () => {
      m.invalidateSize();
      m.fitBounds(INDIA_BOUNDS, { padding: [12, 12], animate: false });
    };
    const ro = new ResizeObserver(invalidate);
    ro.observe(holder.current);
    const t = setTimeout(invalidate, 250);

    return () => {
      clearTimeout(t);
      ro.disconnect();
      m.remove();
      map.current = null;
      layer.current = null;
    };
  }, []);

  // ---- (re)draw markers when the data changes
  useEffect(() => {
    if (!map.current || !layer.current) return;
    layer.current.clearLayers();

    const points = rows
      .map((r) => {
        const c = STATE_CENTROIDS[r.state.toUpperCase()];
        return c ? { row: r, lat: c[0], lon: c[1] } : null;
      })
      .filter(Boolean) as { row: StateRow; lat: number; lon: number }[];
    if (!points.length) return;

    const maxHigh = Math.max(...points.map((p) => p.row.high_risk), 1);

    for (const p of points) {
      // area-proportional radius so a bubble twice the area means twice the count
      const share = p.row.high_risk / maxHigh;
      const radius = 6 + Math.sqrt(share) * 22;
      const hasHigh = p.row.high_risk > 0;
      const color = hasHigh ? RISK_META.high.color : RISK_META.low.color;

      const marker = L.circleMarker([p.lat, p.lon], {
        radius,
        color,
        weight: 2,
        fillColor: color,
        fillOpacity: 0.32,
        className: "risk-bubble",
      });

      marker.bindTooltip(
        `<b>${esc(p.row.state)}</b><br>` +
          esc(t("map.tooltip", { high: compact(p.row.high_risk), total: compact(p.row.flags) })),
        { direction: "top", offset: [0, -radius] },
      );

      marker.bindPopup(
        `<div class="map-pop">
           <div class="map-pop-title">${esc(p.row.state)}</div>
           <dl>
             <dt>${esc(t("map.high"))}</dt><dd class="hi">${compact(p.row.high_risk)}</dd>
             <dt>${esc(t("map.all"))}</dt><dd>${compact(p.row.flags)}</dd>
             <dt>${esc(t("map.avg"))}</dt><dd>${p.row.avg_risk}</dd>
           </dl>
           <button type="button" data-state="${encodeURIComponent(p.row.state)}"
                   class="map-pop-btn">${esc(t("map.view"))}</button>
         </div>`,
        { closeButton: true, minWidth: 190 },
      );

      marker.on("popupopen", (e) => {
        const btn = (e as L.PopupEvent).popup
          .getElement()
          ?.querySelector<HTMLButtonElement>(".map-pop-btn");
        btn?.addEventListener("click", () =>
          onSelect.current?.(decodeURIComponent(btn.dataset.state ?? "")),
        );
      });

      marker.addTo(layer.current);
    }
  }, [rows, t]);

  return (
    <div className="risk-map" style={{ height }}>
      <div ref={holder} className="risk-map-canvas" />

      {!ready && !tileError && (
        <div className="risk-map-overlay">
          <span className="risk-map-spinner" /> {t("map.loading")}
        </div>
      )}

      {tileError && (
        <div className="risk-map-overlay warn">
          {t("map.tileError")}
          <br />
          {t("map.tileErrorNote")}
        </div>
      )}

      <div className="map-legend">
        <div className="map-legend-title">{t("map.legend")}</div>
        <div className="map-legend-row">
          <span className="map-legend-dot sm" /> {t("map.fewer")}
        </div>
        <div className="map-legend-row">
          <span className="map-legend-dot lg" /> {t("map.more")}
        </div>
        <div className="map-legend-note">{t("map.legendNote")}</div>
      </div>
    </div>
  );
}
