"""
パイプラインの不変条件を検証する。

    python -m etl.selftest

pytest 等に依存せず単体で走る。地理計算とスコア正規化は
「動いているように見えて静かに間違っている」種類のコードなので、
外形的な出力ではなく数学的な性質そのものを検査する。

TypeScript 側との式の一致は tools/parity_check.mjs が担当する。
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import traceback
import warnings
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from . import fetch
from . import hosts as hostlib
from . import mesh as meshlib
from . import score
from .aggregate import join_by_mesh_code, build_mesh_frame
from .config import ALL_COMPONENTS, DEMAND_COMPONENTS, LOAD_COMPONENTS, AbsoluteScale
from .schema import ZONING_LOAD

_failures: list[str] = []


def check(name: str):
    """テスト関数を登録して実行するデコレータ代わりの薄いラッパ。"""

    def wrap(fn):
        try:
            fn()
            print(f"  ok    {name}")
        except Exception as e:  # noqa: BLE001 — 失敗を集約して最後に報告する
            print(f"  FAIL  {name}: {e}")
            _failures.append(f"{name}: {e}\n{traceback.format_exc()}")
        return fn

    return wrap


@contextlib.contextmanager
def _quiet():
    """正規化の進捗表示を伏せる。テストの合否だけを読めるようにするため。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield buf


# ---------------------------------------------------------------------------
# 地域メッシュ
# ---------------------------------------------------------------------------

# 実在の地点と、その 3 次メッシュコード（JIS X 0410）。
KNOWN_MESHES = [
    ("渋谷駅", 35.6580, 139.7016, "53393586"),
    ("東京駅", 35.6812, 139.7671, "53394611"),
    ("新宿都庁", 35.6896, 139.6917, "53394525"),
]


@check("メッシュコードが既知の地点と一致する")
def _known_codes():
    for name, lat, lon, expected in KNOWN_MESHES:
        got = meshlib.encode(lat, lon, 3)
        assert got == expected, f"{name}: {got} != {expected}"


@check("全次数で、符号化したセルが元の点を含む")
def _containment():
    for _, lat, lon, _ in KNOWN_MESHES:
        for level in (1, 2, 3, 4, 5):
            cell = meshlib.decode(meshlib.encode(lat, lon, level))
            assert cell.min_lat <= lat <= cell.max_lat, f"lat 外れ level={level}"
            assert cell.min_lon <= lon <= cell.max_lon, f"lon 外れ level={level}"


@check("セル重心を再符号化すると同じコードに戻る")
def _roundtrip():
    bbox = (139.60, 35.60, 139.72, 35.68)
    cells = list(meshlib.iter_bbox(bbox, 5))
    assert len(cells) > 500, f"セル数が少なすぎる: {len(cells)}"
    for c in cells:
        lon, lat = c.center
        assert meshlib.encode(lat, lon, 5) == c.code, f"往復不一致: {c.code}"


@check("メッシュ列挙に重複と抜けが無い")
def _grid_complete():
    bbox = (139.60, 35.60, 139.66, 35.64)
    cells = list(meshlib.iter_bbox(bbox, 5))
    codes = [c.code for c in cells]
    assert len(codes) == len(set(codes)), "重複したメッシュがある"

    # 南西端の格子が完全な矩形を成すこと（欠けがあれば積と一致しない）。
    lats = {round(c.min_lat, 8) for c in cells}
    lons = {round(c.min_lon, 8) for c in cells}
    assert len(cells) == len(lats) * len(lons), (
        f"格子に欠けがある: {len(cells)} != {len(lats)}×{len(lons)}"
    )


@check("上位次数への切り出しが整合する")
def _parent():
    code5 = meshlib.encode(35.6580, 139.7016, 5)
    assert meshlib.parent(code5, 3) == "53393586"
    assert meshlib.parent(code5, 1) == "5339"


# ---------------------------------------------------------------------------
# 正規化
# ---------------------------------------------------------------------------


@check("不在(0)は厳密に 0 のまま、正の値だけが順位付けされる")
def _zero_is_absence():
    v = pd.Series([0.0, 0.0, 0.0, 1.0, 5.0, 100.0])
    n = score.percentile_normalize(v, zero_is_absence=True)
    assert (n[:3] == 0.0).all(), f"0 が 0 になっていない: {n.tolist()}"
    assert n.iloc[3] > 0.0, "最小の正値が 0 と区別されていない"
    assert n.iloc[5] == 1.0, f"最大値が 1.0 でない: {n.iloc[5]}"
    assert n.is_monotonic_increasing


@check("連続量は 0〜1 の全域に配分される")
def _continuous():
    v = pd.Series([10.0, 20.0, 30.0, 40.0])
    n = score.percentile_normalize(v, zero_is_absence=False)
    assert n.iloc[0] == 0.0 and n.iloc[-1] == 1.0, n.tolist()


@check("同値は平均順位を共有する")
def _ties():
    v = pd.Series([5.0, 5.0, 5.0, 9.0])
    n = score.percentile_normalize(v, zero_is_absence=True)
    assert n.iloc[0] == n.iloc[1] == n.iloc[2], f"同値が割れている: {n.tolist()}"
    assert n.iloc[3] == 1.0


@check("定数列は順位差を生まない")
def _constant():
    # 連続量として扱う場合、順位に情報が無いので全体を 0 に潰す。
    n = score.percentile_normalize(pd.Series([7.0] * 5), zero_is_absence=False)
    assert (n == 0.0).all(), n.tolist()

    # 不在扱いの場合は平均順位が一律に割り当たる（5 件なら 3/5 = 0.6）。
    # 値そのものは平均順位の定義から決まる任意の定数であって意味を持たない。
    # 重要なのは (a) 全メッシュで同一であること＝偽の順位差を作らないこと、
    # (b) 0 より大きいこと＝掛け算モデルで存在が消えないこと、
    # (c) TypeScript 側と同じ値になること（tools/parity_check.mjs が検証）。
    n = score.percentile_normalize(pd.Series([7.0] * 5), zero_is_absence=True)
    assert n.nunique() == 1, f"定数列から順位差が生まれている: {n.tolist()}"
    assert 0.0 < n.iloc[0] <= 1.0, n.tolist()

    # 0 が混ざれば、そこだけが厳密に 0 として分離される。
    mixed = score.percentile_normalize(
        pd.Series([0.0, 7.0, 7.0]), zero_is_absence=True
    )
    assert mixed.iloc[0] == 0.0 and mixed.iloc[1] == mixed.iloc[2] > 0.0, mixed.tolist()


@check("配信精度への丸めが JavaScript の Math.round と一致する")
def _publish_round_matches_js():
    # ちょうど半分の値。Python の組み込み round は偶数側（0.0312）へ、
    # JavaScript の Math.round は大きい側（0.0313）へ丸める。
    # ここが食い違うと、地図の初期表示とスライダー既定値の色がズレる。
    assert score.publish_round(0.03125) == 0.0313, score.publish_round(0.03125)
    assert score.publish_round(0.00005) == 0.0001, score.publish_round(0.00005)
    assert score.publish_round(0.12344) == 0.1234
    got = score.publish_round(pd.Series([0.03125, 0.5, 0.0]))
    assert list(got) == [0.0313, 0.5, 0.0], list(got)


@check("絶対尺度は基準点を守り、範囲外を切り詰める")
def _absolute_anchors():
    s = AbsoluteScale(label="", lo=55.0, hi=75.0, basis="")
    n = score.absolute_normalize(pd.Series([55.0, 60.0, 65.0, 70.0, 75.0]), s)
    assert list(n) == [0.0, 0.25, 0.5, 0.75, 1.0], list(n)

    # 基準の外側は飽和させる。40dB と 50dB の差にスコア上の意味は無いし、
    # 要請限度を超えた先で線形に伸ばし続ける根拠も無い。
    n = score.absolute_normalize(pd.Series([40.0, 90.0]), s)
    assert list(n) == [0.0, 1.0], list(n)

    try:
        score.absolute_normalize(pd.Series([1.0]), AbsoluteScale("", 1.0, 1.0, ""))
    except ValueError:
        pass
    else:
        raise AssertionError("lo == hi のスケールが素通りした")


@check("絶対尺度は他のメッシュの値に依存しない")
def _absolute_is_context_free():
    """順位化との決定的な違い。ここが崩れると A1 の事故が再発する。

    同じ 70dB のメッシュが、周りが静かか騒がしいかでスコアを変えてはいけない。
    パーセンタイル正規化に戻してしまうと、この検査だけが落ちる。
    """
    s = AbsoluteScale(label="", lo=55.0, hi=75.0, basis="")
    quiet = score.absolute_normalize(pd.Series([70.0, 56.0, 57.0, 58.0]), s)
    loud = score.absolute_normalize(pd.Series([70.0, 73.0, 74.0, 75.0]), s)
    assert quiet.iloc[0] == loud.iloc[0] == 0.75, (quiet.iloc[0], loud.iloc[0])

    # 対比: 同じ入力を順位化すると 1.0 と 0.0 に割れる。
    q = score.percentile_normalize(pd.Series([70.0, 56.0, 57.0, 58.0]), False)
    l = score.percentile_normalize(pd.Series([70.0, 73.0, 74.0, 75.0]), False)
    assert q.iloc[0] == 1.0 and l.iloc[0] == 0.0, (q.iloc[0], l.iloc[0])


@check("狭い範囲のデータが 0〜1 いっぱいへ引き伸ばされない")
def _absolute_preserves_spread():
    """A1 の事故そのものを検査する。

    対象 2 区の内挿騒音は 62.0〜75.8 dB（標準偏差 2.24 dB）に収まる。
    順位化するとこれが σ 0.289 まで広がり、測定点の区依存の偏りが
    そのまま増幅されて区ダミーとして働いていた。
    """
    rng = np.random.default_rng(0)
    # 尺度の内側（55〜75dB）に収めて、切り詰めの影響を除いた関係を検査する。
    db = pd.Series(np.clip(rng.normal(69.7, 2.24, 1000), 62.0, 75.0))
    s = AbsoluteScale(label="", lo=55.0, hi=75.0, basis="")

    absolute = score.absolute_normalize(db, s)
    ranked = score.percentile_normalize(db, False)
    assert absolute.std() < ranked.std() / 2, (
        f"絶対尺度がばらつきを縮めていない: {absolute.std():.3f} vs {ranked.std():.3f}"
    )
    # dB のばらつきが 20dB 幅の尺度上へそのまま縮小して載る（順位化は載せない）。
    assert abs(absolute.std() - db.std() / 20.0) < 1e-12, absolute.std()


