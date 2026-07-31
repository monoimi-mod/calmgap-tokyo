/**
 * 地図の初期化とレイヤー更新。
 *
 * ベースマップは国土地理院の淡色地図タイル。
 * オープンデータの作品でベースマップだけ商用サービスに依存するのは筋が通らないし、
 * 淡色地図は情報量が抑えられていて主題図（コロプレス）を載せるのに適している。
 */

import maplibregl, {
  type Map as MLMap,
  type SourceSpecification,
  type StyleSpecification,
} from "maplibre-gl";
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

/** 空の GeoJSON ソース。実データは setMeshData / setPointData が後から差し込む。 */
function emptySource(): SourceSpecification {
  return {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  };
}

/**
 * ベースマップと主題レイヤーを 1 つのスタイル定義にまとめて返す。
 *
 * **主題レイヤーを `addSource` / `addLayer` で後から足してはいけない。**
 * それらはスタイルの読み込み完了前に呼ぶと `Style is not done loading.` を投げ、
 * 例外が boot() まで抜けて「起動できませんでした」の画面になる。
 * 以前ここは `styledata` を待ちつつ 4 秒でタイムアウトする作りだったが、
 * タイムアウト側が**スタイルの状態を確認せず無条件に先へ進んでいた**ため、
 * タイル配信が遅い環境で確実に起動不能になっていた
 * （「タイルが無くても主題図は出す」という意図の真逆）。
 *
 * スタイル定義に最初から含めておけば MapLibre がまとめて適用するので、
 * 競合そのものが発生しない。ベースマップのタイルが 1 枚も落ちてこなくても
 * 優先度コロプレスは描かれる。
 */
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
      mesh: emptySource(),
      "demand-points": emptySource(),
      "host-points": emptySource(),
    },
    layers: [
      { id: "bg", type: "background", paint: { "background-color": "#eceae4" } },
      { id: "gsi", type: "raster", source: "gsi", paint: { "raster-opacity": 1 } },

      // --- 主題図: 優先度コロプレス ---
      {
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
      },

      // --- 選択中メッシュの強調（色ではなく輪郭で示す） ---
      {
        id: "mesh-selected",
        type: "line",
        source: "mesh",
        filter: ["==", ["get", "c"], "__none__"],
        paint: {
          "line-color": "#0b0b0b",
          "line-width": 2.2,
        },
      },

      // --- 重畳する点レイヤー（既定は非表示） ---
      {
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
      },
      {
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
      },
    ],
  };
}

type SourceId = "mesh" | "demand-points" | "host-points";

/**
 * スタイル確定前に届いたデータの保留箱（地図インスタンスごと）。
 *
 * データの投入はスタイルの登録完了より先に来ることがある。
 * 「待ってから入れる」方式は待ち方を間違えると起動不能に直結するので
 * （この画面が実際にそうだった）、**待たずに保留し、確定時に流し込む**。
 */
const pendingData = new WeakMap<MLMap, Map<SourceId, GeoJSON.FeatureCollection>>();

/** 保留していたデータをソースへ流し込む。 */
function flushPending(map: MLMap): void {
  const queued = pendingData.get(map);
  if (!queued) return;
  for (const [id, data] of queued) {
    (map.getSource(id) as maplibregl.GeoJSONSource | undefined)?.setData(data);
  }
  queued.clear();
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

  // タイル取得の失敗でコンソールを埋めない。ベースマップ欠落は致命傷ではない。
  map.on("error", (e) => {
    const msg = String((e as { error?: Error }).error?.message ?? "");
    if (/tile|raster|Failed to fetch|NetworkError/i.test(msg)) return;
    console.warn("[map]", msg || e);
  });

  /**
   * キャンバスをコンテナへ合わせ、対象地域へ画面を合わせ直す。
   *
   * `#map` は position:absolute で、生成時点では寸法が 0 のことがある。
   * その状態だと MapLibre は既定の 400×300 のキャンバスを作り、
   * **コンストラクタの `bounds` もその 0 サイズの画面に対して計算される**。
   * 結果、コンテナだけが広がって地図が左上にしか描かれなかったり、
   * 対象地域が画面外に出て真っ白に見えたりする（どちらも実際に踏んだ）。
   *
   * 寸法が確定してから `fitBounds` をやり直せば、どちらも起きない。
   */
  const fitToArea = (): void => {
    map.resize();
    map.fitBounds(
      [
        [minx, miny],
        [maxx, maxy],
      ],
      { padding: 24, animate: false },
    );
  };

  // 主題レイヤーは baseStyle() に含めてあるので、ここで追加する必要はない。
  // スタイルが確定したら、それまでに届いていたデータを流し込む。
  map.on("style.load", () => {
    flushPending(map);
    fitToArea();
  });

  // 生成後にコンテナの寸法が決まる場合に追従する。
  // MapLibre 自身も追従するはずだが、実測では 400×300 のままだったので明示的に見る。
  if (typeof ResizeObserver !== "undefined") {
    let last = "";
    new ResizeObserver(() => {
      const el = map.getContainer();
      const size = `${el.clientWidth}x${el.clientHeight}`;
      // 0 サイズと、同じ寸法での再通知は無視する（fitBounds の暴発を防ぐ）。
      if (size === last || el.clientWidth === 0 || el.clientHeight === 0) return;
      last = size;
      fitToArea();
    }).observe(map.getContainer());
  }

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
      setSourceData(map, "mesh", data);
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

/**
 * ソースへデータを差し込む。スタイル未確定なら保留し、`style.load` で流す。
 *
 * ここで例外を投げてはいけない。以前は投げていて、スタイルの読み込み遅延が
 * そのまま「起動できませんでした」へ変換されていた。
 * 地図が一瞬空で残るほうが、画面ごと消えるより確実に良い。
 */
function setSourceData(
  map: MLMap,
  id: SourceId,
  data: GeoJSON.FeatureCollection,
): void {
  const src = map.getSource(id) as maplibregl.GeoJSONSource | undefined;
  if (src) {
    src.setData(data);
    return;
  }
  let queued = pendingData.get(map);
  if (!queued) {
    queued = new Map();
    pendingData.set(map, queued);
  }
  queued.set(id, data);
}

/** 点レイヤーのデータを差し込む。 */
export function setPointData(
  map: MLMap,
  id: "demand-points" | "host-points",
  data: GeoJSON.FeatureCollection,
): void {
  setSourceData(map, id, data);
}
