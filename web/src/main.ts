/**
 * エントリポイント。データ読み込み → 状態の組み立て → 描画の配線。
 *
 * 状態は単一の AppState に集約し、再描画は render() 一本に通す。
 * スライダー操作のたびに 2,800 区画を再計算するため、
 * 描画は requestAnimationFrame で 1 フレーム 1 回に間引く。
 */

import "maplibre-gl/dist/maplibre-gl.css";
import "./style.css";

import { initMap, setPointData, type MapHandles } from "./map";
import type { Meta, MeshProps, Sensitivity, Weights } from "./types";
import {
  clusterOf,
  computeLayerRanks,
  type HighlightKind,
  recompute,
  renderBanner,
  renderDetail,
  renderIntro,
  renderLayerRoles,
  renderLegendNote,
  renderLimitations,
  renderMethodology,
  renderSensitivity,
  renderPresetAgreement,
  renderPresets,
  renderRankingN,
  renderSliders,
  renderDisplayModes,
  renderStat,
  syncSliders,
  type AppState,
} from "./ui";

const DATA = "./data";

async function loadJSON<T>(name: string): Promise<T> {
  const res = await fetch(`${DATA}/${name}`);
  if (!res.ok) {
    throw new Error(
      `${name} を読み込めません (HTTP ${res.status})。` +
        "先に `python -m etl.build` を実行して web/public/data/ を生成してください。",
    );
  }
  return res.json() as Promise<T>;
}