@check("用途地域の設計値が正規化で潰れない")
def _zoning_absolute_survives():
    """ZONING_LOAD は用途制限の強さから導いた絶対尺度で、
    「第二種住居地域でカラオケ・パチンコが解禁される」といった段差を
    意図して置いてある。順位化に通すとこの段差が均され、
    第一種低層住居専用地域（設計値 0.05）が 0.285 まで持ち上がっていた。
    """
    zoning = next(c for c in LOAD_COMPONENTS if c.key == "zoning")
    assert zoning.absolute is not None, "用途地域が絶対尺度になっていない"

    # 実際の分布に近い構成（低層住専が過半）を作る。
    raw = pd.Series([ZONING_LOAD[1]] * 55 + [ZONING_LOAD[3]] * 22 + [ZONING_LOAD[9]] * 7)
    out = score.normalize_components(pd.DataFrame({"zoning": raw}), (zoning,))
    n = out["n_zoning"]

    assert n.iloc[0] == ZONING_LOAD[1], f"第一種低層が {n.iloc[0]}（設計値 0.05）"
    assert n.iloc[-1] == ZONING_LOAD[9], f"商業地域が {n.iloc[-1]}（設計値 1.00）"
    # 段差の比が保存されること。順位化すると 0.285 対 0.966 で 3.4 倍まで縮む。
    assert n.iloc[-1] / n.iloc[0] == ZONING_LOAD[9] / ZONING_LOAD[1]


@check("データの層は全部が「スコアに入る／入らない」に分類されている")
def _layer_roles_cover_all():
    """画面は「10 層のうち 8 層がスコアに入る」と書く。

    **この数は画面に書いてはいけない**——層が増えたときに、
    その文だけが黙って古くなる。実際「実データ 10/10 レイヤー」と
    「評価に使う 8 つのレイヤー」が対応の無いまま並んでいて、
    外部から「8 なの 10 なの、意味が分からない」と指摘された。

    `build._layer_roles` が分類漏れで止まることを確かめる。
    """
    from . import build
    from .config import SUPPORT_LAYERS

    layers = {c.layer for c in ALL_COMPONENTS} | set(SUPPORT_LAYERS)
    provenance = {k: "real" for k in layers}

    roles = build._layer_roles(provenance)
    assert len(roles) == len(layers), f"層の数が合わない: {len(roles)} != {len(layers)}"
    assert sum(1 for r in roles if r["role"] == "score") == len(ALL_COMPONENTS)
    assert all(r["note"] for r in roles if r["role"] == "support"), (
        "スコアに入らない層に「何をしている層か」が書かれていない"
    )

    # 分類していない層が来たら止まる。
    try:
        build._layer_roles({**provenance, "brand_new": "real"})
    except ValueError:
        pass
    else:
        raise AssertionError("分類漏れの層があるのに止まらなかった")

    # 分類しているのに存在しない層があっても止まる（層名が変わった場合）。
    dropped = dict(provenance)
    dropped.pop(next(iter(SUPPORT_LAYERS)))
    try:
        build._layer_roles(dropped)
    except ValueError:
        pass
    else:
        raise AssertionError("存在しない層を分類しているのに止まらなかった")


@check("騒音: 「測定点 0 件」と「観測圏外」が食い違えば止まる")
def _noise_count_matches_missing():
    """画面は騒音の行に「内挿に使った測定点 N 点」と書き、その N 点を光らせる。

    **N が 0 のとき、その区画の騒音は測定ではなく 23 区の中央値である**——
    画面はそう書く。両者がずれると、画面は「補完値です」と書きながら
    測定点を光らせる（またはその逆）ことになる。ずれ得る経路は
    座標の丸めだけ（IDW は丸めない座標・件数は配信する丸めた座標）。
    """
    from . import build

    mesh = build.gpd.GeoDataFrame(
        {"mesh_code": ["a", "b"], "f_noise_n": [3, 0]},
        geometry=[None, None],
    )
    noise = pd.Series([65.0, float("nan")])
    build._check_noise_point_count(mesh, noise)  # 一致していれば通る

    mesh_bad = build.gpd.GeoDataFrame(
        {"mesh_code": ["a", "b"], "f_noise_n": [3, 1]},
        geometry=[None, None],
    )
    try:
        build._check_noise_point_count(mesh_bad, noise)
    except ValueError:
        pass
    else:
        raise AssertionError("測定点があるのに観測圏外、という状態で止まらなかった")


@check("出典名に、出典どうしの区切り文字が入っていない")
def _source_labels_have_no_separator():
    """1 つの地物が複数の出典を持つとき、名前を区切りでつないで持ち回る
    （騒音は同じ地点を令和5年度＝都の資料と令和6年度＝環境GIS＋が測っている）。

    **区切りが出典名の中に入っていると、分割したときに名前が壊れる。**
    実際に踏んだ——「・」で区切っていたが、
    「常時監視・要請限度測定地点」という名前自体に「・」が入っており、
    出典一覧の件数がレイヤー全体の件数のまま直らなかった。
    """
    from .config import SOURCES, SOURCE_JOIN

    for s in SOURCES.values():
        assert SOURCE_JOIN not in s.label, f"{s.key} の label に {SOURCE_JOIN!r}"
        for alias in s.aliases:
            assert SOURCE_JOIN not in alias, f"{s.key} の alias に {SOURCE_JOIN!r}"


@check("旧 label で保存された行も「入れ直した出典」として扱う")
def _append_matches_source_aliases():
    """`--append` の「古い行を捨てる」判定が `Source.aliases` を見ているか。

    **見ていないと、再正規化が黙って効かなくなる。** 公共施設一覧を改名して
    旧名を `aliases` に入れた結果、こうなっていた（2026-08-07 に発覚）:

      1. 古い 1,082 行が「別の出典」として残る
      2. 新しい 1,207 行は「同名かつ 100m 以内」で全部重複として消える
      3. 出力は改名前と同一。**「1,541件を書き出した」と成功と表示される**

    除外パターンを足しても効かないので、供給側が過大なまま静かに固定される。
    **同じ事故を防ぐために入れた処理が、改名でもう一度開いていた。**
    """
    from .config import SOURCES, current_source_label

    # 実際に alias を持つ出典が在ること（無いとこの検査は空振りする）
    aliased = [s for s in SOURCES.values() if s.aliases]
    assert aliased, "alias を持つ出典が 1 つも無い。この検査は空振りしている"

    for s in aliased:
        for old in s.aliases:
            assert current_source_label(old) == s.label, (
                f"{s.key}: 旧 label {old!r} が現在の label へ寄らない"
            )
            # `--append` が使う経路そのもの。旧 label で保存された行が
            # 「入れ直した出典」に当たらなければ、古い行が残る。
            assert current_source_label(old) in {s.label}, f"{s.key}: stale 判定が効かない"


@check("「照合できない理由」は、照合できる出典に書かれていない")
def _unverifiable_only_without_hint():
    """`Source.unverifiable` は「そのページで試したが裏を取れなかった」理由。

    **`file_hint` があるなら、それは照合できているということ**なので、
    理由が書いてあっても `link_check` は読まない——**誰も読まない文字列が
    「検査してあります」の顔で残る**。理由を書いた出典が実際に
    照合されていないことを、ここで固定する。

    逆向き（理由が無いのに file_hint も無い）は**未着手**であって誤りでは
    ないので落とさない。実際 2026-08-07 まで都教委の在籍者数がそれで、
    「一覧ページにファイル名は出ていない」という**確かめずに書いた理由**が
    コメントに残っていた（ページには載っていた）。
    """
    from .config import SOURCES

    for s in SOURCES.values():
        if not s.unverifiable:
            continue
        assert not s.file_hint, (
            f"{s.key}: file_hint があるのに unverifiable が書いてある。"
            "照合できているなら理由は要らない（読まれない文字列になる）"
        )
        assert s.unverifiable.strip() == s.unverifiable and len(s.unverifiable) > 10, (
            f"{s.key}: unverifiable が理由になっていない（{s.unverifiable!r}）"
        )


@check("絶対尺度の層は基準の出典を必ず持つ")
def _absolute_requires_basis():
    """lo / hi をどの法令から取ったか書けない層に絶対尺度を使うと、
    順位化の恣意性を別の恣意性に置き換えただけになる。
    """
    for c in ALL_COMPONENTS:
        if c.absolute is None:
            continue
        assert c.absolute.basis.strip(), f"{c.key} の absolute.basis が空"
        assert c.absolute.label.strip(), f"{c.key} の absolute.label が空"
        assert c.absolute.lo < c.absolute.hi, f"{c.key} の lo < hi が成り立たない"


# ---------------------------------------------------------------------------
# 合成
# ---------------------------------------------------------------------------


def _toy_frame(n: int = 40) -> pd.DataFrame:
    rng = np.random.default_rng(1)
    df = pd.DataFrame({"mesh_code": [f"{5339358600 + i}" for i in range(n)]})
    for c in ALL_COMPONENTS:
        df[c.key] = rng.random(n)
    return df


@check("需要が 0 のメッシュは優先度も 0 になる（掛け算モデルの核心）")
def _multiplicative():
    df = _toy_frame()
    # 先頭 5 件の需要側レイヤーをすべて 0 にする＝「誰も通わない場所」
    for c in DEMAND_COMPONENTS:
        df.loc[:4, c.key] = 0.0
    out = score.compose(score.normalize_components(df))
    assert (out.loc[:4, "demand"] == 0.0).all(), "需要が 0 になっていない"
    assert (out.loc[:4, "priority_raw"] == 0.0).all(), (
        "需要 0 のメッシュに優先度が付いている。足し算になっていないか確認すること"
    )
    assert out["priority_raw"].max() > 0.0, "全メッシュが 0 になっている"


