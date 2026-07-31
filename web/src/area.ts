/**
 * 上位メッシュを「隣接する区画のまとまり（地区）」へ束ねる。
 *
 * 提言の単位を施設名から地区へ移した経緯は etl/hosts.py の build_proposals に
 * 書いてある（要点: このモデルは施設の適性を一切測っていない）。
 *
 * ここは etl/mesh.py の grid_index / etl/hosts.py の cluster_adjacent・
 * _area_label と対になる実装。**片方を触ったら必ず両方。**
 * スコアと違って parity_check の対象外なので、食い違っても静かに通る。
 * その代わり、束ね方を整数だけで決めている（浮動小数の距離を使わない）ので、
 * 両言語で結果がズレる余地は無い。
 */

/**
 * メッシュコードを格子の整数座標 [南北, 東西] に直す。
 *
 * 次数ごとの分割数（2次で8、3次で10、4次と5次で2）を順に掛けて足すだけ。
 * 緯度側と経度側で分割数が同じなので、同じ式で両方出せる。
 */
export function gridIndex(code: string): [number, number] {
  const c = code.trim();
  let i = Number(c.slice(0, 2));
  let j = Number(c.slice(2, 4));
  if (c.length >= 6) {
    i = i * 8 + Number(c[4]);
    j = j * 8 + Number(c[5]);
  }
  if (c.length >= 8) {
    i = i * 10 + Number(c[6]);
    j = j * 10 + Number(c[7]);
  }
  for (const pos of [8, 9]) {
    if (c.length >= pos + 1) {
      const quad = Number(c[pos]) - 1;
      i = i * 2 + Math.floor(quad / 2);
      j = j * 2 + (quad % 2);
    }
  }
  return [i, j];
}

/**
 * 隣接するメッシュ同士を連結成分にまとめる（斜めも隣とみなす 8 近傍）。
 * 返すのは添字の配列で、各まとまりの中は元の順序（＝優先度順）を保つ。
 */
export function clusterAdjacent(codes: string[]): number[][] {
  const idx = codes.map(gridIndex);
  const parent = codes.map((_, i) => i);

  const find = (i: number): number => {
    while (parent[i] !== i) {
      parent[i] = parent[parent[i]];
      i = parent[i];
    }
    return i;
  };

  for (let a = 0; a < codes.length; a++) {
    for (let b = a + 1; b < codes.length; b++) {
      if (Math.abs(idx[a][0] - idx[b][0]) <= 1 && Math.abs(idx[a][1] - idx[b][1]) <= 1) {
        const ra = find(a);
        const rb = find(b);
        if (ra !== rb) parent[Math.max(ra, rb)] = Math.min(ra, rb);
      }
    }
  }

  const groups = new Map<number, number[]>();
  for (let i = 0; i < codes.length; i++) {
    const r = find(i);
    const g = groups.get(r);
    if (g) g.push(i);
    else groups.set(r, [i]);
  }
  return [...groups.entries()].sort((a, b) => a[0] - b[0]).map(([, g]) => g);
}

/**
 * 地区の見出し。
 *
 * 駅名は「その区画から最も近い駅」であって管理者でも所在地でもないので、
 * 実在の施設を名指しせずに場所を指せる。優先度の高い区画のものから採り、
 * 片方がもう片方の先頭に含まれる名前は落とす（「大塚」と「大塚駅前」を
 * 並べても場所は 1 つしか指していない）。
 */
export function areaLabel(
  wards: string[],
  stations: string[],
): { label: string; wardLabel: string } {
  const picked: string[] = [];
  for (const s of stations) {
    if (!s) continue;
    if (picked.some((p) => s.startsWith(p) || p.startsWith(s))) continue;
    picked.push(s);
    if (picked.length === 2) break;
  }

  const uniq = [...new Set(wards.filter(Boolean))];
  // またがっていること自体が重要な情報。提言先の自治体が分かれる。
  const wardLabel = uniq.length === 0 ? "" : uniq.length === 1 ? uniq[0] : `${uniq[0]}ほか`;
  const where = picked.length ? `${picked.join("・")}周辺` : "周辺";
  return { label: `${wardLabel} ${where}`.trim(), wardLabel };
}
