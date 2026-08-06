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
  midOrAboveSet,
  recompute,
  renderBanner,
  renderDetail,
  renderFindings,
  renderIntro,
  renderLayerRoles,
  renderLegendNote,
  renderLimitations,
  renderMethodology,
  renderSensitivity,
  renderPresetAgreement,
  renderPresets,
  renderRankingFilter,
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
  const [meta, meshFC, demandFC, hostsFC, noiseFC, parksFC] = await Promise.all([
    loadJSON<Meta>("meta.json"),
    loadJSON<GeoJSON.FeatureCollection>("mesh.geojson"),
    loadJSON<GeoJSON.FeatureCollection>("demand_points.geojson"),
    loadJSON<GeoJSON.FeatureCollection>("hosts.geojson"),
    // 騒音の測定地点。**需要側の点と別ファイルにしてある**——施設ではなく
    // 「調査がそこを測った」という事実で、地図でも白抜きで描き分ける。
    loadJSON<GeoJSON.FeatureCollection>("noise_points.geojson"),
    // 公園。**これも施設ではない**——負荷を下げる要素として数えているだけで、
    // 退避先として評価してはいない。行の順序が mesh の `gp`（添字の配列）と
    // 対応するので、**並べ替えてはいけない。**
    loadJSON<GeoJSON.FeatureCollection>("parks.geojson"),
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
    rankingFilter: "all",
    midOrAbove: new Set(),
    highlight: null,
    // 層ごとの順位は重みに依存しないので、起動時に 1 回だけ作る。
    layerRanks: computeLayerRanks(rows, meta.components),
  };
  state.score = recompute(state);
  state.midOrAbove = midOrAboveSet(state);

  // **既定の重みでは Python の数と一致しなければならない。**
  // `meta.unreachable.mid_or_above` は etl/hosts.py の reach_report が
  // 同じ規則（区内の優先度の中央値・母数は区内の全区画）で数えた値である。
  // 食い違うなら、どちらかが規則から外れている——**黙って違う数を出すのが
  // いちばん悪い**ので、起動時に 1 回だけ突き合わせる。
  if (state.midOrAbove.size !== meta.unreachable.mid_or_above) {
    console.error(
      "[calmgap] 区内で中位以上の到達不可区画が Python と一致しません: " +
        `画面 ${state.midOrAbove.size} / 配信 ${meta.unreachable.mid_or_above}`,
    );
  }

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
      // **重みで動く。** しきい値が区内の優先度の中央値なので、
      // スコアを計算し直したら必ず数え直す。ここで 1 回作って、
      // 見出し数値・順位表・地図の 3 箇所へ同じものを配る。
      state.midOrAbove = midOrAboveSet(state);

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
          // 重ねる層が使う真偽値。**見出しの数と同じ集合**（1 = 区内で
          // 中位以上の到達不可）。重みで動くので毎回書き直す。
          rows[i].mid = state.midOrAbove.has(i) ? 1 : 0;
        }
        handles.setMeshData(meshFC);
      }
      // 破線が囲む範囲（接している上位区画）は重みで変わる。
      // **スコアを計算し直したら必ず引き直す**——重みを動かして上位の
      // 顔ぶれが変わったのに枠だけ残ると、画面が古い隣接を主張し続ける。
      handles.setCluster(clusterOf(state, state.selected));
      renderStat(state);
      // **結論も重みで動く。** 上位の顔ぶれも、到達不可の件数も、
      // スライダーを動かせば変わる。凍らせて置くと、画面の最上部だけが
      // 古い結論を主張し続けることになる（見出し数値で一度やっている）。
      renderFindings(
        state,
        () => applyRankingFilter("all"),
        () => applyRankingFilter("unreachable"),
      );
      // 件数を見出しに出しているので、重みで動いたら押しボタン側も直す。
      renderRankingFilter(state, applyRankingFilter);

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

    // **公園だけは、ここで数えない。** 被覆率は「この区画に重なる公園」から
    // 出しており、その判定は円とセル形状の交差である。ブラウザで矩形近似に
    // すると **9,507 区画のうち 9 区画で件数が食い違った**（うち 6 区画は
    // 0 件かどうかまで変わる）——投影後のセルは軸に平行ではなく、経度で
    // 0.6m ほど傾くため。**Python が出した対応表（`gp`）を引くだけにすれば、
    // 食い違う余地が構造的に無い。**
    if (kind === "green") {
      const gp = (row.gp as number[] | undefined) ?? [];
      const centroid = centroidOf(feature);
      if (!centroid) return null;
      return {
        points: {
          type: "FeatureCollection",
          features: gp
            .map((i) => parksFC.features[i])
            .filter((f): f is GeoJSON.Feature => Boolean(f))
            .map((f) => ({
              ...f,
              properties: { ...(f.properties ?? {}), side: "green", layer: "park" },
            })),
        },
        center: centroid,
        // 半径の円は描かない。**この層に半径という概念が無い**ので、
        // 円を描くと「この距離までを数えた」という嘘になる。
        radiusM: 0,
      };
    }

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
    syncUrl();
    render(false);
  }

  /** 提言リスト・表からの選択。地図側は選んだものが見える位置へ寄せる。 */
  function onPickMesh(meshCode: string): void {
    state.selected = meshCode;
    handles.setSelected(meshCode);

    // **同じ「区画を選ぶ」操作が、押した場所で別の結果になっていた。**
    // 地図をクリックしたときは根拠へ移る（select）のに、順位表の行を
    // 押したときは移らず、**地図が寄る以外に画面が何も変わらない**。
    // 根拠を読むにはタブを自分で押す必要があり、それに気付かなければ
    // 「行を押しても何も起きない」と読める。select と同じ規則にする。
    //
    // **「レイヤー別」タブでは移らない**（select も移らない）。あちらは
    // 層ごとの並びを見ているところで、1 行押すたびに一覧から追い出されると
    // 層の比較そのものができなくなる。移るのは順位表から選んだときだけ。
    if (state.tab === "ranking") {
      state.tab = "selected";
      syncTabs();
    }

    // 接している上位区画があるなら、その全体が入るように寄せる。
    // 1 区画へ寄ると「接する上位区画 11」と書いてあるものが画面から外れる。
    syncUrl();

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

  /**
   * 順位表の絞り込み。**地図の重ねる層と連動させる。**
   *
   * 一覧が 148 区画を並べている隣で、地図がそれを示していないのでは
   * 「どこなのか」がまた分からなくなる（数字だけ大きく出して場所を
   * 見せない状態が長く続いた、というのがこの層を作った理由そのもの）。
   * 切り替えたら重ねる／戻したら外す、とチェックボックスまで同期する。
   */
  function applyRankingFilter(id: AppState["rankingFilter"]): void {
    state.rankingFilter = id;
    // 絞り込んだ一覧を先頭から見せる。前の位置に留まると、
    // 148 件の途中から始まって「切り替わっていない」ように見える。
    state.tab = "ranking";
    syncTabs();

    const box = document.getElementById("toggle-unreachable") as HTMLInputElement;
    const on = id === "unreachable";
    box.checked = on;
    handles.toggleLayer("mesh-unreachable", on);

    renderRankingFilter(state, applyRankingFilter);
    syncUrl();
    render(false);

    // **押した結果が画面の外にあってはいけない。** モバイルでは
    // 順位表が結論カードの 1,000px 以上下にあるので、ボタンを押しても
    // **その場では何も起きていないように見える**（デスクトップは
    // 右のパネルに出ているので気付かなかった）。
    if (window.matchMedia("(max-width: 760px)").matches) {
      document.getElementById("detail")?.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    }
  }
  renderRankingFilter(state, applyRankingFilter);

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

  /* ------------------------------------------------- URL で状態を持ち回る */

  /**
   * **「この区画を見てください」と渡せないのは、この道具にとって本当の穴だった。**
   *
   * 目的は「9,507 区画を、人が話し合える十数区画まで絞る」ことで、
   * その話し合いは会議やメールの上で起きる。**なのに区画を指す方法が
   * 「スクロールして 5339463631 を探してください」しか無かった。**
   *
   * `#c=<メッシュコード>` … その区画を選んで寄る
   * `#f=unreachable`      … 順位表を「徒歩圏に区の公共施設が無い区画」に絞る
   *
   * **`replaceState` で書く。** 区画を選ぶたびに履歴が積まれると、
   * 戻るボタンが「地図を触った回数ぶん」戻ることになる。
   */
  function syncUrl(): void {
    const parts: string[] = [];
    if (state.selected) parts.push(`c=${state.selected}`);
    if (state.rankingFilter !== "all") parts.push(`f=${state.rankingFilter}`);
    const hash = parts.length ? `#${parts.join("&")}` : "";
    if (hash !== window.location.hash) {
      history.replaceState(null, "", `${window.location.pathname}${hash}`);
    }
  }

  function applyUrl(): void {
    const h = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    if (h.get("f") === "unreachable") applyRankingFilter("unreachable");
    const code = h.get("c");
    // **実在しないコードは黙って無視する。** 選択だけ立てて地図が動かないと、
    // 「リンクが壊れている」のか「そういう区画なのか」が分からない。
    if (code && state.rows.some((r) => r.c === code)) onPickMesh(code);
  }

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

  // **重い節はモバイルでだけ畳む。** 左パネルを先頭へ移したので、
  // そのままだとスライダー 8 本と出典一覧が「結論」と「順位表」の間に
  // 挟まり、電話では順位表まで 5,000px 近くスクロールすることになる。
  //
  // **CSS では `open` を外せない**（属性なので）。幅で 1 回だけ判定し、
  // 以後は触らない——後から広げた人が自分で開いた節を勝手に畳まない。
  if (window.matchMedia("(max-width: 760px)").matches) {
    for (const d of document.querySelectorAll<HTMLDetailsElement>("details.heavy")) {
      d.open = false;
    }
  }

  render();

  // **URL の指定は最後に当てる。** 先に当てると、そのあとの初期描画が
  // 選択を上書きしてしまう（applyRankingFilter が render を呼ぶため）。
  applyUrl();

  // **読み込み中の覆いを外す。** 最初のタイルまで描けた時点で外すので、
  // 「白い地図に凡例だけ浮いている」状態を人に見せない。
  //
  // **必ず時間でも外す。** `idle` はタイルの取得に失敗すると来ないことが
  // あり、そのとき覆いが残ると**読み込み中の表示が画面を永久に塞ぐ**
  // ——読み込みを助けるための表示が、いちばん重い障害になる。
  const dismissBoot = (): void => {
    const el = document.getElementById("boot-overlay");
    if (!el) return;
    el.classList.add("is-done");
    window.setTimeout(() => el.remove(), 300);
  };
  handles.map.once("idle", dismissBoot);
  window.setTimeout(dismissBoot, 6000);
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

/**
 * 起動に失敗したときの画面。
 *
 * **失敗の種類で相手が違う。** かつてはどの失敗でも同じ画面を出しており、
 * **生の例外文と `python -m etl.build` という開発者向けのコマンド**が
 * 並んでいた。**WebGL が使えない端末で開いた人**——GPU を切ってある
 * 業務用 PC、古い端末、リモートデスクトップ——には、
 * `{"requestedAttributes":{"antialias":false,…}}` と
 * 「npm install してください」が出ることになる。
 * 行政の窓口で開かれる想定の道具でそれは通らない。
 *
 * データが読めないのは**手元でビルドしていない人**（開発者）の話、
 * WebGL が無いのは**見に来た人**の話で、書くべきことが違う。
 */
boot().catch((err: unknown) => {
  const msg = err instanceof Error ? err.message : String(err);
  const webglFailed =
    /webgl/i.test(msg) ||
    // MapLibre は WebGL が無いと生成時点で投げる。文言は版で変わるので、
    // 「WebGL」の語に頼りきらず生成の失敗も拾う。
    /Failed to initialize|WebGL context/i.test(msg);

  const esc = (s: string) => s.replace(/[<>]/g, "");
  const body = webglFailed
    ? `<h1 style="font-size:18px;margin:0 0 10px">この端末では地図を表示できません</h1>
       <p style="color:#52514e;margin:0 0 14px">
         地図の描画に <b>WebGL</b> を使っています。お使いのブラウザや端末で
         無効になっているか、対応していないようです。
       </p>
       <ul style="color:#52514e;margin:0 0 14px;padding-left:1.2em">
         <li>別のブラウザ（Chrome / Edge / Safari の最新版）で開く</li>
         <li>ブラウザ設定の「ハードウェア アクセラレーション」を有効にする</li>
         <li>リモートデスクトップ経由の場合、手元の端末で開く</li>
       </ul>
       <p style="color:#8a897f;font-size:12px;margin:0">
         算出方法・データ出典・限界の記述は
         <a href="https://github.com/monoimi-mod/calmgap-tokyo" style="color:#3b6fb5">
           リポジトリの docs/</a> にあります（地図が無くても読めます）。
       </p>`
    : `<h1 style="font-size:18px;margin:0 0 10px">データを読み込めませんでした</h1>
       <p style="color:#52514e;margin:0 0 14px">${esc(msg)}</p>
       <p style="color:#8a897f;font-size:12px;margin:0 0 8px">
         手元で動かしている場合は、先に配信データを作ってください。
       </p>
       <pre style="background:#f4f4f1;padding:12px;border-radius:6px;font-size:12px;margin:0">python -m etl.build
cd web &amp;&amp; npm install &amp;&amp; npm run dev</pre>`;

  document.body.innerHTML = `
    <div style="padding:32px;font-family:system-ui,sans-serif;max-width:640px;line-height:1.7">
      ${body}
    </div>`;
  console.error(err);
});