@check("レイヤーの並び順を変えてもスコアは 1 ビットも変わらない")
def _order_independent():
    """**画面の並び順が数値に触れてはいけない。**

    `config.py` のタプルの順は「左のスライダー・算出方法・内訳をどの順で
    出すか」という**表示の決めごと**で、スコアの定義ではない。
    ところが浮動小数の加算は順序で最後の桁が変わるので、素朴に定義順で
    足すと**並べ替えただけでスコアが動く**。実際 2026-08-04 に需要側の
    並びを変えたとき、29 区画の優先度が 4 桁目でずれて 18 区画の順位が
    動いた（`score._weighted_sum` はキー順に足すようにして解消）。

    ここが落ちたら、直すのは並び順ではなく `_weighted_sum` の側である。
    """
    df = score.normalize_components(_toy_frame())
    base = score.compose(df)

    # 定義順を逆さにして同じ計算をする。表示順が変わっただけの状態。
    import etl.score as score_mod

    orig_d, orig_l = score_mod.DEMAND_COMPONENTS, score_mod.LOAD_COMPONENTS
    try:
        score_mod.DEMAND_COMPONENTS = tuple(reversed(orig_d))
        score_mod.LOAD_COMPONENTS = tuple(reversed(orig_l))
        flipped = score.compose(df)
    finally:
        score_mod.DEMAND_COMPONENTS, score_mod.LOAD_COMPONENTS = orig_d, orig_l

    for col in ("demand", "load", "priority_raw"):
        assert (base[col].to_numpy() == flipped[col].to_numpy()).all(), (
            f"並び順を変えたら {col} が変わった。"
            "_weighted_sum がキー順に足しているか確認すること"
        )


@check("片側の重みを全て 0 にしても優先度が消えない")
def _neutral_side():
    df = score.normalize_components(_toy_frame())
    weights = {c.key: (0.0 if c.side == "load" else c.weight) for c in ALL_COMPONENTS}
    out = score.compose(df, weights)
    assert (out["load"] == 1.0).all(), "負荷側が中立(1.0)になっていない"
    assert out["priority_raw"].max() > 0.0, "優先度が全滅している"


@check("減点レイヤーは優先度を下げる向きに効く")
def _negative_sign():
    df = _toy_frame()
    df["green"] = 0.0
    base = score.compose(score.normalize_components(df))["load"].mean()
    df["green"] = np.linspace(0.1, 1.0, len(df))
    with_green = score.compose(score.normalize_components(df))
    # 緑被覆が最大のメッシュは、最小のメッシュより負荷が低くなるはず。
    lo = with_green.loc[with_green["green"].idxmax(), "load"]
    hi = with_green.loc[with_green["green"].idxmin(), "load"]
    assert lo < hi, f"緑地が負荷を下げていない: {lo} >= {hi}"
    assert base == base  # 基準値の算出自体が例外を投げないことの確認


# ---------------------------------------------------------------------------
# メッシュコード結合
# ---------------------------------------------------------------------------


@check("粗いメッシュ統計を細かいメッシュへ按分しても総量が保存される")
def _population_split():
    bbox = (139.70, 35.65, 139.72, 35.67)
    mesh_gdf = build_mesh_frame(bbox, 5)

    # 3 次メッシュ(1km)の人口表を作る。
    codes3 = sorted({c[:8] for c in mesh_gdf["mesh_code"]})
    table = pd.DataFrame({"mesh_code": codes3, "pop": [16000.0] * len(codes3)})

    got = join_by_mesh_code(mesh_gdf, table, "mesh_code", "pop")

    # 5 次メッシュは 3 次メッシュを 16 分割したもの。1 セルあたり 1/16。
    assert np.allclose(got.to_numpy(), 16000.0 / 16.0), (
        f"按分後の値が想定と違う: {sorted(set(got.round(3)))}"
    )

    # 研究領域が 3 次メッシュを完全に覆う場合、総量が保存されていること。
    covered = {c[:8] for c in mesh_gdf["mesh_code"]}
    full = [c for c in covered if sum(1 for m in mesh_gdf["mesh_code"] if m[:8] == c) == 16]
    if full:
        total = got[[m[:8] in full for m in mesh_gdf["mesh_code"]]].sum()
        assert np.isclose(total, 16000.0 * len(full)), (
            f"総量が保存されていない: {total} != {16000.0 * len(full)}"
        )


# ---------------------------------------------------------------------------
# 感度分析の速い経路が、本番と同じ数字を出すこと
# ---------------------------------------------------------------------------
#
# 重み以外の固定値を揺さぶる感度分析は、build_mesh_table を毎回呼ぶ代わりに
# カーネルの三つ組と面積シェアを使い回す（1 試行 24 秒 → 20 ミリ秒）。
# **速い経路が本番と違う数字を出していたら、そこで測った「変わりません」が
# 一番信用できないものになる。** ここはその一致を検査する。


@check("カーネルの三つ組から組み立てた集計が、本番の集計と一致する")
def _kernel_triplets_match_points_to_mesh():
    from .aggregate import apply_kernel, kernel_triplets, points_to_mesh
    from shapely.geometry import Point

    rng = np.random.default_rng(0)
    mesh_gdf = build_mesh_frame((139.69, 35.65, 139.72, 35.67), 5)
    n = 40
    pts = gpd.GeoDataFrame(
        {"demand_value": rng.uniform(1, 100, n)},
        geometry=[
            Point(139.685 + rng.uniform(0, 0.04), 35.645 + rng.uniform(0, 0.03))
            for _ in range(n)
        ],
        crs="EPSG:4326",
    )
    direct = points_to_mesh(mesh_gdf, pts, "demand_value", 800.0).to_numpy()
    fast = apply_kernel(
        kernel_triplets(mesh_gdf, pts, 800.0),
        pts["demand_value"].to_numpy(),
        len(mesh_gdf),
    )
    assert np.allclose(direct, fast, atol=1e-12), (
        f"最大乖離 {np.abs(direct - fast).max():.3e}"
    )
    # 打ち切りの外が三つ組に入っていないこと（入ると重みが 0 でも数が膨れる）。
    _, _, w = kernel_triplets(mesh_gdf, pts, 800.0)
    assert (w > 0).all(), "重み 0 の組が三つ組に残っている"


@check("面積シェアに値を掛けると、面積加重平均と一致する")
def _area_shares_match_weighted_mean():
    from .aggregate import polygon_area_shares, polygon_area_weighted_mean
    from shapely.geometry import box

    mesh_gdf = build_mesh_frame((139.70, 35.65, 139.71, 35.66), 5)
    # 縦に 3 分割した帯。1 メッシュが複数区分に跨る形にする。
    polys = gpd.GeoDataFrame(
        {"zoning_code": [1, 7, 12], "zoning_load": [0.05, 0.60, 0.90]},
        geometry=[
            box(139.698, 35.648, 139.704, 35.662),
            box(139.704, 35.648, 139.708, 35.662),
            box(139.708, 35.648, 139.714, 35.662),
        ],
        crs="EPSG:4326",
    )
    want = polygon_area_weighted_mean(
        mesh_gdf, polys, "zoning_load", default=0.30
    ).to_numpy()
    classes, shares = polygon_area_shares(mesh_gdf, polys, "zoning_code")
    loads = dict(zip(polys["zoning_code"], polys["zoning_load"]))
    vec = np.array([loads[c] for c in classes], dtype=float)
    got = np.where(shares.sum(axis=1) > 0, shares @ vec, 0.30)
    assert np.allclose(want, got, atol=1e-9), (
        f"最大乖離 {np.abs(want - got).max():.3e}"
    )


@check("固定値を差し替えない感度分析は、本番の生値と完全に一致する")
def _fixed_value_model_reproduces_build():
    from . import build as buildlib
    from . import sensitivity
    from .config import BANDWIDTH_M
    from .schema import ASSUMED_CAPACITY, WELFARE_DEMAND_WEIGHT

    with _quiet():
        layers, _ = buildlib.load_layers(live=False)
        mesh_gdf = buildlib.build_mesh_table(layers, 4)
        model = sensitivity.FixedValueModel(mesh_gdf, layers)

        # 1. 何も渡さないとき。
        raw = model.raw_columns()
        # 2. 既定値をそのまま「差し替え」たとき。作り直しの経路を通る。
        rebuilt = model.raw_columns(
            welfare_weights=dict(WELFARE_DEMAND_WEIGHT),
            assumed_capacities=dict(ASSUMED_CAPACITY),
            zoning_loads=dict(ZONING_LOAD),
            bandwidths=dict(BANDWIDTH_M),
            idw={"smoothing_m": 50.0, "max_distance_m": 1500.0},
        )

    for c in ALL_COMPONENTS:
        want = mesh_gdf[c.key].to_numpy(dtype=float)
        assert np.allclose(raw[c.key].to_numpy(), want, rtol=1e-12, atol=1e-9), (
            f"{c.key}: 何も差し替えないのに生値がずれる"
        )
        # 帯域の作り直しは距離の再計算を通るので、丸め誤差の分だけ緩める。
        assert np.allclose(rebuilt[c.key].to_numpy(), want, rtol=1e-6, atol=1e-6), (
            f"{c.key}: 既定値で作り直すと元に戻らない "
            f"（最大乖離 {np.abs(rebuilt[c.key].to_numpy() - want).max():.3e}）"
        )


@check("「仮定員を落とす」は空振りしない（模擬データでも仮定員の行が在る）")
def _drop_assumed_actually_changes_demand():
    from . import build as buildlib
    from . import sensitivity

    # 模擬データに仮定員の行が無いと、A3 の最重要シナリオが
    # **模擬モードでだけ「100% 維持」と表示される**。
    with _quiet():
        layers, _ = buildlib.load_layers(live=False)
        mesh_gdf = buildlib.build_mesh_table(layers, 4)
        model = sensitivity.FixedValueModel(mesh_gdf, layers)
        dropped = model.raw_columns(drop_assumed=True)

    assert model.n_assumed > 0, "模擬データに仮定員の行が 1 件も無い"
    base = model.base_raw["welfare_capacity"].to_numpy()
    got = dropped["welfare_capacity"].to_numpy()
    assert got.sum() < base.sum(), "仮定員を落としたのに需要が減っていない"
    assert (got <= base + 1e-9).all(), "落としたのに増えたメッシュがある"


# ---------------------------------------------------------------------------
# 列の特定（決め打ちが外れたときに止まること）
# ---------------------------------------------------------------------------

# A29 で実際に起きた事故——列名の決め打ちが外れ、全件 NaN のまま既定値が
# 乗り、**ビルドもテストも成功と表示された**——を他のレイヤーで再発させない。
# ここで検査するのは「正しく動くこと」ではなく **間違いが黙って通らないこと**。
#
# 実データが手元に無くても検査できる。列名を故意にずらした小さな入力を作り、
# 正規化が例外で止まるかどうかを見ればよい。

# 渋谷駅前。研究領域（行政界から導出）の内側であることが前提。
_INSIDE = (35.6580, 139.7016)


