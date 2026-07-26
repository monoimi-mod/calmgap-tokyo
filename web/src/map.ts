/**
 * 地図の初期化とレイヤー更新。
 *
 * ベースマップは国土地理院の淡色地図タイル。
 * オープンデータの作品でベースマップだけ商用サービスに依存するのは筋が通らないし、
 * 淡色地図は情報量が抑えられていて主題図（コロプレス）を載せるのに適している。
 */

import maplibregl, { type Map as MLMap, type StyleSpecification } from "maplibre-gl";
import type { Meta } from "./types";

/** シーケンシャル（青）ランプ。style.css の --seq-* と同じ値。 */
const SEQ = {
  100: "#cde2fb",
  200: "#9ec5f4",
  300: "#6da7ec",
  400: "#3987e5",
  500: "#256abf",
  600: "#184f95",
  700: "#0d366b",
} as const;

const CAT_DEMAND = "#eb6834";
const CAT_HOST = "#1baf7a";

const GSI_ATTRIBUTION =
  '<a href="https://maps.gsi.go.jp/development/ichiran.html" target="_blank" rel="noopener">国土地理院</a>';

function baseStyle(): StyleSpecification {
  return {
    version: 8,
    // タイルが読めない環境（オフライン・閉域網）でも主題図だけは必ず描けるよう、
    // 背景色を敷いてからラスタを重ねる。
    sources: {
      gsi: {
        type: "raster",
        tiles: ["https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png"],
        tileSize: 256,
        minzoom: 5,
        maxzoom: 18,
        attribution: GSI_ATTRIBUTION,
      },
    },
    layers: [
      { id: "bg", type: "background", paint: { "background-color": "#eceae4" } },
      { id: "gsi", type: "raster", source: "gsi", paint: { "raster-opacity": 1 } },
    ],
  };
}

export interface MapHandles {
  map: MLMap;
  setMeshData: (data: GeoJSON.FeatureCollection) => void;
  setSelected: (meshCode: string | null) => void;
  toggleLayer: (id: "demand-points" | "host-points", visible: boolean) => void;
  flyTo: (lon: number, lat: number, zoom?: number) => void;
}

