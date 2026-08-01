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
  recompute,
  renderBanner,
  renderDetail,
  renderLegendNote,
  renderLimitations,
  renderMethodology,
  renderSensitivity,
  renderPresets,
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
  const [meta, meshFC, demandFC, hostsFC] = await Promise.all([
    loadJSON<Meta>("meta.json"),
    loadJSON<GeoJSON.FeatureCollection>("mesh.geojson"),
    loadJSON<GeoJSON.FeatureCollection>("demand_points.geojson"),
    loadJSON<GeoJSON.FeatureCollection>("hosts.geojson"),
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
    tab: "proposals",
    activePreset: "default",
    displayMode: "priority",
  };
  state.score = recompute(state);

  renderBanner(meta);
  renderLegendNote(meta);
  renderMethodology(meta);
  renderLimitations(meta);

  // 感度分析は --sensitivity を付けたビルドでのみ出力される。
  // 無くても地図は動くので、失敗しても起動は止めない。
  loadJSON<Sensitivity>("sensitivity.json")
    .then((s) => renderSensitivity(s, meta))
    .catch(() => renderSensitivity(null));

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
      renderStat(state);
      renderDetail(state, onPickMesh);
    });
  }

  function select(meshCode: string | null): void {
    state.selected = meshCode;
    handles.setSelected(meshCode);
    if (meshCode && state.tab === "proposals") state.tab = "selected";
    syncTabs();
    render(false);
  }

  function onPickMesh(meshCode: string): void {
    const row = state.rows.find((r) => r.c === meshCode);
    state.selected = meshCode;
    handles.setSelected(meshCode);
    if (row) {
      const f = meshFC.features.find(
        (x) => (x.properties as unknown as MeshProps).c === meshCode,
      );
      const c = f && centroidOf(f);
      if (c) handles.flyTo(c[0], c[1]);
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
  const n = ring.length;
  if (!n) return null;
  let x = 0;
  let y = 0;
  for (const [lon, lat] of ring) {
    x += lon;
    y += lat;
  }
  return [x / n, y / n];
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