def _tmp_geojson(tmp: Path, name: str, rows: list[dict]) -> Path:
    """点データの GeoJSON を書き出す。lat/lon を除いた列がそのまま属性になる。"""
    from shapely.geometry import Point

    gdf = gpd.GeoDataFrame(
        [{k: v for k, v in r.items() if k not in ("lat", "lon")} for r in rows],
        geometry=[Point(r["lon"], r["lat"]) for r in rows],
        crs="EPSG:4326",
    )
    path = tmp / name
    gdf.to_file(path, driver="GeoJSON")
    return path


def _tmp_csv(tmp: Path, name: str, rows: list[dict]) -> Path:
    path = tmp / name
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8")
    return path


def _raises(fn, *, contains: str) -> str:
    """fn が例外で止まり、その説明に手掛かりが入っていることを確かめる。"""
    try:
        with _quiet():
            fn()
    except Exception as e:  # noqa: BLE001 — 例外の型ではなく止まることが要件
        msg = str(e)
        assert contains in msg, f"説明に {contains!r} が無い:\n{msg}"
        return msg
    raise AssertionError("止まらずに通ってしまった（既定値で埋めていないか確認）")


def _p29_rows(n_special: int = 3, n_other: int = 4) -> list[dict]:
    """実データ第2.0版の形をした P29 の行を作る。

        P29_003 学校分類コード（16012 = 特別支援学校）/ P29_004 名称
        P29_006 設置者コード（1 国立 / 2 都道府県立 / 3 市区町村立 / 4 私立）

    設置者は「国立・私立が都教委の調査に載らないこと」と
    「名寄せの失敗」を切り分けるために要る。名称の 3 グループが
    3 つの別々のコードに割れることが列を特定する条件なので、
    都立だけの行では成立しない——実データと同じ 4 種を混ぜておく。
    """
    lat, lon = _INSIDE
    rows = [
        {
            "lat": lat,
            "lon": lon + i * 0.002,
            "P29_003": "16012",
            "P29_004": f"東京都立第{i}特別支援学校",
            "P29_006": "2",
        }
        for i in range(n_special)
    ]
    rows += [
        {
            "lat": lat + 0.002,
            "lon": lon + i * 0.002,
            "P29_003": "16001",
            "P29_004": f"区立第{i}小学校",
            "P29_006": "3",
        }
        for i in range(n_other)
    ]
    return rows


def _p29_mixed_founders() -> list[dict]:
    """設置者が 4 種そろった特別支援学校の行（23 区の実際の内訳と同じ形）。

    都立 2・区立 1・国立 1・私立 1。国立と私立は東京都教育委員会の
    在籍者数調査に載らないので、突き合わせの結果が 3 通りに分かれる。
    """
    lat, lon = _INSIDE
    spec = [
        ("東京都立城南特別支援学校", "2"),
        ("都立城東特別支援学校", "2"),  # 「東京都立」と「都立」は実データでも混在
        ("新宿区立新宿養護学校", "3"),
        ("筑波大学附属大塚特別支援学校", "1"),
        ("旭出学園", "4"),
    ]
    rows = [
        {
            "lat": lat,
            "lon": lon + i * 0.002,
            "P29_003": "16012",
            "P29_004": name,
            "P29_006": founder,
        }
        for i, (name, founder) in enumerate(spec)
    ]
    # 分類コードの列は「2〜30 種のコードを持つ列」であることも条件なので、
    # 特別支援学校以外の学校を混ぜておく（実データも小中高が同居している）。
    rows += [
        {
            "lat": lat + 0.002,
            "lon": lon + i * 0.002,
            "P29_003": "16001",
            "P29_004": f"区立第{i}小学校",
            "P29_006": "3",
        }
        for i in range(2)
    ]
    return rows


def _sped_enrollment_csv(tmp: Path, rows: list[tuple[str, str, int]], total: int | None = None) -> Path:
    """都教委の在籍者数 CSV と同じ形（学校番号・設置者・学校名・在籍者数/総数）。

    1 行 = 学校 × 障害種別なので、同じ学校番号を 2 行に分けられる。
    """
    path = tmp / "zaiseki.csv"
    lines = ["学校番号,設置者,障害種別,学校名,在籍者数/総数"]
    for sid, name, n in rows:
        lines.append(f"{sid},東京都,知的,{name},{n}")
    if total is None:
        total = sum(n for _, _, n in rows)
    lines.append(f"合計,,,,{total}")
    path.write_text("\n".join(lines) + "\n", encoding="cp932")
    return path


@check("P29: 学校名の列を特定できなければ止まる（分類コードの裏取りができない）")
def _p29_requires_name():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        # 学校名らしい列が無い。分類コードだけがあっても、それが本当に
        # 分類コードなのかを確かめる手掛かりが無くなる。
        rows = [
            {"lat": lat, "lon": lon + i * 0.002, "P29_003": "16012", "P29_002": f"A{i}"}
            for i in range(4)
        ]
        path = _tmp_geojson(tmp, "p29.geojson", rows)
        _raises(lambda: fetch.normalize_schools(path), contains="学校名")


@check("P29: 学校分類コードの列を特定できなければ止まる")
def _p29_requires_class_code():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        # 名称は特別支援学校だと分かるのに、分類コードの列が無い。
        path = _tmp_geojson(
            tmp,
            "p29.geojson",
            [
                {
                    "lat": lat,
                    "lon": lon + i * 0.002,
                    "P29_004": f"都立第{i}特別支援学校",
                    "生徒数": 200 + i * 50,
                }
                for i in range(3)
            ],
        )
        _raises(lambda: fetch.normalize_schools(path), contains="学校分類コード")


@check("P29: 版で列の意味が入れ替わっても、名称で裏を取って正しい列を選ぶ")
def _p29_picks_class_column_by_name():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        # 第1.1版の形。P29_003 は施設種別詳細（盲16008/聾16009/養護16010）で、
        # 学校分類コードは P29_004。列名の順だけで選ぶと P29_003 を掴み、
        # **特別支援学校が数分の一に減る**（実データでは 67 件 → 9 件）。
        rows = [
            {
                "lat": lat,
                "lon": lon + i * 0.002,
                "P29_003": code,
                "P29_004": "16012",
                "P29_005": f"都立第{i}特別支援学校",
            }
            for i, code in enumerate(["16008", "16009", "16010", "16010", "16012"])
        ]
        rows += [
            {
                "lat": lat + 0.002,
                "lon": lon + i * 0.002,
                "P29_003": "16001",
                "P29_004": "16001",
                "P29_005": f"区立第{i}小学校",
            }
            for i in range(4)
        ]
        path = _tmp_geojson(tmp, "p29.geojson", rows)
        with _quiet():
            out = fetch.normalize_schools(path, assume_missing=True)
        assert len(out) == 5, f"特別支援学校 5 件のはずが {len(out)} 件（列の選択が違う）"


@check("P29: 分類コードで絞ると名称から分かる学校を取りこぼす場合は止まる")
def _p29_class_column_must_not_undercount():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        # 16012 を持つ列はあるが、名称が特別支援学校の 4 件のうち 1 件しか拾えない。
        # 減る方向の誤りは地図を見ても気付けないので、ここで止める。
        rows = [
            {
                "lat": lat,
                "lon": lon + i * 0.002,
                "P29_003": code,
                "P29_004": f"都立第{i}特別支援学校",
            }
            for i, code in enumerate(["16012", "16008", "16009", "16010"])
        ]
        path = _tmp_geojson(tmp, "p29.geojson", rows)
        _raises(lambda: fetch.normalize_schools(path), contains="漏れる")


@check("P29: 分類コードで絞った結果が特別支援学校らしくなければ止まる")
def _p29_class_column_must_not_overcount():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        # 16012 がほぼ全件に付いている列。絞り込みが効かず小中高まで需要に乗る。
        rows = [
            {
                "lat": lat,
                "lon": lon + i * 0.002,
                "P29_003": "16012",
                "P29_004": name,
            }
            for i, name in enumerate(
                ["都立◯◯特別支援学校", "区立第1小学校", "区立第2小学校", "都立△△高等学校"]
            )
        ]
        # 分類コードの列として選ばれるには 2 種類以上の値が要る。
        rows += [
            {
                "lat": lat + 0.002,
                "lon": lon + i * 0.002,
                "P29_003": "16001",
                "P29_004": f"区立第{i}中学校",
            }
            for i in range(2)
        ]
        path = _tmp_geojson(tmp, "p29.geojson", rows)
        _raises(lambda: fetch.normalize_schools(path), contains="取り違えている")


@check("P29: 特別支援学校のコードが 1 件も無ければ止まる")
def _p29_requires_special_needs_rows():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        # 名称は特別支援学校なのに、分類コードの体系が違って 16012 がどこにも無い
        #（年度でコードが変わった場合）。0 件のレイヤーは実データとして
        # 数えられたまま需要を消してしまうので、既定値で通してはならない。
        rows = [
            {
                "lat": lat,
                "lon": lon + i * 0.002,
                "P29_003": code,
                "P29_004": f"都立第{i}特別支援学校",
            }
            for i, code in enumerate(["16001", "16002", "16003", "16001", "16002"])
        ]
        path = _tmp_geojson(tmp, "p29.geojson", rows)
        _raises(lambda: fetch.normalize_schools(path), contains="1 つも無かった")


@check("P29: 在籍者数の出典が無ければ止まり、--assume-missing でだけ通る")
def _p29_students():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        rows = _p29_rows(n_special=3)
        path = _tmp_geojson(tmp, "p29.geojson", rows)
        # P29 は在籍者数を持たない。黙って既定値で埋めると、
        # このレイヤーが規模を失って「近くに学校があるか」のフラグになる。
        _raises(lambda: fetch.normalize_schools(path), contains="在籍者数の出典")

        with _quiet():
            out = fetch.normalize_schools(path, assume_missing=True)
        assert len(out) == 3, f"3 件のはずが {len(out)} 件"
        assert (out["capacity"] == fetch.SCHOOL_STUDENTS_FALLBACK).all(), (
            "既定値が入っていない"
        )
        # 既定値で埋めたことが列に残ること（規模不明の件数を数える根拠）。
        assert out["students_assumed"].all(), "既定値であることが失われている"