async function boot(): Promise<void> {
  const [meta, meshFC, demandFC, hostsFC, noiseFC] = await Promise.all([
    loadJSON<Meta>("meta.json"),
    loadJSON<GeoJSON.FeatureCollection>("mesh.geojson"),
    loadJSON<GeoJSON.FeatureCollection>("demand_points.geojson"),
    loadJSON<GeoJSON.FeatureCollection>("hosts.geojson"),
    // 騒音の測定地点。**需要側の点と別ファイルにしてある**——施設ではなく
    // 「調査がそこを測った」という事実で、地図でも白抜きで描き分ける。
    loadJSON<GeoJSON.FeatureCollection>("noise_points.geojson"),
  ]);

  const rows = meshFC.features.map((f) => f.properties as unknown as MeshProps);

  const weights: Weights = {};
  for (const c of meta.components) weights[c.key] = c.weight;

  const state: AppState = {
    meta,
    rows,
    weights,
    score: { demand: new Float64Array(), load: new Float64Array(), priority: new Float64Array(), order: [] },
    selected: null,
    highlightPoints: null,
    tab: "ranking",
    // レイヤー別タブの初期表示。**config.py の定義順の先頭**で、
    // 「いちばん重要な層」という意味ではない（重みの既定値も最大ではない）。
    layerKey: meta.components[0]?.key ?? "",
    activePreset: "default",
    displayMode: "priority",
    rankingN: meta.ranking_default_n,
    highlight: null,
    // 層ごとの順位は重みに依存しないので、起動時に 1 回だけ作る。
    layerRanks: computeLayerRanks(rows, meta.components),
  };
  state.score = recompute(state);

  renderBanner(meta);
  renderIntro(meta);
  renderLayerRoles(meta);
  renderLegendNote(meta);
  renderMethodology(meta);
  renderLimitations(meta);

  // 感度分析は --sensitivity を付けたビルドでのみ出力される。
  // 無くても地図は動くので、失敗しても起動は止めない。
  loadJSON<Sensitivity>("sensitivity.json")
    .then((s) => {
      renderSensitivity(s, meta);
      renderPresetAgreement(s, meta);
    })
    .catch(() => {
      renderSensitivity(null);
      renderPresetAgreement(null, meta);
    });

  const handles: MapHandles = await initMap("map", meta, (meshCode) => {
    select(meshCode);
  });

  setPointData(handles.map, "demand-points", demandFC);
  setPointData(handles.map, "host-points", hostsFC);

  /* ---------------------------------------------------------- 再描画 */

  let frame = 0;
  function render(mapToo = true): void {
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      state.score = recompute(state);

      if (mapToo) {
        // properties を直接書き換えて同じオブジェクトを差し戻す。
        // 2,800 区画程度なら再アップロードのほうが
        // setFeatureState を 2,800 回呼ぶより速い。
        const shown = state.score[state.displayMode];
        for (let i = 0; i < rows.length; i++) {
          rows[i].priority = state.score.priority[i];
          rows[i].demand = state.score.demand[i];
          rows[i].load = state.score.load[i];
          // 地図が塗るのは常に `v`。表示モードはここで差し替える。
          rows[i].v = shown[i];
        }
        handles.setMeshData(meshFC);
      }
      // 破線が囲む範囲（接している上位区画）は重みで変わる。
      // **スコアを計算し直したら必ず引き直す**——重みを動かして上位の
      // 顔ぶれが変わったのに枠だけ残ると、画面が古い隣接を主張し続ける。
      handles.setCluster(clusterOf(state, state.selected));
      renderStat(state);

      // **1 回だけ数えて、地図と一覧の両方へ同じ配列を配る。**
      // 一覧を別に組み立てると「表の件数・地図の点・一覧の行数」が
      // 3 者ばらばらにずれ得るものになる。同じものを配れば、
      // ずれる余地が構造的に無い（tools/facility_parity.mjs は
      // 表と地図の一致を検査していて、一覧はその地図側と同一物）。
      const hl = highlightFor(state);
      state.highlightPoints = hl?.points ?? null;
      renderDetail(state, onPickMesh, applyHighlight, onFocusPoint, applyLayerKey);
      handles.setHighlight(hl);
    });
  }

  /**
   * 「徒歩圏に在るもの」を光らせる。
   *
   * **距離の判定は Python と同じ投影座標（x/y・mx/my）で行う。**
   * 緯度経度から測ると 800m の境界付近で 1〜2 件ズレ、「60 件」と書いた
   * 隣で 59 点しか光らないことになる（tools/facility_parity.mjs）。
   */
  function highlightFor(st: AppState): {
    points: GeoJSON.FeatureCollection;
    center: [number, number];
    radiusM: number;
  } | null {
    if (!st.highlight || !st.selected) return null;
    const row = st.rows.find((r) => r.c === st.selected);
    const feature = meshFC.features.find(
      (f) => (f.properties as unknown as MeshProps).c === st.selected,
    );
    if (!row || !feature) return null;
    const mx = row.mx as number | undefined;
    const my = row.my as number | undefined;
    if (typeof mx !== "number" || typeof my !== "number") return null;

    const kind = st.highlight;
    const isHost = kind === "host";
    // **騒音だけは「徒歩圏に在るもの」ではない。** 光らせるのは
    // この区画の騒音値を作った測定点（IDW の打ち切り 1,500m 以内）で、
    // 0 件ならその値は測定ではなく 23 区の中央値である。
    const isNoise = kind === "noise";
    const src = isHost ? hostsFC : isNoise ? noiseFC : demandFC;
    const layerOf: Record<string, string> = {
      welfare: "welfare",
      school: "school",
      clinic: "clinic",
      station: "station",
    };

    let picked: GeoJSON.Feature[] = [];
    let radiusM = 0;

    if (kind === "station_nearest") {
      // 最寄り 1 駅。Python の nearest_feature と同じ「いちばん近い 1 件」。
      // **これは区画の呼び名で、需要の実数ではない**（半径 1,500m）。
      // 帯域 600m の全駅は下の半径判定の側（kind === "station"）で扱う。
      let best: GeoJSON.Feature | null = null;
      let bestD2 = Infinity;
      for (const f of demandFC.features) {
        const p = f.properties as Record<string, unknown>;
        if (p.layer !== "station") continue;
        const dx = (p.x as number) - mx;
        const dy = (p.y as number) - my;
        const d2 = dx * dx + dy * dy;
        if (d2 < bestD2) {
          bestD2 = d2;
          best = f;
        }
      }
      if (best) picked = [best];
    } else {
      radiusM = meta.fact_radius_m[
        kind as "welfare" | "school" | "clinic" | "host" | "station" | "noise"
      ];
      const r2 = radiusM * radiusM;
      for (const f of src.features) {
        const p = f.properties as Record<string, unknown>;
        if (!isHost && !isNoise && p.layer !== layerOf[kind]) continue;
        const dx = (p.x as number) - mx;
        const dy = (p.y as number) - my;
        if (dx * dx + dy * dy <= r2) picked.push(f);
      }
    }

    const side = isHost ? "host" : isNoise ? "noise" : "demand";
    const centroid = centroidOf(feature);
    if (!centroid) return null;
    return {
      points: {
        type: "FeatureCollection",
        features: picked.map((f) => ({
          ...f,
          properties: { ...(f.properties ?? {}), side },
        })),
      },
      center: centroid,
      radiusM,
    };
  }

  function applyHighlight(kind: HighlightKind | null): void {
    state.highlight = kind;
    render(false);
  }

  /** レイヤー別タブで見る層の切り替え。スコアには触れない。 */
  function applyLayerKey(key: string): void {
    state.layerKey = key;
    render(false);
  }

  /**
   * 一覧の項目から、その施設へ地図を寄せて吹き出しを出す。
   *
   * **区画の選択は変えない。** 変えると、寄せた先の区画が選択され、
   * いま開いている一覧そのものが別の区画のものへ入れ替わる
   *（「近くを見たい」だけの操作で文脈が飛ぶ）。
   *
   * **寄せるだけでは足りなかった。** 250m 四方に 60 件が重なる場所では、
   * 寄った先のどの点が押した施設なのか分からない（点は全部同じ色・
   * 同じ大きさで、名前はホバーしないと出ない）。吹き出しまで出して初めて
   * 「この点です」と言えたことになる。
   *
   * **渡すのは配列の添字で、緯度経度ではない。** 一覧は
   * `state.highlightPoints`（＝地図へ渡したのと同一の配列）から作っており、
   * 添字で引けば**一覧の行・地図の点・吹き出しの中身が同じ 1 件**である
   * ことが構造的に保証される。座標で引き直すと、同じ地点に 2 件ある
   * ときに別の行の中身が出得る（CLAUDE.md「数えたものと光らせるもの」）。
   */
  function onFocusPoint(index: number): void {
    const f = state.highlightPoints?.features[index];
    const g = f?.geometry;
    if (!f || !g || g.type !== "Point") return;
    const [lon, lat] = g.coordinates as number[];
    handles.flyTo(lon, lat, 16.2);
    handles.openPointPopup(lon, lat, (f.properties ?? {}) as Record<string, unknown>);
  }

  function select(meshCode: string | null): void {
    state.selected = meshCode;
    handles.setSelected(meshCode);
    if (meshCode && state.tab === "ranking") state.tab = "selected";
    syncTabs();
    render(false);
  }

  /** 提言リスト・表からの選択。地図側は選んだものが見える位置へ寄せる。 */
  function onPickMesh(meshCode: string): void {
    state.selected = meshCode;
    handles.setSelected(meshCode);

    // 接している上位区画があるなら、その全体が入るように寄せる。
    // 1 区画へ寄ると「接する上位区画 11」と書いてあるものが画面から外れる。
    const cluster = clusterOf(state, meshCode);
    const b = boundsOf(meshFC, cluster.length > 1 ? cluster : [meshCode]);
    if (b) {
      if (cluster.length > 1) handles.fitTo(b);
      else handles.flyTo((b[0][0] + b[1][0]) / 2, (b[0][1] + b[1][1]) / 2);
    }
    render(false);
  }

  /* ---------------------------------------------------------- 操作の配線 */

  renderSliders(meta, weights, (key, value) => {
    state.weights[key] = value;
    // 手で動かした時点でプリセット選択は解除する。
    if (state.activePreset !== "custom") {
      state.activePreset = "custom";
      renderPresets(meta, "custom", applyPreset);
      document.getElementById("preset-note")!.textContent =
        "重みを手動で調整中。プリセットを押すと戻ります。";
    }
    render();
  });

  function applyPreset(id: string): void {
    const preset = meta.presets.find((p) => p.id === id);
    if (!preset) return;
    for (const c of meta.components) {
      state.weights[c.key] = preset.weights[c.key] ?? c.weight;
    }
    state.activePreset = id;
    syncSliders(state.weights, meta);
    renderPresets(meta, id, applyPreset);
    render();
  }

  renderPresets(meta, "default", applyPreset);

  function applyRankingN(n: number): void {
    state.rankingN = n;
    renderRankingN(meta, n, applyRankingN);
    // 地図の破線は「表示中の上位 N のうち連なるもの」なので、
    // 件数を変えたら引き直す（render の中で clusterOf を呼び直している）。
    render(false);
  }
  renderRankingN(meta, state.rankingN, applyRankingN);

  function applyDisplayMode(id: "priority" | "demand" | "load"): void {
    state.displayMode = id;
    renderDisplayModes(id, applyDisplayMode);
    render();
  }
  renderDisplayModes("priority", applyDisplayMode);

  document.getElementById("reset-btn")!.addEventListener("click", () => {
    applyPreset("default");
  });

  document.getElementById("toggle-demand")!.addEventListener("change", (e) => {
    handles.toggleLayer("demand-points", (e.target as HTMLInputElement).checked);
  });
  document.getElementById("toggle-hosts")!.addEventListener("change", (e) => {
    handles.toggleLayer("host-points", (e.target as HTMLInputElement).checked);
  });
  // 見出し数値（区内で中位以上の到達不可 区画）が「どこなのか」を出す層。
  // **数字だけ大きく出して場所を見せない状態が長く続いていた。**
  document.getElementById("toggle-unreachable")!.addEventListener("change", (e) => {
    handles.toggleLayer("mesh-unreachable", (e.target as HTMLInputElement).checked);
  });

  const tabs = [...document.querySelectorAll<HTMLButtonElement>(".tab")];
  function syncTabs(): void {
    for (const t of tabs) {
      t.setAttribute("aria-selected", String(t.dataset.tab === state.tab));
    }
  }
  for (const t of tabs) {
    t.addEventListener("click", () => {
      state.tab = t.dataset.tab as AppState["tab"];
      syncTabs();
      render(false);
    });
  }

  render();
}

