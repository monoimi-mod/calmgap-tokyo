/**
 * 地図の初期化とレイヤー更新。
 *
 * ベースマップは国土地理院の淡色地図タイル。
 * オープンデータの作品でベースマップだけ商用サービスに依存するのは筋が通らないし、
 * 淡色地図は情報量が抑えられていて主題図（コロプレス）を載せるのに適している。
 */

import maplibregl, {
  type ExpressionSpecification,
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

/**
 * ポップアップとカーソルを付ける点レイヤー。
 * **ハイライト層を必ず含めること**——数えた当のものが無反応だと、
 * 「なぜこの点が光っているのか」を画面から辿れない。
 */
const POINT_LAYERS = ["demand-points", "host-points", "highlight-points"] as const;

/**
 * 規模の欄に何を書くか。**層ごとに単位が違う。**
 *
 * `capacity` という 1 つの列に、在籍者数（人）・定員（人）・乗降客数（人/日）が
 * 入っている。集計側は掛け算の材料として同じ列で扱ってよいが、
 * **画面に「規模 131」とだけ出すと、それが何の 131 なのかが消える**。
 * クリニックは全件 1（存在フラグとして 1 を置いてある）なので出さない。
 */
const CAPACITY_LABEL: Record<
  string,
  { label: string; unit: string; assumedNote?: string } | null
> = {
  // **仮の値である理由が層で違う。** 学校は「公表されていない」（国立 3 校は
  // 在籍者数を出していない）。事業所は「そもそも制度上の定員が無い」種別が
  // 多く、**欠測ではない**——訪問系・相談系・共同生活援助がこれに当たる。
  // 同じ「未公表のため」で括ると、後者を「本当はあるのに隠されている数」に
  // 見せてしまう。
  school: {
    label: "在籍者数",
    unit: "人",
    assumedNote: "仮の値（在籍者数が未公表のため既定値）",
  },
  welfare: {
    label: "定員",
    unit: "人",
    assumedNote: "仮の値（届出に定員の記載が無く、種別ごとの既定値）",
  },
  station: { label: "乗降客数", unit: "人/日" },
  clinic: null,
};

/**
 * 点をクリックしたときに出す詳細。
 *
 * **規模と出典を必ず並べる。** この作品は「区画について述べる」道具なので、
 * 施設の点は根拠の材料でしかない。材料である以上、
 * **どの数字がどこから来たか**を、その数字の隣で言えなければならない。
 *
 * **仮定値には必ず印を付ける。** 在籍者数を公表していない国立 3 校には
 * 既定値 150 人が入っており、印が無ければ実数と区別できない。
 */
function pointDetailHtml(p: Record<string, unknown>): string {
  const name = escapeHtml(String(p.name ?? ""));
  const kind = escapeHtml(String(p.host_kind ?? p.kind ?? ""));

  // **騒音の測定点は施設ではない。** 名前の欄に入っているのは住所で、
  // 「そこに何かが在る」のではなく「調査がそこを測った」という事実である。
  // 需要側の点と同じ書式（種別・規模・出典）に流し込むと、
  // 測定点が 768 番目の施設に見える。
  if (p.layer === "noise") {
    const db = p.laeq_db != null ? `${Number(p.laeq_db)} dB (LAeq)` : "—";
    const years = p.years ? String(p.years) : "";
    const n = Number(p.n_years ?? 0);
    // **どちらの調査で測った点かを必ず出す。** 測定点の選ばれ方が
    // 違う 2 つの調査を 1 つの層に混ぜているので、混ぜたことが
    // 画面から見えなくなってはいけない（docs/issues.md A1）。
    const survey = String(p.survey ?? "");
    return `<div class="popup-card"><b>${name}</b>
      <div class="popup-kind">自動車交通騒音の測定地点（施設ではありません）</div>
      <div class="popup-row"><b>等価騒音レベル</b> ${escapeHtml(db)}</div>
      ${
        years
          ? `<div class="popup-row popup-sub">測定年度 ${escapeHtml(years)}${
              n > 1 ? `（${n} 年度の平均）` : ""
            }</div>`
          : ""
      }
      ${survey ? `<div class="popup-row popup-sub">調査 ${escapeHtml(survey)}${surveyNote(survey)}</div>` : ""}
      <div class="popup-role">この点そのものは道路端の値です。区画の値は
        周囲の測定点からの距離重み付き内挿で、測定値ではありません。</div>
      ${
        p.source
          ? `<div class="popup-source">出典: ${escapeHtml(String(p.source))}</div>`
          : ""
      }</div>`;
  }

  // **公園も施設ではない。** 数えているのは「この区画に重なる面積」で、
  // 退避先として評価したものではない（屋外が退避先になるかは当事者に
  // 確かめていない）。**円であることをここで言う**——元データ（P13）は
  // 点で、公園の形は入っていない。面積の等しい円に置き換えて被覆率を
  // 出しており、**その円が地図に描いてあるものそのもの**である。
  if (p.layer === "park") {
    const a = Number(p.area_m2 ?? 0);
    const r = Number(p.r_m ?? 0);
    return `<div class="popup-card"><b>${name}</b>
      <div class="popup-kind">公園（緑・公園被覆を作っているもの）</div>
      <div class="popup-row"><b>面積</b> ${a.toLocaleString("ja-JP")} m²</div>
      <div class="popup-row popup-sub">地図の円は<b>面積の等しい円（半径 ${r} m 相当）</b>です
        — 元データは点で、公園の形は入っていません。</div>
      <div class="popup-role">負荷を下げる要素として数えています。
        <b>退避先として評価したものではありません</b>（屋内かどうかも、
        使えるかどうかも測っていません）。</div>
      ${
        p.source
          ? `<div class="popup-source">出典: ${escapeHtml(String(p.source))}</div>`
          : ""
      }</div>`;
  }

  const spec = CAPACITY_LABEL[String(p.layer ?? "")];
  let size = "";
  if (spec && p.capacity != null) {
    const n = Number(p.capacity).toLocaleString("ja-JP");
    // 仮定値であることを、値と同じ行に書く。注記へ逃がすと読み飛ばされる。
    const mark =
      p.assumed && spec.assumedNote
        ? `<span class="popup-assumed">${spec.assumedNote}</span>`
        : "";
    size = `<div class="popup-row"><b>${spec.label}</b> ${n}${spec.unit}${mark}</div>`;
  }

  const source = p.source
    ? `<div class="popup-source">出典: ${escapeHtml(String(p.source))}</div>`
    : "";

  // ホスト施設だけは「供給側＝徒歩圏の判定に使う」ことを明示する。
  // 需要側の点と同じ見た目で出ると、何のために数えているかが伝わらない。
  const role =
    p.host_kind != null
      ? '<div class="popup-role">既存の公共施設（徒歩圏にあるかの判定に使う）</div>'
      : "";

  return `<div class="popup-card"><b>${name}</b><div class="popup-kind">${kind}</div>${role}${size}${source}</div>`;
}

/**
 * 調査名の後ろに置く一言。**2 つの調査は測っているものではなく、
 * 測る場所の選ばれ方が違う。**
 *
 * 名前（「常時監視」「要請限度」）だけでは、行政の用語を知らない人に
 * 何も伝わらない。ここが伝わらないと、地図に並ぶ点が同じ性質のものに見える。
 */
function surveyNote(survey: string): string {
  const both = survey.includes("・");
  if (both) return "（同じ街区を両方の調査が測っています）";
  if (survey.includes("要請限度")) {
    return "（苦情が出た道路を測る調査。うるさい場所を指しますが、選ばれ方が区に依存します）";
  }
  return "（幹線道路を年度ごとに順に測る系統調査）";
}

function escapeHtml(s: string): string {
  return s.replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

/**
 * 斜線のパターン画像を作る（到達不可の区画に敷く）。
 *
 * **画像ファイルを置かない。** タイルと同じで、外部リソースが 1 つ増えると
 * それが落ちたときの分岐が増える。16×16 を実行時に描けば依存が無い。
 *
 * 色は黒（選択の輪郭と同じ）。**新しい色相を作らない**——橙と緑は
 * 点レイヤーに割り当て済みで、3 つ目の色を出すと「この色は施設の色と
 * 関係があるのか」という問いが生まれる。区別は色ではなく形（斜線）で付ける。
 */
function hatchImage(): ImageData | null {
  const size = 16;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  ctx.strokeStyle = "rgba(11,11,11,0.55)";
  ctx.lineWidth = 2;
  // 端をまたぐ線も引く。1 本だけだとタイルの継ぎ目で斜線が途切れる。
  for (const offset of [-size, 0, size]) {
    ctx.beginPath();
    ctx.moveTo(offset, size);
    ctx.lineTo(offset + size, 0);
    ctx.stroke();
  }
  return ctx.getImageData(0, 0, size, size);
}

/**
 * コロプレスの不透明度。**値だけでなく縮尺でも決める。**
 *
 * 値だけで決めていたため、順位表の行を押して寄った先（zoom 15.2）では
 * 区画 1 つが 60px を超え、**塗りがベースマップを完全に覆っていた**——
 * 道路も駅名も建物も見えず、画面に残るのは色の付いた正方形だけになる。
 * 「江東区 亀戸」と言われた人が、**そこがどこなのかを地図で確かめられない。**
 * 俯瞰する縮尺では塗りそのものが主題なので落とさず、区画が個々に
 * 見える縮尺に入ってから落とす。
 *
 * **`["zoom"]` は入れ子にできない。** 値の傾斜に縮尺の係数を掛ける形
 * （`["*", 値の interpolate, zoom の interpolate]`）は MapLibre が
 * *"zoom" expression may only be used as input to a top-level "step" or
 * "interpolate" expression* として弾き、**スタイルごと読み込みに失敗する**
 * ——mesh レイヤーが出ないだけでなく、後続の `setPointData` が
 * `Style is not done loading.` を投げて起動そのものが止まる。
 * そのため **`["zoom"]` を最上位に置き、各停止点の出力を値の傾斜にする。**
 * 傾斜は同じ形を係数違いで 3 つ書くことになるので、コードで作る。
 */
const MESH_FILL_OPACITY = (() => {
  // 値 → 不透明度の傾斜。scale は縮尺ごとの一律の係数。
  const ramp = (scale: number): ExpressionSpecification =>
    [
      "interpolate",
      ["linear"],
      ["get", "v"],
      0.0, 0.12 * scale,
      0.5, 0.45 * scale,
      0.85, 0.72 * scale,
      1.0, 0.86 * scale,
    ] as ExpressionSpecification;

  return [
    "interpolate",
    ["linear"],
    ["zoom"],
    13, ramp(1.0),
    15, ramp(0.62),
    16.5, ramp(0.38),
  ] as ExpressionSpecification;
})();

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
      // 選択中の区画の「徒歩圏に在るもの」を光らせる層。
      // 数字の隣で同じものを見せるためにある（tools/facility_parity.mjs）。
      "highlight-ring": emptySource(),
      "highlight-points": emptySource(),
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
          // 寄るほど全体を薄くする（MESH_FILL_OPACITY を参照）。
          "fill-opacity": MESH_FILL_OPACITY,
        },
      },

      // --- 徒歩圏に公共施設が 1 件も無い区画（既定は非表示） ---
      //
      // **画面でいちばん大きい数字なのに、それがどこなのかは
      // どこにも出ていなかった。** 1,058 区画と書いてあっても、
      // 地図で見せなければ「多い」以上のことは伝わらない。
      //
      // **表示モード（優先度/需要/負荷）には入れない。** あれは
      // 連続値を色の濃淡に写す枠で、こちらは真偽値である。
      // 同じランプに載せると 0/1 が両端に張り付き、
      // **凡例のグラデーションが「途中の値」があるかのように嘘をつく。**
      // 重ねる層として、優先度の塗りの上から斜線で示す。
      //
      // 斜線にするのは色を増やさないため（橙=需要側・緑=供給側・
      // 黒=選択と隣接で埋まっている）。パターンなら、どの濃さの
      // コロプレスの上でも「印が付いている」と読める。
      {
        id: "mesh-unreachable",
        type: "fill",
        source: "mesh",
        layout: { visibility: "none" },
        // **見出しの数と同じ集合を出す。** かつては `["!", ["has","host"]]`
        // ＝到達不可すべて（1,058 区画）を重ねていたが、見出しに出ている数は
        // **区内で優先度が中位以上のもの（148 区画）**だった。
        // 「地図の『重ねる』で該当区画を表示できます」と書いた隣で、
        // **7 倍の区画が光っていた**——皇居や埋立地まで含めて。
        //
        // `mid` は main.ts が毎回書き込む（1 = 区内で中位以上の到達不可）。
        // **重みで動く**ので、`v` と同じく描画のたびに差し替える値である。
        filter: ["==", ["get", "mid"], 1],
        paint: {
          "fill-pattern": "hatch",
          "fill-opacity": 0.85,
        },
      },

      // --- 徒歩圏の円（ハイライト時のみ） ---
      //
      // **塗りつぶさない。** 中を塗ると「この円の中が均等に効いている」と
      // 読めるが、スコアは距離減衰カーネルで重み付けしている。
      // 円は「どこまでを数えたか」だけを示す。
      {
        id: "highlight-ring",
        type: "line",
        source: "highlight-ring",
        paint: {
          "line-color": "#0b0b0b",
          "line-width": 1.6,
          "line-dasharray": [3, 2],
          "line-opacity": 0.8,
        },
      },

      // --- 選択中の区画と接している上位区画の輪郭 ---
      //
      // **順位表が「接している上位区画 6」と書いているのに、地図には
      // 1 マスしか出ていなかった。** 何と接しているのか（格子の上で
      // 隣り合う上位区画）が画面のどこにも見えず、「選択中のメッシュと
      // 周囲 8 マスを評価している」という誤解を招いていた。
      // 選択中の 1 区画とは別の色・別の太さで描く。
      //
      // **これは「地区」ではない。** 2026-08-03 に地区を廃止したあとも
      // 破線そのものは残してある——囲む対象は変わっておらず、
      // 変わったのは**それを提言の単位として述べるのをやめた**ことだけ。
      // 単位ではなく記述なので、順位表の側では必ず母数を添える。
      {
        id: "mesh-cluster",
        type: "line",
        source: "mesh",
        filter: ["in", ["get", "c"], ["literal", []]],
        paint: {
          // **新しい色を足さない。** 橙とアクアは点レイヤー（需要側の施設・
          // 既存の公共施設）に割り当て済みで、3 つ目のカテゴリカル色を出すと
          // 「この橙の枠は施設の色と関係があるのか」という問いが生まれる。
          // 選択中の 1 区画と同じ黒の、破線・細めで区別する。
          "line-color": "#0b0b0b",
          // 1.4px・不透明度 0.85 では、濃い区画の上でほとんど見えなかった。
          // 選択中の 1 区画（実線 2.2px）と混ざらない範囲で太くする。
          "line-width": 1.9,
          "line-dasharray": [2.2, 1.6],
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
      // --- ハイライトした施設（最前面） ---
      {
        id: "highlight-points",
        type: "circle",
        source: "highlight-points",
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, 3.5, 16, 8],
          // 需要側か供給側かで、既存の点レイヤーと同じ色を使う。
          // ハイライト用に 3 つ目の色を作らない。
          //
          // **騒音の測定点だけは白抜き。** これは施設ではなく
          // 「調査がそこを測った」という事実で、橙（需要側）でも
          // 緑（供給側）でもない。**それでも新しい色相は作らない**——
          // 斜線ハッチと同じ考え方で、区別は色ではなく塗りの有無で付ける
          //（3 つ目の色を出すと「この色は施設の色と関係があるのか」という
          // 問いが生まれる）。輪郭は他と同じ黒。
          //
          // **公園も白抜きにする。** 規則は「施設として評価していないもの」で、
          // 測定点と同じ側である——公園は負荷を下げる要素として数えており、
          // **退避先として評価してはいない**（`config.py` の green）。
          // 橙（需要側）でも緑（供給側）でもないので、ここに入る。
          "circle-color": [
            "case",
            ["any",
              ["==", ["get", "side"], "noise"],
              ["==", ["get", "side"], "green"]],
            "#ffffff",
            ["==", ["get", "side"], "host"],
            CAT_HOST,
            CAT_DEMAND,
          ],
          "circle-opacity": 1,
          "circle-stroke-width": 2,
          "circle-stroke-color": "#0b0b0b",
        },
      },
      // --- 公園の等価円（緑・公園被覆をハイライトしたときだけ） ---
      //
      // **点だけでは、この層の一番の限界が見えない。** 被覆率は
      // 「面積の等しい円」から出しており、**等価半径の中央値は 18m**
      //（3,835 件のうち 3,738 件は 250m 区画より小さい円）。
      // 点で描くと「公園がそこに在る」としか読めず、**その円が区画に
      // どれだけ重なっているのか**——つまり被覆率が何から出た数字なのかが
      // 分からない。実寸の円を描けば、0% の区画の隣に円があることも見える。
      //
      // 半径はメートルなので、縮尺に合わせて画素へ直す。
      // 緯度 35.7° の zoom 20 で 1px ≒ 0.1213m → r_px = r_m × 8.244。
      // `["exponential", 2]` は縮尺 1 段ごとに 2 倍という意味で、
      // これが「地図上の実寸で固定する」書き方になる。
      {
        id: "green-circles",
        type: "circle",
        source: "highlight-points",
        filter: ["==", ["get", "side"], "green"],
        paint: {
          "circle-radius": [
            "interpolate",
            ["exponential", 2],
            ["zoom"],
            10, ["*", ["get", "r_m"], 8.244 / 1024],
            20, ["*", ["get", "r_m"], 8.244],
          ],
          // 中を塗らない。**塗ると「この中が均等に効いている」と読める**が、
          // 効いているのは区画と重なった部分の面積だけである
          //（徒歩圏の円を塗らないのと同じ理由）。
          "circle-opacity": 0,
          "circle-stroke-width": 1,
          "circle-stroke-color": "#0b0b0b",
          "circle-stroke-opacity": 0.55,
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

type SourceId =
  | "mesh"
  | "demand-points"
  | "host-points"
  | "highlight-ring"
  | "highlight-points";

/**
 * スタイル確定前に届いたデータの保留箱（地図インスタンスごと）。
 *
 * データの投入はスタイルの登録完了より先に来ることがある。
 * 「待ってから入れる」方式は待ち方を間違えると起動不能に直結するので
 * （この画面が実際にそうだった）、**待たずに保留し、確定時に流し込む**。
 */
const pendingData = new WeakMap<MLMap, Map<SourceId, GeoJSON.FeatureCollection>>();

/**
 * スタイル確定前に届いた**レイヤー操作**の保留箱。
 *
 * **データ側だけ防いでいて、レイヤー側が素通しだった。**
 * `setFilter` / `setLayoutProperty` はスタイル確定前に呼ぶと
 * `Style is not done loading.` を投げ、それが boot() まで抜けて
 * **画面ごと「データを読み込めませんでした」に化ける。**
 *
 * 人の操作では起こらない（読み込み終わるまで押せない）が、
 * **URL で区画を指定して開くと必ず通る**——`#c=...` を足して初めて
 * 再現した。**押せないから安全、はタイミングの話であって設計ではない。**
 */
const pendingOps = new WeakMap<MLMap, (() => void)[]>();

/** スタイルが確定していれば即実行、まだなら保留する。 */
function whenStyled(map: MLMap, fn: () => void): void {
  if (map.isStyleLoaded()) {
    fn();
    return;
  }
  let q = pendingOps.get(map);
  if (!q) {
    q = [];
    pendingOps.set(map, q);
  }
  q.push(fn);
}

/** 保留していたデータと操作を流し込む。 */
function flushPending(map: MLMap): void {
  const queued = pendingData.get(map);
  if (queued) {
    for (const [id, data] of queued) {
      (map.getSource(id) as maplibregl.GeoJSONSource | undefined)?.setData(data);
    }
    queued.clear();
  }
  // **データより後に流す。** 選択の枠やハイライトは、対象のデータが
  // 入っている前提で filter を当てるものなので、順序が逆だと空振りする。
  const ops = pendingOps.get(map);
  if (ops) {
    for (const fn of ops) fn();
    ops.length = 0;
  }
}

export interface MapHandles {
  map: MLMap;
  setMeshData: (data: GeoJSON.FeatureCollection) => void;
  setSelected: (meshCode: string | null) => void;
  /** 選択中の区画と接している上位区画を輪郭で囲む。空配列で消える。 */
  setCluster: (meshCodes: string[]) => void;
  /**
   * 「徒歩圏に在るもの」を光らせる。null で消える。
   * **数えたものと光らせるものは同じでなければならない**
   * （tools/facility_parity.mjs が全区画で検査する）。
   */
  setHighlight: (h: {
    points: GeoJSON.FeatureCollection;
    center: [number, number];
    radiusM: number;
  } | null) => void;
  toggleLayer: (
    id: "demand-points" | "host-points" | "mesh-unreachable",
    visible: boolean,
  ) => void;
  flyTo: (lon: number, lat: number, zoom?: number) => void;
  /** 接している上位区画までが画面に入るように寄せる。1 区画なら flyTo で足りる。 */
  fitTo: (bounds: [[number, number], [number, number]]) => void;
  /**
   * 一覧から選ばれた 1 点に吹き出しを出す。
   *
   * **地図を寄せるだけでは足りなかった。** 一覧の施設名を押すとその点へ
   * 寄るところまでは動いていたが、250m 四方に 60 件が重なる場所では
   * **寄った先のどれが押した施設なのかが分からない**（点は全部同じ色・
   * 同じ大きさで、名前はホバーしないと出ない）。中身は地図の点を
   * 押したときと**同じ `pointDetailHtml`**——一覧と地図で違うことを
   * 書いたら、どちらが本当なのかという問いが増える。
   */
  openPointPopup: (
    lon: number,
    lat: number,
    props: Record<string, unknown>,
  ) => void;
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
  // **一度きり。** 対象地域へ合わせるのは「まだどこも見ていない」ときだけで、
  // それ以降は視点を触らない。
  let fitted = false;

  const fitToArea = (): void => {
    map.resize();
    if (fitted) return;
    map.fitBounds(
      [
        [minx, miny],
        [maxx, maxy],
      ],
      { padding: 24, animate: false },
    );
    // 寸法が確定していないうちは「合わせた」と見なさない
    //（0 サイズのキャンバスに対して合わせても意味が無い）。
    const el = map.getContainer();
    if (el.clientWidth > 0 && el.clientHeight > 0) fitted = true;
  };

  /**
   * 視点をこちらから動かしたことを記録する。
   *
   * **ResizeObserver が後から視点を上書きしていた。** 起動直後は
   * コンテナの寸法が数フレームかけて確定するので、`fitToArea` が
   * 何度か走る——その間に区画へ寄せても**全域表示へ引き戻される。**
   * URL で区画を指定して開いたときに毎回そうなって発覚したが、
   * **リンク以前の問題**で、ウィンドウをリサイズしただけでも
   * 見ていた場所が失われていた。
   */
  const markMoved = (): void => {
    fitted = true;
  };

  // 主題レイヤーは baseStyle() に含めてあるので、ここで追加する必要はない。
  // スタイルが確定したら、それまでに届いていたデータを流し込む。
  map.on("style.load", () => {
    // **パターンはスタイル確定後にしか登録できない。** 登録前でも
    // レイヤー定義は通り、`fill-pattern` が解決できないぶんだけ
    // 何も描かれない（例外は出ない）。既定で非表示の層なので、
    // ここで失敗しても地図そのものは無事——ベースマップと同じ扱いにする。
    if (!map.hasImage("hatch")) {
      const img = hatchImage();
      if (img) map.addImage("hatch", img);
    }
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
    // 点の上で押したときは区画を選び直さない。**点と区画は重なっている**ので、
    // 素通しにすると「施設を見ようとして押したら選択が飛んで、
    // その施設のハイライトごと消える」ことになる。
    if (map.queryRenderedFeatures(e.point, { layers: [...POINT_LAYERS] }).length)
      return;
    const f = e.features?.[0];
    if (f) onMeshClick(String(f.properties?.c));
  });
  map.on("mouseenter", "mesh-fill", () => {
    map.getCanvas().style.cursor = "pointer";
  });
  map.on("mouseleave", "mesh-fill", () => {
    map.getCanvas().style.cursor = "";
  });

  // ホバーは軽い下見（名前と種別だけ）、クリックで規模・仮定値・出典まで出す。
  // **ハイライトした点にも付ける。** 以前は重畳トグルの 2 層にしか付いておらず、
  // 「徒歩圏に事業所 60 件」と書いた隣で光っている点を押しても何も出なかった
  // ——数えた当のものだけが、画面で唯一無反応だった。
  const hover = new maplibregl.Popup({
    closeButton: false,
    closeOnClick: false,
    offset: 10,
  });
  const detail = new maplibregl.Popup({ closeButton: true, offset: 10 });

  for (const id of POINT_LAYERS) {
    map.on("mouseenter", id, (e) => {
      const f = e.features?.[0];
      if (!f) return;
      map.getCanvas().style.cursor = "pointer";
      const p = f.properties ?? {};
      // 騒音の測定点には種別が無い（名前の欄は住所）。空行を出さず、
      // 「これは施設ではない」が一目で分かる語を置く。
      const sub =
        p.layer === "noise"
          ? `騒音の測定地点${p.laeq_db != null ? ` ${Number(p.laeq_db)} dB` : ""}`
          : String(p.host_kind ?? p.kind ?? "");
      hover
        .setLngLat(e.lngLat)
        .setHTML(
          `<b>${escapeHtml(String(p.name ?? ""))}</b><br>` +
            `${escapeHtml(sub)}` +
            '<br><span class="popup-hint">押すと詳細</span>',
        )
        .addTo(map);
    });
    map.on("mouseleave", id, () => {
      map.getCanvas().style.cursor = "";
      hover.remove();
    });
    map.on("click", id, (e) => {
      const f = e.features?.[0];
      if (!f) return;
      hover.remove();
      detail.setLngLat(e.lngLat).setHTML(pointDetailHtml(f.properties ?? {})).addTo(map);
    });
  }

  return {
    map,
    setMeshData: (data) => {
      setSourceData(map, "mesh", data);
    },
    // **レイヤー操作は whenStyled を通す。** スタイル確定前に呼ぶと
    // `Style is not done loading.` を投げ、boot() まで抜けて画面ごと落ちる。
    // 人が押す経路では起きないが、**URL で区画を指定して開くと必ず通る。**
    setSelected: (meshCode) => {
      whenStyled(map, () => {
        map.setFilter("mesh-selected", ["==", ["get", "c"], meshCode ?? "__none__"]);
      });
    },
    setCluster: (meshCodes) => {
      whenStyled(map, () => {
        map.setFilter("mesh-cluster", ["in", ["get", "c"], ["literal", meshCodes]]);
      });
    },
    setHighlight: (h) => {
      const empty: GeoJSON.FeatureCollection = {
        type: "FeatureCollection",
        features: [],
      };
      if (!h) {
        setSourceData(map, "highlight-points", empty);
        setSourceData(map, "highlight-ring", empty);
        return;
      }
      setSourceData(map, "highlight-points", h.points);
      // **半径 0 のときは円を描かない。** 緑・公園は「この区画に重なる
      // 公園」を数える層で、半径という概念を持たない。0 の円を描くと
      // 選択中の区画の中心に点が落ち、**「ここまでを数えた」という
      // 存在しない距離を主張する**ことになる。
      setSourceData(map, "highlight-ring", {
        type: "FeatureCollection",
        features: h.radiusM > 0 ? [circleFeature(h.center, h.radiusM)] : [],
      });
    },
    toggleLayer: (id, visible) => {
      whenStyled(map, () => {
        map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
      });
    },
    flyTo: (lon, lat, zoom = 15.2) => {
      markMoved();
      map.flyTo({ center: [lon, lat], zoom, duration: 800 });
    },
    fitTo: (bounds) => {
      markMoved();
      // 上限を切らないと 1 区画（250m 四方）で最大ズームまで寄ってしまい、
      // 接している区画がどこまで続いているのかが読めなくなる。
      map.fitBounds(bounds, { padding: 80, maxZoom: 15.2, duration: 800 });
    },
    openPointPopup: (lon, lat, props) => {
      hover.remove();
      detail.setLngLat([lon, lat]).setHTML(pointDetailHtml(props)).addTo(map);
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

/**
 * 半径 m の円をポリゴンで近似する（測地線ではなく、東京での局所近似）。
 *
 * **これは「どこまでを数えたか」を示す飾りで、判定には使わない。**
 * 判定は投影座標での引き算で、Python と同じ（config.PUBLISH_XY_DECIMALS）。
 * ここで多少ズレても件数は変わらない。
 */
function circleFeature(
  center: [number, number],
  radiusM: number,
  steps = 96,
): GeoJSON.Feature {
  const [lon, lat] = center;
  const dLat = radiusM / 110574;
  const dLon = radiusM / (111320 * Math.cos((lat * Math.PI) / 180));
  const ring: [number, number][] = [];
  for (let i = 0; i <= steps; i++) {
    const t = (i / steps) * Math.PI * 2;
    ring.push([lon + dLon * Math.cos(t), lat + dLat * Math.sin(t)]);
  }
  return {
    type: "Feature",
    properties: {},
    geometry: { type: "Polygon", coordinates: [ring] },
  };
}