@check("P29: 在籍者数 CSV から実数を当て、併置校は 1 校に合算する")
def _p29_students_from_csv():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        path = _tmp_geojson(tmp, "p29.geojson", _p29_mixed_founders())
        # 城南は肢体と病弱で 2 行に分かれる（実データの光明学園と同じ形）。
        csv = _sped_enrollment_csv(
            tmp,
            [
                ("811050", "城南特別支援学校", 131),
                ("811050", "城南特別支援学校", 47),
                ("812490", "城東特別支援学校", 275),
                ("821010", "新宿養護学校", 46),
            ],
        )
        with _quiet():
            out = fetch.normalize_schools(path, students_path=csv)
        got = dict(zip(out["name"], out["capacity"]))
        assert got["東京都立城南特別支援学校"] == 178, f"合算していない: {got}"
        assert got["都立城東特別支援学校"] == 275, got
        assert got["新宿区立新宿養護学校"] == 46, got
        # 私立は自己公表値の対照表から、国立は公表が無いので既定値。
        assert got["旭出学園"] == 90, got
        assert got["筑波大学附属大塚特別支援学校"] == fetch.SCHOOL_STUDENTS_FALLBACK, got
        assumed = dict(zip(out["name"], out["students_assumed"]))
        assert assumed["筑波大学附属大塚特別支援学校"], "規模不明が区別されていない"
        assert not assumed["旭出学園"], "公表値なのに規模不明になっている"


@check("P29: 公立が在籍者数 CSV と一致しなければ止まる（名寄せの綻びを既定値で埋めない）")
def _p29_students_unmatched_public():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        path = _tmp_geojson(tmp, "p29.geojson", _p29_mixed_founders())
        # 城東が CSV に無い。国立・私立と同じ扱いで既定値に流すと、
        # 「規模が消えた」ことが出力を眺めても分からない。
        csv = _sped_enrollment_csv(
            tmp,
            [("811050", "城南特別支援学校", 131), ("821010", "新宿養護学校", 46)]
            # 都内の他区の学校（研究領域の外にもあるので CSV には載る）。
            # 在籍者数の異なり値を実データ並みに保つための埋め草でもある。
            + [(f"8125{i:02d}", f"第{i}特別支援学校", 200 + i * 30) for i in range(4)],
        )
        _raises(
            lambda: fetch.normalize_schools(path, students_path=csv),
            contains="城東特別支援学校",
        )


@check("P29: 在籍者数 CSV の合計行と合わなければ止まる（行の取りこぼし・二重計上）")
def _p29_students_total_mismatch():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        csv = _sped_enrollment_csv(
            tmp,
            [(f"8120{i:02d}", f"第{i}特別支援学校", 100 + i * 20) for i in range(6)],
            total=500,  # ファイルが主張する合計と合わない
        )
        _raises(lambda: fetch.read_school_enrollment(csv), contains="合計が合わない")


@check("P29: 設置者の接頭辞を外して突き合わせる（「東京都立」と「都立」の混在）")
def _school_name_normalization():
    from etl.schema import normalize_school_name as norm

    assert norm("東京都立城南特別支援学校") == "城南特別支援学校"
    assert norm("都立城東特別支援学校") == "城東特別支援学校"
    assert norm("新宿区立新宿養護学校") == "新宿養護学校"
    assert norm("旭出学園") == "旭出学園"
    # 「◯◯立」を含まない学校名を削らないこと。
    assert norm("筑波大学附属大塚特別支援学校") == "筑波大学附属大塚特別支援学校"
    # 欠測が名前として持ち回られないこと（normalize_ward で一度やらかしている）。
    assert norm(float("nan")) == ""


@check("P14: 施設名称の列を特定できなければ止まる（都道府県名の列を掴まない）")
def _p14_requires_name():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        # 名称の列が無い。文字列であることだけを条件に探すと
        # P14_001（"東京都"）を名称と誤認する——実際に一度そうなった。
        rows = [
            {
                "lat": lat,
                "lon": lon + i * 0.002,
                "P14_001": "東京都",
                "P14_002": "渋谷区",
                "P14_007": code,
            }
            for i, code in enumerate(["030300", "050901"])
        ]
        path = _tmp_geojson(tmp, "p14.geojson", rows)
        _raises(lambda: fetch.normalize_welfare(path), contains="施設名称")


@check("P14: 小分類コードと名称がそろえば障害福祉事業所を抽出できる")
def _p14_happy_path():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        rows = [
            {
                "lat": lat,
                "lon": lon,
                "P14_007": "030300",
                "P14_008": "障害者支援施設あおぞら",
            },
            {
                "lat": lat,
                "lon": lon + 0.002,
                "P14_007": "050901",
                "P14_008": "児童発達支援センターにじ",
            },
            {
                "lat": lat,
                "lon": lon + 0.004,
                "P14_007": "030300",
                "P14_008": "就労継続支援Ｂ型ひだまり",
            },
        ]
        path = _tmp_geojson(tmp, "p14.geojson", rows)
        with _quiet():
            out = fetch.normalize_welfare(path)
        assert len(out) == 3, f"3 件のはずが {len(out)} 件"
        # 定員は推定値であることが列として残っていること（提言時の但し書きの根拠。
        # 感度分析が「仮定員の行だけ落とす」ためにこの列を見る）。
        assert out["capacity_assumed"].all(), "推定であることが失われている"
        assert (out["demand_value"] > 0).all(), out["demand_value"].tolist()


@check("P29: 生徒数らしい数値の列があっても、名前の裏付けが無ければ拾わない")
def _p29_students_needs_name_hint():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        # 「建築年」は児童生徒数と値域が重なる。中身だけで当てようとすると
        # これを生徒数として拾い、規模の重み付けが無意味な値で回る。
        rows = [
            {**r, "建築年": 1975 + i * 5} for i, r in enumerate(_p29_rows(n_special=6))
        ]
        path = _tmp_geojson(tmp, "p29.geojson", rows)
        # 建築年を拾っていれば「出典が無い」ではなく通ってしまう。
        _raises(lambda: fetch.normalize_schools(path), contains="在籍者数の出典")


@check("P29: 列名が違っても、名前に手掛かりがあれば生徒数を特定する")
def _p29_students_detected_with_hint():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        rows = [
            {**r, "在籍者数": 100 + i * 30}
            for i, r in enumerate(_p29_rows(n_special=6, n_other=2))
        ]
        path = _tmp_geojson(tmp, "p29.geojson", rows)
        with _quiet():
            out = fetch.normalize_schools(path)
        assert sorted(out["capacity"]) == [100, 130, 160, 190, 220, 250], (
            out["capacity"].tolist()
        )


@check("P04: 診療科目の列を特定できなければ止まる（全医療機関の混入を防ぐ）")
def _p04_requires_departments():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        rows = [
            {"lat": lat, "lon": lon + i * 0.002, "P04_002": f"◯◯病院{i}", "備考": ""}
            for i in range(3)
        ]
        path = _tmp_geojson(tmp, "p04.geojson", rows)
        _raises(lambda: fetch.normalize_clinics(path), contains="診療科目")


@check("P04: 列名が違っても中身から診療科目を特定し、絞り込みが効く")
def _p04_detects_departments_by_value():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        # 列名 P04_003 ではない。値だけが診療科目であることを示している。
        rows = [
            {"lat": lat, "lon": lon, "P04_002": "Aクリニック", "診療科": "精神科・心療内科"},
            {"lat": lat, "lon": lon + 0.002, "P04_002": "B医院", "診療科": "内科・小児科"},
            {"lat": lat, "lon": lon + 0.004, "P04_002": "C病院", "診療科": "外科・整形外科"},
        ]
        path = _tmp_geojson(tmp, "p04.geojson", rows)
        with _quiet():
            out = fetch.normalize_clinics(path)
        assert len(out) == 1, f"精神科 1 件だけ残るはずが {len(out)} 件"
        assert out.iloc[0]["name"] == "Aクリニック", out.iloc[0]["name"]


# 常時監視測定地点の様式。**見出しが 3 行にまたがる**（項目名・副項目名・単位）
# のが実データの形で、昼夜の区別は 3 行目にしか出ない。
# 住所は実在する街区（位置参照情報に載っているもの）を使う——ここを作り物に
# すると座標化が全滅し、「様式を読めたか」ではなく「住所が実在するか」を
# 検査することになる。
_NOISE_ADDRESSES = (
    "渋谷区神南1丁目1",
    "渋谷区道玄坂1丁目10",
    "渋谷区桜丘町2",
    "新宿区西新宿2丁目8",
    "新宿区高田馬場1丁目1",
    "豊島区南池袋2丁目45",
)


def _tmp_monitoring_csv(
    tmp: Path,
    name: str,
    *,
    day: list[int],
    night: list[int] | None = None,
    area_types: list[str] | None = None,
    marks: bool = True,
    year: int = 2023,
    addresses: tuple[str, ...] = _NOISE_ADDRESSES,
) -> Path:
    """常時監視測定地点の CSV を、実データと同じ 3 行見出しで書き出す。"""
    night = night or [v - 4 for v in day]
    area_types = area_types or ["C", "B", "A"] * 3
    head = [
        ["騒音測定地点番号", "測定地点の住所", "環境基準類型", "遮音壁等の有無",
         "測定開始年月日", "車道端からの距離", "等価騒音レベル(ｄＢ)", ""],
        ["", "", "", "", "", "", "", ""],
        ["", "", "", "", "", "(m)", "昼間", "夜間"],
    ]
    body = [
        [
            i + 1,
            addresses[i % len(addresses)],
            area_types[i % len(area_types)],
            "○" if marks else i + 1,
            f"{year}-11-{(i % 20) + 1:02d}",
            4.2 + i,
            day[i],
            night[i],
        ]
        for i in range(len(day))
    ]
    path = tmp / name
    pd.DataFrame(head + body).to_csv(path, index=False, header=False, encoding="utf-8")
    return path


@check("騒音: 見出しが 3 行にまたがっても昼間の dB 列を選ぶ")
def _noise_prefers_daytime():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        # 昼夜は値域が同じで、区別は**3 行目の見出しにしか無い**。
        # 見出しを 1 行しか読まないと夜間を掴み得る（実測で 3〜4dB 低い）。
        path = _tmp_monitoring_csv(
            tmp, "noise.csv", day=[70, 71, 72, 73, 74, 75], night=[60, 61, 62, 63, 64, 65]
        )
        with _quiet():
            out = fetch.normalize_noise(path)
        assert out["laeq_db"].min() >= 70.0, (
            f"夜間の列を拾っている: {sorted(out['laeq_db'].unique())}"
        )
        assert len(out) == 6, f"6 点あるはずが {len(out)} 点"