export async function initMap(
  container: string,
  meta: Meta,
  onMeshClick: (meshCode: string) => void,
): Promise<MapHandles> {
  const [minx, miny, maxx, maxy] = meta.bbox;

  const map = new maplibregl.Map({
    container,
    style: baseStyle(),
    bounds: [
      [minx, miny],
      [maxx, maxy],
    ],
    fitBoundsOptions: { padding: 24 },
    maxZoom: 17,
    minZoom: 9,
    attributionControl: false,
  });

  map.addControl(
    new maplibregl.AttributionControl({ compact: true, customAttribution: [] }),
    "bottom-right",
  );
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  // スケールバーは左下だと凡例と重なるため左上に置く。
  map.addControl(
    new maplibregl.ScaleControl({ maxWidth: 110, unit: "metric" }),
    "top-left",
  );

  // ベースマップのタイルが落ちてきているかに関係なく、スタイルが解釈でき次第
  // 主題レイヤーを追加する。
  // `load` イベントを待つと、閉域網やタイル配信障害のときに永久に発火せず、
  // 肝心の優先度マップまで表示されなくなる。ベースマップは文脈情報であって
  // 本体ではないので、無くても主題図は必ず出す。
  await new Promise<void>((resolve) => {
    if (map.isStyleLoaded()) {
      resolve();
      return;
    }
    map.on("styledata", () => {
      if (map.isStyleLoaded()) resolve();
    });
    // それでも駄目な場合の保険。resolve は複数回呼んでも無害。
    setTimeout(resolve, 4000);
  });

  // タイル取得の失敗でコンソールを埋めない。ベースマップ欠落は致命傷ではない。
  map.on("error", (e) => {
    const msg = String((e as { error?: Error }).error?.message ?? "");
    if (/tile|raster|Failed to fetch|NetworkError/i.test(msg)) return;
    console.warn("[map]", msg || e);
  });

  // --- 主題図: 優先度コロプレス ---
  map.addSource("mesh", {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });

  map.addLayer({
    id: "mesh-fill",
    type: "fill",
    source: "mesh",
    paint: {
      // 単一色相 light→dark。順位（パーセンタイル）を直接色に写す。
      // 参照する属性は `v`。表示モード（優先度/需要/負荷）の切替は
      // main.ts が各 feature の v を差し替えることで行い、
      // 塗り分けの定義自体は 1 つに保つ。
      "fill-color": [
        "interpolate",
        ["linear"],
        ["get", "v"],
        0.0, SEQ[100],
        0.35, SEQ[200],
        0.6, SEQ[300],
        0.78, SEQ[400],
        0.9, SEQ[500],
        0.97, SEQ[600],
        1.0, SEQ[700],
      ],
      // 低優先度はベースマップへ後退させ、地理的文脈を読めるようにする。
      "fill-opacity": [
        "interpolate",
        ["linear"],
        ["get", "v"],
        0.0, 0.12,
        0.5, 0.45,
        0.85, 0.72,
        1.0, 0.86,
      ],
    },
  });

  // --- 選択中メッシュの強調（色ではなく輪郭で示す） ---
  map.addLayer({
    id: "mesh-selected",
    type: "line",
    source: "mesh",
    filter: ["==", ["get", "c"], "__none__"],
    paint: {
      "line-color": "#0b0b0b",
      "line-width": 2.2,
    },
  });

  // --- 重畳する点レイヤー（既定は非表示） ---
  map.addSource("demand-points", {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });
  map.addLayer({
    id: "demand-points",
    type: "circle",
    source: "demand-points",
    layout: { visibility: "none" },
    paint: {
      // 種別は色ではなく大きさとツールチップで区別する。
      // カテゴリカル色を 3 スロット以上同時に出さないための設計判断。
      "circle-radius": [
        "interpolate",
        ["linear"],
        ["zoom"],
        10, ["case", ["==", ["get", "layer"], "station"], 4.5, 2],
        16, ["case", ["==", ["get", "layer"], "station"], 13, 5.5],
      ],
      "circle-color": CAT_DEMAND,
      "circle-opacity": 0.82,
      "circle-stroke-width": 1,
      "circle-stroke-color": "#ffffff",
    },
  });

  map.addSource("host-points", {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });
  map.addLayer({
    id: "host-points",
    type: "circle",
    source: "host-points",
    layout: { visibility: "none" },
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, 2.5, 16, 7],
      "circle-color": CAT_HOST,
      "circle-opacity": 0.9,
      // アクアは淡色ベースマップに対して 3:1 を切るため、
      // 白リングで確実に地から分離する（relief rule）。
      "circle-stroke-width": 1.4,
      "circle-stroke-color": "#ffffff",
    },
  });

  // --- 操作 ---
  map.on("click", "mesh-fill", (e) => {
    const f = e.features?.[0];
    if (f) onMeshClick(String(f.properties?.c));
  });
  map.on("mouseenter", "mesh-fill", () => {
    map.getCanvas().style.cursor = "pointer";
  });
  map.on("mouseleave", "mesh-fill", () => {
    map.getCanvas().style.cursor = "";
  });

  const popup = new maplibregl.Popup({
    closeButton: false,
    closeOnClick: false,
    offset: 10,
  });

  for (const id of ["demand-points", "host-points"] as const) {
    map.on("mouseenter", id, (e) => {
      const f = e.features?.[0];
      if (!f) return;
      map.getCanvas().style.cursor = "pointer";
      const p = f.properties ?? {};
      const kind = p.host_kind ?? p.kind ?? "";
      const cap =
        p.capacity != null && p.layer !== "clinic"
          ? `<br>規模 ${Number(p.capacity).toLocaleString("ja-JP")}`
          : "";
      popup
        .setLngLat(e.lngLat)
        .setHTML(`<b>${p.name ?? ""}</b><br>${kind}${cap}`)
        .addTo(map);
    });
    map.on("mouseleave", id, () => {
      map.getCanvas().style.cursor = "";
      popup.remove();
    });
  }

  return {
    map,
    setMeshData: (data) => {
      (map.getSource("mesh") as maplibregl.GeoJSONSource).setData(data);
    },
    setSelected: (meshCode) => {
      map.setFilter("mesh-selected", ["==", ["get", "c"], meshCode ?? "__none__"]);
    },
    toggleLayer: (id, visible) => {
      map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
    },
    flyTo: (lon, lat, zoom = 15.2) => {
      map.flyTo({ center: [lon, lat], zoom, duration: 800 });
    },
  };
}

/** 点レイヤーのデータを差し込む。 */
export function setPointData(
  map: MLMap,
  id: "demand-points" | "host-points",
  data: GeoJSON.FeatureCollection,
): void {
  (map.getSource(id) as maplibregl.GeoJSONSource).setData(data);
}