/** ポリゴンの外環から重心を求める（メッシュは矩形なので平均で足りる）。 */
function centroidOf(f: GeoJSON.Feature): [number, number] | null {
  const g = f.geometry;
  if (g.type !== "Polygon" || !g.coordinates[0]?.length) return null;
  const ring = g.coordinates[0].slice(0, -1);
  if (!ring.length) return null;
  let x = 0;
  let y = 0;
  for (const [lon, lat] of ring) {
    x += lon;
    y += lat;
  }
  return [x / ring.length, y / ring.length];
}

/** 指定したメッシュ群を囲む矩形。1 件でも複数でも同じ経路で出す。 */
function boundsOf(
  fc: GeoJSON.FeatureCollection,
  meshCodes: string[],
): [[number, number], [number, number]] | null {
  const want = new Set(meshCodes);
  let minx = Infinity;
  let miny = Infinity;
  let maxx = -Infinity;
  let maxy = -Infinity;

  for (const f of fc.features) {
    if (!want.has((f.properties as unknown as MeshProps).c)) continue;
    const g = f.geometry;
    if (g.type !== "Polygon") continue;
    for (const [lon, lat] of g.coordinates[0] ?? []) {
      if (lon < minx) minx = lon;
      if (lat < miny) miny = lat;
      if (lon > maxx) maxx = lon;
      if (lat > maxy) maxy = lat;
    }
  }
  if (!Number.isFinite(minx)) return null;
  return [
    [minx, miny],
    [maxx, maxy],
  ];
}

boot().catch((err: unknown) => {
  const msg = err instanceof Error ? err.message : String(err);
  document.body.innerHTML = `
    <div style="padding:32px;font-family:sans-serif;max-width:640px;line-height:1.7">
      <h1 style="font-size:18px">起動できませんでした</h1>
      <p style="color:#52514e">${msg.replace(/[<>]/g, "")}</p>
      <pre style="background:#f4f4f1;padding:12px;border-radius:6px;font-size:12px">python -m etl.build
cd web &amp;&amp; npm install &amp;&amp; npm run dev</pre>
    </div>`;
  console.error(err);
});