@check("騒音: 騒音レベルの列を特定できなければ止まる")
def _noise_requires_laeq():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        # 常時監視の様式のまま、**dB の列だけ落ちている**表。数値の列は
        # 残っているので「表は読めたが騒音レベルが無い」形になる。
        head = [
            ["騒音測定地点番号", "測定地点の住所", "環境基準類型", "遮音壁等の有無",
             "車道端からの距離", "地上からの高さ"],
            ["", "", "", "", "(m)", "(m)"],
            ["", "", "", "", "", ""],
        ]
        body = [
            [i + 1, _NOISE_ADDRESSES[i], "C", "○", 4.2 + i, 1.2] for i in range(6)
        ]
        path = tmp / "noise.csv"
        pd.DataFrame(head + body).to_csv(
            path, index=False, header=False, encoding="utf-8"
        )
        _raises(lambda: fetch.normalize_noise(path), contains="等価騒音レベル")


@check("騒音: 常時監視と要請限度を取り違えない（大文字/小文字で判定する）")
def _noise_labels_survey():
    """**2026-08-05 まで、要請限度は読まずに止めていた。**

    測定点の選ばれ方が区に依存する調査（`docs/issues.md` A1 の原因）なので
    混ぜない、という方針だった。**入れることにした**——実際にうるさいと
    申し立てが出た道路の情報のほうが価値がある、というユーザー判断。

    方針が変わっても、**取り違えてはいけない**ことは変わらない。むしろ
    強くなる: 画面は「この点は常時監視/要請限度」と書くので、**判定を
    間違えると、苦情の出た道路の実測値が系統調査の値として画面に出る。**
    出力を眺めても気付けない種類の誤りである。

    手掛かりは法概念の書き分けで、表記のゆれではない——環境基本法の
    地域類型は大文字 A/AA/B/C、騒音規制法の区域の区分は小文字 a/b/c。
    """
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        # 中身はそっくりで、違うのは類型の大文字/小文字と○×の列だけ。
        mon = _tmp_monitoring_csv(tmp, "mon.csv", day=[70, 71, 72, 73, 74, 75])
        req = _tmp_monitoring_csv(
            tmp,
            "req.csv",
            day=[70, 71, 72, 73, 74, 75],
            area_types=["c", "b", "a"] * 3,
            marks=False,
        )
        with _quiet():
            m = fetch.normalize_noise(mon)
            r = fetch.normalize_noise(req)
        assert set(m["survey"]) == {fetch.SURVEY_MONITORING}, set(m["survey"])
        assert set(r["survey"]) == {fetch.SURVEY_REQUEST_LIMIT}, set(r["survey"])

        # 束ねても調査名は消えない。同じ座標を両方が測っていれば併記する
        # （捨てると「混ぜたこと」が画面から見えなくなる）。
        with _quiet():
            merged = fetch.aggregate_noise_years([m, r])
        assert len(merged) == len(m), f"同じ住所なので 1 点に畳まれるはず: {len(merged)}"
        assert set(merged["survey"]) == {
            f"{fetch.SURVEY_MONITORING}・{fetch.SURVEY_REQUEST_LIMIT}"
        }, set(merged["survey"])


@check("騒音: 「N 年度の平均」の N と、並べる年度の数が一致する")
def _noise_year_count_matches_list():
    """**画面が「6 年度の平均」と書きながら 5 年度しか並べていなかった。**

    `n_years` は `nunique()`、一覧は空文字を落とした集合、と**2 箇所から
    別々に作っていた**ため、年度を取れなかった行の空文字が件数にだけ乗った
    （241 地点）。原因は令和3年度の要請限度だけ日付の列が「測定開始開始」に
    平坦化されて候補から外れていたこと——**列名で決めていたから**である。

    ここで検査するのは 2 つ:
      - 年度の列は列名で見つからなくても**中身**から見つかること
      - 件数と一覧が同じ集合から作られていること
    """
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        frames = []
        for year in (2021, 2022):
            path = _tmp_monitoring_csv(
                tmp, f"n{year}.csv", day=[70, 71, 72, 73, 74, 75], year=year
            )
            raw = pd.read_csv(path, header=None, dtype=object)
            # 見出しを実データと同じ壊れ方にする（「測定開始年月日」→「測定開始開始」）。
            raw.iloc[0, 4] = "測定開始"
            raw.iloc[2, 4] = "開始"
            broken = tmp / f"b{year}.csv"
            raw.to_csv(broken, index=False, header=False, encoding="utf-8")
            with _quiet():
                frames.append(fetch.normalize_noise(broken))

        assert set(frames[0]["year"]) == {"2021"}, set(frames[0]["year"])
        with _quiet():
            merged = fetch.aggregate_noise_years(frames)
        for _, row in merged.iterrows():
            listed = len([v for v in str(row["years"]).split("・") if v])
            assert row["n_years"] == listed, (
                f"{row['name']}: n_years={row['n_years']} だが一覧は {listed} 年度"
            )


@check("騒音: 同じ地点の別年度は平均し、近くの別地点は潰さない")
def _noise_merges_years_not_neighbours():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        # 同じ住所を 2 年度ぶん。各地点ちょうど 4dB 差にしてあるので、
        # 平均が取れていれば 2dB だけ上がる。
        first = [70, 71, 72, 73, 74, 75]
        one = _tmp_monitoring_csv(tmp, "y1.csv", day=first, year=2019)
        two = _tmp_monitoring_csv(tmp, "y2.csv", day=[v + 4 for v in first], year=2023)
        with _quiet():
            frames = [fetch.normalize_noise(p) for p in (one, two)]
            out = fetch.aggregate_noise_years(frames)
        assert len(out) == 6, f"6 地点に束ねるはずが {len(out)} 地点"
        assert sorted(out["laeq_db"]) == [v + 2.0 for v in first], (
            f"年度平均になっていない: {sorted(out['laeq_db'])}"
        )
        assert set(out["n_years"]) == {2}, f"年度数が合わない: {set(out['n_years'])}"

        # **距離では寄せない。** 別の街区に落ちた点は 100m 以内でも別地点。
        # `dedupe_points`（同名かつ 100m 以内は同一施設）をそのまま通すと、
        # 別々の道路の実測値が 1 点に潰れる。
        far = fetch.aggregate_noise_years([frames[0]])
        assert len(far) == 6, f"1 年度だけでも 6 地点残るはずが {len(far)} 地点"


@check("公共施設: 施設名から種別を判定できなければ止まる（到達不可の過大計上を防ぐ）")
def _facilities_requires_name():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        # 名称の列が「施設コード」しかない。従来は候補 0 件のまま通り、
        # 「既存施設では到達不可」が全メッシュで成立していた。
        rows = [
            {"緯度": lat, "経度": lon + i * 0.002, "施設コード": f"S{i:03d}"}
            for i in range(4)
        ]
        path = _tmp_csv(tmp, "facilities.csv", rows)
        _raises(lambda: fetch.normalize_hosts(path), contains="施設名")


@check("公共施設: 列名が違っても中身から施設名を特定し、対象外施設だけを落とす")
def _facilities_detects_name_by_value():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        rows = [
            {"緯度": lat, "経度": lon, "名前": "渋谷区立中央図書館"},
            {"緯度": lat, "経度": lon + 0.002, "名前": "◯◯区民センター"},
            {"緯度": lat, "経度": lon + 0.004, "名前": "△△保育園"},
        ]
        path = _tmp_csv(tmp, "facilities.csv", rows)
        with _quiet():
            out = fetch.normalize_hosts(path)
        kinds = set(out["host_kind"])
        assert len(out) == 2, f"図書館と区民センターの 2 件のはずが {len(out)} 件"
        assert "図書館" in kinds, kinds


@check("WAM NET: 29 サービス種別すべてに明示の需要重みがある")
def _wamnet_all_types_weighted():
    # 既定重み 0.50 へ落ちても警告しか出ない。全角の「就労継続支援Ａ型」を
    # 半角前提のまま扱っていたため、東京都で最多の通所系 1,197 件が
    # 0.90 ではなく 0.50 で計算される状態だった。
    from .schema import (
        WAMNET_SERVICE_TYPES,
        WELFARE_DEMAND_WEIGHT,
        normalize_welfare_type,
    )

    missing = [
        t
        for t in WAMNET_SERVICE_TYPES
        if normalize_welfare_type(t) not in WELFARE_DEMAND_WEIGHT
    ]
    assert not missing, f"既定重みへ落ちる種別: {missing}"


@check("WAM NET: 同じ事業所の別サービスを名寄せで消さない")
def _wamnet_keeps_multi_service():
    # 多機能型事業所は同一名称・同一住所で種別ごとに届け出る。名称だけで
    # 寄せると 1 行しか残らず、**残りの定員が黙って消える**（需要が下がる
    # 方向の誤りなので地図を見ても気付けない）。
    from shapely.geometry import Point

    rows = [
        ("◯◯作業所", "就労継続支援B型", 20.0),
        ("◯◯作業所", "生活介護", 20.0),
        # 同名・同種別・同位置なら 1 件に寄せる（出典をまたいだ重複）。
        ("◯◯作業所", "生活介護", 20.0),
    ]
    gdf = gpd.GeoDataFrame(
        {
            "name": [r[0] for r in rows],
            "kind": [r[1] for r in rows],
            "capacity": [r[2] for r in rows],
        },
        geometry=[Point(139.7016, 35.6580)] * len(rows),
        crs="EPSG:4326",
    )
    with _quiet():
        out = fetch.dedupe_points(gdf, tag="test")
    assert len(out) == 2, f"種別の違う 2 件が残るはずが {len(out)} 件"
    assert out["capacity"].sum() == 40.0, out["capacity"].tolist()


@check("WAM NET: 定員が空の行は種別ごとの仮定員で補う")
def _wamnet_capacity_fallback_by_kind():
    # 訪問系・相談系には制度上定員が無く、WAM NET でも空欄になる。
    # 一律の既定値で埋めると、人が集まらない拠点に通所系と同じ規模を与える。
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        rows = [
            {"事業所緯度": lat, "事業所経度": lon, "事業所の名称": "A",
             "サービス種別": "居宅介護", "定員": ""},
            {"事業所緯度": lat, "事業所経度": lon + 0.002, "事業所の名称": "B",
             "サービス種別": "生活介護", "定員": 40},
        ]
        path = _tmp_csv(tmp, "wamnet.csv", rows)
        with _quiet():
            out = fetch.normalize_wamnet(path)
        cap = dict(zip(out["name"], out["capacity"]))
        assert cap["B"] == 40.0, cap
        assert cap["A"] == 5.0, f"居宅介護の仮定員は 5 人のはず: {cap}"


