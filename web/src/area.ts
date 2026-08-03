/**
 * メッシュの格子座標と、隣接するメッシュの連結成分。
 *
 * **かつてはここで「地区」を作り、それを提言の単位にしていた。**
 * 2026-08-03 にやめた（理由は etl/hosts.py の build_ranking）。
 * いま隣接が使われるのは 2 箇所だけ——順位表の「接する上位区画」の数と、
 * 地図の破線。どちらも**単位ではなく記述**である。
 *
 * ここは etl/mesh.py の grid_index / etl/hosts.py の cluster_adjacent と
 * 対になる実装。**片方を触ったら必ず両方。** スコアと違って parity_check の
 * 対象外なので食い違っても静かに通る。その代わり整数だけで決めている
 * （浮動小数の距離を使わない）ので、両言語で結果がズレる余地は無い。
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
