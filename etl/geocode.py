"""住所から座標を作る。

**外部のジオコーディング API を呼ばない。** 国土交通省の位置参照情報
（オフラインで配布されるオープンデータ）だけを使う。理由は 3 つ:

- ビルドが再現する。API は結果が変わるし、落ちていればビルドも落ちる
- 他のレイヤーと同じ出典系統（国土数値情報・位置参照情報）で説明が閉じる
- 利用規約と出典表示の扱いが他レイヤーと揃う

区の公共施設一覧は、**住所はあるが緯度経度が無い**ものがある。
千代田区は 230 行すべて座標が空、品川区は座標の列自体が無い。
座標が無いだけで区ごと落とすと、供給側の網羅性が区によって変わり、
「到達不可」が実態ではなくデータの有無で決まってしまう（docs/issues.md A9）。

## 精度

街区レベル（±50m 程度）で当てるのを基本とし、当たらなければ
大字・町丁目レベル（その町丁目の代表点。市街地で ±200m 程度）へ落とす。
**どちらで当てたかは必ず件数で表示する。** ホスト割当の上限は 700m なので
街区レベルなら影響は無く、町丁目レベルでも大きくは外れないが、
「座標が公表値なのか推定値なのか」は出力を見ても分からないため。

## 住所の書き方が区によって違う

同じ「◯丁目◯番」でも、算用数字の区と漢数字の区がある。

    品川区   東京都品川区西五反田3-6-3      → 西五反田三丁目 / 街区 6
    千代田区 東京都千代田区麹町二丁目8       → 麹町二丁目     / 街区 8
    千代田区 東京都千代田区一番町6-4         → 一番町（丁目なし）/ 街区 6

**先頭の数字を必ず丁目と見なしてはいけない。** 「麹町二丁目8」の 8 は街区で、
これを丁目として「麹町八丁目」を引くと当たらない。実際それで千代田区の
一致率が 22.6% まで落ちていた（直して 94.3%）。
逆に「一番町6-4」の 6 は街区であって丁目ではない（一番町に丁目は無い）。

住所の形を決め打ちせず、候補を順に照合して**最初に当たったものを採る**。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .config import DATA_RAW

ISJ_DIR = DATA_RAW / "isj"

# 位置参照情報の列名。どちらの水準かはこの列の有無で判定する
# （ファイル名や版番号からは判断しない。版で変わる）。
GAIKU_KEY = "街区符号・地番"
CHOME_KEY = "大字町丁目名"

_KANJI = "〇一二三四五六七八九"


def _to_kanji(value: str) -> str | None:
    """丁目の番号を漢数字にする。丁目としてあり得ない値は None。

    位置参照情報の大字・丁目名は「麹町二丁目」のように漢数字で入る。
    1〜99 に限るのは、番地（「1-2-345」の 345）を丁目と取り違えないため。
    """
    n = int(value)
    if not 1 <= n <= 99:
        return None
    if n < 10:
        return _KANJI[n]
    if n < 20:
        return "十" + (_KANJI[n % 10] if n % 10 else "")
    return _KANJI[n // 10] + "十" + (_KANJI[n % 10] if n % 10 else "")


@dataclass
class Geocoder:
    """位置参照情報の照合表。"""

    gaiku: dict[tuple[str, str, str], tuple[float, float]]
    chome: dict[tuple[str, str], tuple[float, float]]
    wards: tuple[str, ...]

    def lookup(self, address: object) -> tuple[float | None, float | None, str]:
        """住所を (緯度, 経度, 当てた水準) にする。当たらなければ (None, None, 理由)。"""
        s = unicodedata.normalize("NFKC", str(address or ""))
        s = s.replace(" ", "").replace("　", "")
        if s.startswith("東京都"):
            s = s[len("東京都") :]
        ward = next((w for w in self.wards if s.startswith(w)), None)
        if ward is None:
            return None, None, "住所を解釈できない"
        rest = s[len(ward) :]
        head = re.match(r"^([^0-9]+)", rest)
        if head is None:
            return None, None, "町名を取れない"
        town = head.group(1)
        nums = [str(int(x)) for x in re.findall(r"\d+", rest)]

        # (大字・丁目名, 街区符号) の候補を、確からしい順に並べる。
        candidates: list[tuple[str, str | None]] = []
        if town.endswith("丁目"):
            # 「麹町二丁目8」。丁目は既に名前に含まれ、続く数字は街区。
            candidates.append((town, nums[0] if nums else None))
        elif nums:
            kanji = _to_kanji(nums[0])
            if kanji:
                # 「西五反田3-6-3」。先頭が丁目、次が街区。
                candidates.append((town + kanji + "丁目", nums[1] if len(nums) > 1 else None))
            # 「一番町6-4」。丁目が無い町で、先頭の数字が街区。
            candidates.append((town, nums[0]))
        candidates.append((town, None))

        for cho, gai in candidates:
            if gai is not None and (ward, cho, gai) in self.gaiku:
                lat, lon = self.gaiku[(ward, cho, gai)]
                return lat, lon, "街区"
        for cho, _ in candidates:
            if (ward, cho) in self.chome:
                lat, lon = self.chome[(ward, cho)]
                return lat, lon, "町丁目"
        return None, None, "照合できない"


def _read(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="cp932", dtype=str)


ALL_MUNICIPALITIES = "*"


def load(wards: tuple[str, ...]) -> Geocoder:
    """data/raw/isj/ 以下の位置参照情報を読む。

    どちらの水準のファイルかは**列の中身**で判定する。版番号（19.0a / 19.0b）で
    区別する書き方にすると、版が上がったときに黙って別の水準を読む。

    `wards` に `ALL_MUNICIPALITIES` を渡すと、**読み込んだ表に実際に入っている
    市区町村名**を全部使う（都内 62 市区町村村）。区界の外側にある点まで
    座標化したいときに使う——騒音の測定点は都全域が 1 ファイルに入っており、
    23 区分だけ座標化すると**区界のすぐ外の測定点が落ちて縁のメッシュが
    不自然に静かに出る**（`config.CLIP_BUFFER_M` / `issues.md` A8）。
    **市区町村名は決め打ちしない**——郡部が「西多摩郡瑞穂町」の形で入るなど、
    書き方を外から当てるとそこだけ黙って落ちる。
    """
    files = sorted(ISJ_DIR.rglob("*.csv"))
    if not files:
        raise FileNotFoundError(
            f"位置参照情報が {ISJ_DIR} に無い。住所から座標を作れない。\n"
            "  街区レベル: https://nlftp.mlit.go.jp/isj/dls/data/19.0a/13000-19.0a.zip\n"
            "  大字・町丁目レベル: https://nlftp.mlit.go.jp/isj/dls/data/19.0b/13000-19.0b.zip\n"
            f"  どちらも展開して {ISJ_DIR} の下に置く。"
        )
    gaiku: dict[tuple[str, str, str], tuple[float, float]] = {}
    chome: dict[tuple[str, str], tuple[float, float]] = {}
    for path in files:
        df = _read(path)
        if GAIKU_KEY in df.columns:
            # 代表フラグ = 1 の行だけ使う。同じ街区に複数行あるため。
            df = df[df["代表フラグ"] == "1"]
            for row in df.itertuples(index=False):
                gaiku[(row.市区町村名, getattr(row, "大字・丁目名"), getattr(row, "街区符号・地番"))] = (
                    float(row.緯度),
                    float(row.経度),
                )
        elif CHOME_KEY in df.columns:
            for row in df.itertuples(index=False):
                chome[(row.市区町村名, row.大字町丁目名)] = (float(row.緯度), float(row.経度))
        else:
            print(f"[geocode] {path.name} は位置参照情報の形をしていない。飛ばす")
    if not gaiku and not chome:
        raise ValueError(f"{ISJ_DIR} に読める位置参照情報が無い")
    print(f"[geocode] 位置参照情報 街区 {len(gaiku):,} / 町丁目 {len(chome):,} を読んだ")
    if wards == (ALL_MUNICIPALITIES,) or wards == ALL_MUNICIPALITIES:
        names = {k[0] for k in gaiku} | {k[0] for k in chome}
        # 長い名前から照合する。「府中市」と「西多摩郡瑞穂町」のように
        # 前方一致が入れ子になる書き方が混じるため、短い方が先に当たると
        # 町名を取り損なう。
        wards = tuple(sorted(names, key=len, reverse=True))
        print(f"[geocode] 市区町村名を表から取った: {len(wards)} 件")
    return Geocoder(gaiku=gaiku, chome=chome, wards=wards)


def geocode_column(
    addresses: pd.Series, geocoder: Geocoder, *, tag: str
) -> tuple[pd.Series, pd.Series]:
    """住所の列を緯度・経度の列にする。当てた水準の内訳を表示する。"""
    results = [geocoder.lookup(a) for a in addresses]
    lat = pd.Series([r[0] for r in results], index=addresses.index, dtype="float64")
    lon = pd.Series([r[1] for r in results], index=addresses.index, dtype="float64")
    counts = pd.Series([r[2] for r in results]).value_counts()
    hit = int(lat.notna().sum())
    total = len(addresses)
    detail = " / ".join(f"{k} {v:,}" for k, v in counts.items())
    print(f"[{tag}] 住所から座標化 {hit:,}/{total:,}（{hit / max(total, 1):.1%}）— {detail}")
    return lat, lon