@check("WAM NET: 実定員が仮定員と同値でも「仮定」とは印を付けない")
def _wamnet_marks_only_blank_as_assumed():
    # 感度分析は capacity_assumed の行だけを揺さぶる（docs/issues.md A3）。
    # この列を後から「capacity == assumed_capacity(kind)」で復元すると、
    # **実定員がたまたま仮定員と同じ 20 人だった生活介護**を仮定側に数え、
    # 「需要の何割が仮定に由来するか」という土台の数字が過大になる。
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        rows = [
            # 空欄 → 仮定員 5 人。
            {"事業所緯度": lat, "事業所経度": lon, "事業所の名称": "訪問",
             "サービス種別": "居宅介護", "定員": ""},
            # 実定員 20 人。生活介護の仮定員も 20 人だが、これは届出の値。
            {"事業所緯度": lat, "事業所経度": lon + 0.002, "事業所の名称": "通所",
             "サービス種別": "生活介護", "定員": 20},
        ]
        path = _tmp_csv(tmp, "wamnet.csv", rows)
        with _quiet():
            out = fetch.normalize_wamnet(path)
        flag = dict(zip(out["name"], out["capacity_assumed"]))
        assert flag["訪問"], "空欄の行に仮定の印が付いていない"
        assert not flag["通所"], (
            "実定員 20 人が仮定扱いになっている。"
            "値の一致ではなく「空欄だったか」で判定すること"
        )


@check("公共施設: 駐輪場・公衆便所・喫煙場所はホスト候補に入らない")
def _facilities_rejects_non_hosts():
    # 区の一覧には「◯◯駅前自転車等駐車場」「◯◯駅東口公衆便所」が施設と同じ
    # 粒度で載っている。名称に「駅」が入るだけで拾っていたため、渋谷区 32 件・
    # 世田谷区 10 件が供給側に混ざり、地図上は「到達できている」と見えていた。
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        rows = [
            {"緯度": lat, "経度": lon, "名称": "中央図書館"},
            {"緯度": lat, "経度": lon + 0.002, "名称": "世田谷駅南自転車等駐車場"},
            {"緯度": lat, "経度": lon + 0.004, "名称": "渋谷駅東口公衆便所"},
            {"緯度": lat, "経度": lon + 0.006, "名称": "経堂駅指定喫煙場所"},
            {"緯度": lat, "経度": lon + 0.008, "名称": "渋谷駅ハチ公口"},
        ]
        path = _tmp_csv(tmp, "facilities.csv", rows)
        with _quiet():
            out = fetch.normalize_hosts(path)
        assert list(out["name"]) == ["中央図書館"], list(out["name"])


@check("公共施設: 区名に都名が付いていても対象区として扱う")
def _facilities_normalizes_ward():
    # 「東京都渋谷区」のまま持つと TARGET_WARDS と一致せず、対象区の施設に
    # 「対象区の所管外」という逆の注記が付いた提言が出る。
    assert fetch.normalize_ward("東京都渋谷区") == "渋谷区"
    assert fetch.normalize_ward("世田谷区") == "世田谷区"
    # 「府中市」を都道府県付きと誤読して「中市」にしないこと。
    assert fetch.normalize_ward("府中市") == "府中市"
    assert fetch.normalize_ward(None) == ""
    # **欠測が区名として持ち回られないこと。** float('nan') は真なので
    # `str(value or "")` と書くと "nan" が返り、区名になる。実際に千代田区で
    # これが起き、公開中の提言 3 件に「ただしnanの施設であり、対象区の所管外」
    # という逆の注記が出ていた。空文字なら注記そのものが出ない（安全側）。
    assert fetch.normalize_ward(float("nan")) == ""
    assert fetch.normalize_ward("nan") == ""
    assert fetch.normalize_ward("") == ""
    assert fetch.normalize_ward("  ") == ""


# ---------------------------------------------------------------------------
# 複数出典が同じレイヤーへ書くとき
# ---------------------------------------------------------------------------

# hosts は p14-hosts（児童館）と facilities（区の公共施設一覧）が、
# welfare は p14 と wamnet が書く。あとから流した方が既存を丸ごと
# 置き換えるため、児童館 154 件を消したことに気付かないまま
# 「既存施設では到達不可」が増える——という壊れ方をしていた。


def _hosts_frame(rows: list[tuple[str, float, float, str]]) -> gpd.GeoDataFrame:
    from shapely.geometry import Point

    return gpd.GeoDataFrame(
        {
            "name": [r[0] for r in rows],
            "host_kind": ["児童館"] * len(rows),
            "source": [r[3] for r in rows],
        },
        geometry=[Point(r[2], r[1]) for r in rows],
        crs="EPSG:4326",
    )


@check("既存の出典が消えるなら、指定が無い限り書かずに止まる")
def _merge_requires_choice():
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "hosts.geojson"
        _hosts_frame([("A児童館", 35.658, 139.701, "P14")]).to_file(
            out, driver="GeoJSON"
        )
        new = _hosts_frame([("B図書館", 35.659, 139.702, "公共施設一覧")])

        try:
            with _quiet():
                fetch._merge_with_existing(
                    new, out, kind="facilities", layer_key="hosts", merge=None
                )
        except SystemExit as e:
            assert "--append" in str(e) and "P14" in str(e), str(e)
        else:
            raise AssertionError("既存の出典を黙って捨てた")

        # append なら両方残る。replace なら入れ替わる。
        with _quiet():
            merged = fetch._merge_with_existing(
                new, out, kind="facilities", layer_key="hosts", merge="append"
            )
            replaced = fetch._merge_with_existing(
                new, out, kind="facilities", layer_key="hosts", merge="replace"
            )
        assert len(merged) == 2, len(merged)
        assert set(merged["source"]) == {"P14", "公共施設一覧"}, set(merged["source"])
        assert len(replaced) == 1, len(replaced)


@check("同じ出典を入れ直すだけなら止まらない")
def _merge_same_source_replaces():
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "hosts.geojson"
        _hosts_frame([("A児童館", 35.658, 139.701, "P14")]).to_file(
            out, driver="GeoJSON"
        )
        new = _hosts_frame(
            [("A児童館", 35.658, 139.701, "P14"), ("B児童館", 35.66, 139.70, "P14")]
        )
        with _quiet():
            got = fetch._merge_with_existing(
                new, out, kind="p14-hosts", layer_key="hosts", merge=None
            )
        assert len(got) == 2, len(got)


@check("--append で入れ直した出典は、古い行ではなく新しい行が残る")
def _merge_append_prefers_new_rows():
    # 既存を先に並べて統合していたため、同名・同位置の組では古い行が生き残り、
    # 正規化を直して入れ直しても出力が 1 バイトも変わらなかった。
    # 「直したのに効いていない」ことに気付けない壊れ方なので固定する。
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "hosts.geojson"
        old = _hosts_frame([("A図書館", 35.658, 139.701, "公共施設一覧")])
        old["ward"] = "東京都渋谷区"
        keep = _hosts_frame([("B児童館", 35.660, 139.703, "P14")])
        keep["ward"] = "渋谷区"
        _concat = gpd.GeoDataFrame(
            pd.concat([old, keep], ignore_index=True), crs=old.crs
        )
        _concat.to_file(out, driver="GeoJSON")

        new = _hosts_frame([("A図書館", 35.658, 139.701, "公共施設一覧")])
        new["ward"] = "渋谷区"
        with _quiet():
            got = fetch._merge_with_existing(
                new, out, kind="facilities", layer_key="hosts", merge="append"
            )
        assert len(got) == 2, len(got)
        wards = dict(zip(got["name"], got["ward"]))
        assert wards["A図書館"] == "渋谷区", wards
        # 別出典（P14）は残っていること。
        assert "B児童館" in wards, wards


@check("同名で近い施設は 1 件に寄せ、同名でも離れていれば残す")
def _dedupe_by_name_and_distance():
    # 区の一覧と P14 で同じ児童館。名称に全角空白、座標は 44m ずれ。
    # 座標を丸めて格子で寄せる実装ではこれを取り逃がした（格子の境目）。
    rows = [
        ("四番町児童館", 35.68830, 139.73660, "P14"),
        ("四番　町児童館", 35.68870, 139.73660, "公共施設一覧"),
        # 同名だが 1km 以上離れた別施設（分館など）は残す。
        ("四番町児童館", 35.70000, 139.73660, "公共施設一覧"),
    ]
    with _quiet():
        got = fetch.dedupe_points(_hosts_frame(rows), tag="test")
    assert len(got) == 2, [n for n in got["name"]]
    assert got.iloc[0]["source"] == "P14", "先に入っていた側を残していない"


# ---------------------------------------------------------------------------
# 提言の単位（地区）
#
# 提言を施設名で出すのをやめ、隣接する区画のまとまりで出すようにした。
# **束ね方が壊れても出力はもっともらしく見える** ——地区が 1 つに繋がりすぎても
# バラバラでも、それらしい見出しと文章が出てしまい、眺めても気付けない。
# ---------------------------------------------------------------------------


@check("メッシュの格子座標が隣接関係を正しく表す")
def _grid_index_adjacency():
    # 5339458711 の 4 近傍を、コードから作らず緯度経度から作る（実装の裏取り）。
    base = "5339458711"
    cell = meshlib.decode(base)
    dlat, dlon = meshlib.CELL_SIZE[5]
    clat = (cell.min_lat + cell.max_lat) / 2
    clon = (cell.min_lon + cell.max_lon) / 2

    for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1)):
        code = meshlib.encode(clat + di * dlat, clon + dj * dlon, 5)
        gi, gj = meshlib.grid_index(code)
        bi, bj = meshlib.grid_index(base)
        assert (gi - bi, gj - bj) == (di, dj), (code, gi - bi, gj - bj, di, dj)
        assert meshlib.is_adjacent(base, code), code

    # 2 セル離れたら隣ではない。
    far = meshlib.encode(clat + 2 * dlat, clon, 5)
    assert not meshlib.is_adjacent(base, far), far


@check("離れた区画は 1 つの地区にまとめない")
def _cluster_keeps_separate_areas():
    a = "5339458711"
    cell = meshlib.decode(a)
    dlat, dlon = meshlib.CELL_SIZE[5]
    clat = (cell.min_lat + cell.max_lat) / 2
    clon = (cell.min_lon + cell.max_lon) / 2

    neighbor = meshlib.encode(clat, clon + dlon, 5)  # 隣
    far = meshlib.encode(clat + 10 * dlat, clon, 5)  # 10 セル北
    # far の隣を挟んでも、a とは繋がらない。
    far_neighbor = meshlib.encode(clat + 10 * dlat, clon + dlon, 5)

    groups = hostlib.cluster_adjacent([a, neighbor, far, far_neighbor])
    assert len(groups) == 2, groups
    assert groups[0] == [0, 1], groups
    assert groups[1] == [2, 3], groups


@check("到達不可の点検は、区内で優先度が下位の区画（非市街地）を数えない")
def _reach_report_excludes_low_priority():
    # 江東区の到達不可は大半が有明・青海・夢の島の埋立地で、区内で下位に沈む。
    # そこを数えると「転用できる施設が少ない区」に見えてしまう。
    rows = []
    for i in range(10):
        rows.append(
            {
                "ward": "江東区",
                # 上位 5 件が市街地、下位 5 件が埋立地のつもり。
                "priority": 0.9 - i * 0.1,
                # 到達不可なのは市街地 1 件と埋立地 4 件。
                "f_host_n": 0 if i in (1, 6, 7, 8, 9) else 3,
            }
        )
    out = hostlib.reach_report(pd.DataFrame(rows))
    row = out[out["区"] == "江東区"].iloc[0]
    assert row["到達不可"] == 5, row.to_dict()
    assert row["中位以上"] == 1, f"埋立地側を数えている: {row.to_dict()}"


@check("到達不可の中位判定は区ごとに切る（全体の中央値で切らない）")
def _reach_report_threshold_is_per_ward():
    # 優先度の水準が区で違う。全体の中央値で切ると、水準の低い区
    #（ここでは足立区）の中で相対的に切実な区画がまとめて消える。
    rows = [{"ward": "豊島区", "priority": 0.9 + i * 0.01, "f_host_n": 1} for i in range(4)]
    rows += [{"ward": "足立区", "priority": 0.1 + i * 0.01, "f_host_n": 1} for i in range(4)]
    # 足立区の中では上位だが、全体で見れば下位半分に入る到達不可区画。
    rows.append({"ward": "足立区", "priority": 0.20, "f_host_n": 0})
    out = hostlib.reach_report(pd.DataFrame(rows))
    row = out[out["区"] == "足立区"].iloc[0]
    assert row["中位以上"] == 1, f"全体の中央値で切っている: {out.to_string(index=False)}"


@check("到達不可の中位判定の母数は区内の全区画（到達不可だけで中央値を取らない）")
def _reach_report_median_over_all_meshes():
    # 到達不可のメッシュだけで中央値を取ると、非市街地の多い区で閾値が下がり、
    # 埋立地どうしの比較で「上半分」が中位以上に化ける。
    rows = [{"ward": "江東区", "priority": 0.8, "f_host_n": 2} for _ in range(6)]
    rows += [{"ward": "江東区", "priority": p, "f_host_n": 0} for p in (0.1, 0.2, 0.3, 0.4)]
    out = hostlib.reach_report(pd.DataFrame(rows))
    row = out[out["区"] == "江東区"].iloc[0]
    assert row["到達不可"] == 4, row.to_dict()
    assert row["中位以上"] == 0, (
        f"到達不可だけで中央値を取っている: {row.to_dict()}"
    )


@check("到達不可の点検は、数える区が 1 つも無くても列を保ったまま返る")
def _reach_report_keeps_columns_when_empty():
    # 区名が付かないメッシュしか無い場合。表が空になったときに列ごと消えると、
    # 呼び出し側が列名で触った瞬間に KeyError で落ちる（--report が実際に落ちた）。
    out = hostlib.reach_report(
        pd.DataFrame([{"ward": "", "priority": 0.5, "f_host_n": 0}])
    )
    assert len(out) == 0, out.to_string(index=False)
    assert int(out["到達不可"].sum()) == 0, out.columns.tolist()
    assert int(out["中位以上"].sum()) == 0, out.columns.tolist()


@check("提言に施設への設置を指示する語を出さない")
def _ranking_never_recommends_a_facility():
    # 施設名は「徒歩圏に在るもの」としてなら出てよいが、
    # それを設置先として名指しする語と結び付けてはいけない。
    cards = [
        {
            "rank": 1,
            "mesh_code": "5339458711",
            "lon": 139.71,
            "lat": 35.73,
            "ward": "豊島区",
            "station": "池袋",
            "priority": 1.0,
            "demand": 1.0,
            "load": 1.0,
            "host_count": 5,
            "host_name": "上池袋図書館",
            "host_kind": "図書館",
            "host_ward": "豊島区",
            "host_distance_m": 400,
            "narrative": "（区画の説明）",
        }
    ]
    got = hostlib.build_ranking(cards, 20)
    assert len(got) == 1, got
    # **単位は区画。** 地区の見出し（「○○周辺」）はもう作らない——
    # その広がりはモデルが計算しておらず、母数の切り方で決まっていた。
    assert "area_label" not in got[0], got[0]
    assert got[0]["mesh_code"] == "5339458711", got[0]
    for word in ("設置候補", "設置先", "設置すべき", "転用", "周辺"):
        assert word not in got[0]["narrative"], (word, got[0]["narrative"])


@check("隣接の数は母数つきで述べる（地区として束ねない）")
def _ranking_states_adjacency_with_denominator():
    def card(rank, code, ward, station, host):
        return {
            "rank": rank,
            "mesh_code": code,
            "lon": 139.75,
            "lat": 35.70,
            "ward": ward,
            "station": station,
            "priority": 1.0,
            "demand": 1.0,
            "load": 1.0,
            "host_count": 0 if not host else 3,
            "host_name": host,
            "host_kind": "図書館" if host else "",
            "host_ward": ward if host else "",
            "host_distance_m": None if not host else 400,
            "narrative": "（区画の説明）",
        }

    base = "5339464011"
    cell = meshlib.decode(base)
    dlat, dlon = meshlib.CELL_SIZE[5]
    clat = (cell.min_lat + cell.max_lat) / 2
    clon = (cell.min_lon + cell.max_lon) / 2
    right = meshlib.encode(clat, clon + dlon, 5)
    far = meshlib.encode(clat + dlat * 40, clon, 5)

    got = hostlib.build_ranking(
        [
            card(1, base, "千代田区", "水道橋", "千代田図書館"),
            card(2, right, "文京区", "水道橋", ""),
            card(3, far, "板橋区", "板橋", "板橋図書館"),
        ],
        20,
    )
    # 束ねない。3 区画は 3 行のまま出る。
    assert len(got) == 3, got
    assert [g["rank"] for g in got] == [1, 2, 3], got

    # 隣り合う 2 件は互いに 1 件ずつ接している。離れた 1 件は 0。
    assert got[0]["adjacent_n"] == 1, got[0]
    assert got[1]["adjacent_n"] == 1, got[1]
    assert got[2]["adjacent_n"] == 0, got[2]

    # **母数を必ず書く。** 「隣接 1 区画」だけだと場所の性質に見えるが、
    # この数は上位何件を見るかで変わる。
    assert "上位20区画のうち1区画" in got[0]["narrative"], got[0]["narrative"]
    assert "上位20区画のうち、この区画に接するものは無い" in got[2]["narrative"], got[2]
    assert got[2]["adjacent_of"] == 20, got[2]

    # 到達不可は区画ごとのフラグとして残る（地区単位の集計ではない）。
    assert got[0]["unreachable"] is False, got[0]
    assert got[1]["unreachable"] is True, got[1]


@check("A1 が追い続けている「層 × 区」の組が、実在する層と区を指している")
def _a1_tracked_pair_is_real():
    """`config.A1_TRACKED_PAIR` が実体を失っていないこと。

    **これが無いと、検査が黙って消える。** `doc_numbers.py` は
    `meta.ward_dependence.tracked` が在るときだけ相関を突き合わせ、
    `score.ward_dummy_correlation` は層名か区名が見つからなければ
    `None` を返す。つまり **`TARGET_WARDS` から世田谷区が抜けたり
    構成要素のキーを変えたりすると、A1 の系列の検査だけが静かに
    外れる**——落ちるのではなく、項目ごと表から消える。

    A1 の指標は 5 ビルド並べてきた数値で、消えても出力は何も変わらない。
    **間違っていても動く**種類の壊れ方なので、ここで止める。
    """
    from .config import A1_TRACKED_PAIR, TARGET_WARDS

    key, ward = A1_TRACKED_PAIR
    keys = [c.key for c in ALL_COMPONENTS]
    assert key in keys, f"A1_TRACKED_PAIR の層 {key!r} が構成要素に無い（{keys}）"
    assert ward in TARGET_WARDS, f"A1_TRACKED_PAIR の区 {ward!r} が対象区に無い"

    # 相関が実際に取れること（定数列を渡したときだけ None になる）。
    df = pd.DataFrame(
        {
            f"n_{key}": [0.0, 1.0, 0.5, 0.2],
            "ward": [ward, ward, "千代田区", "千代田区"],
        }
    )
    r = score.ward_dummy_correlation(df, key, ward)
    assert r is not None and -1.0 <= r <= 1.0, r

    # 区が見つからなければ None。**doc_numbers はこれを「項目なし」として
    # 素通りさせるので、上の TARGET_WARDS 検査が最後の砦になる。**
    assert score.ward_dummy_correlation(df, key, "存在しない区") is None


@check("区ダミーとの相関は、区を決め打たずに全区から選ぶ")
def _ward_dependence_scans_all_wards():
    """`ward_dependence_report` が、名指しの区ではなく最大の区を返すこと。

    **A1 は「世田谷ダミー」という区を名指した指標**で、名指したまま
    追い続けると偏りが別の区へ移ったときに気付けない（実際いま騒音の
    最大は練馬区で、符号も逆）。決め打たない側が本当に決め打っていない
    ことを、世田谷区より強い区を仕込んで確かめる。
    """
    key = ALL_COMPONENTS[0].key
    df = pd.DataFrame(
        {
            f"n_{key}": [1.0, 1.0, 0.0, 0.0, 0.5, 0.5],
            "ward": ["練馬区", "練馬区", "世田谷区", "世田谷区", "港区", "港区"],
        }
    )
    out = score.ward_dependence_report(df)
    assert not out.empty, out
    row = out.iloc[0]
    assert row["ward"] == "練馬区", f"最大の区を選んでいない: {row.to_dict()}"
    assert row["相関"] > 0, row.to_dict()

    # ward 列が無ければ空を返す（例外にしない——模擬モードでも呼ばれる）
    assert score.ward_dependence_report(df.drop(columns="ward")).empty


def main() -> int:
    print("calmgap-tokyo セルフテスト\n")
    # モジュール読み込み時に @check が実行済み。
    if _failures:
        print(f"\n{len(_failures)} 件失敗\n")
        for f in _failures:
            print(f)
        return 1
    print("\nすべて通過\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
