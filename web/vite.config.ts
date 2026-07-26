import { defineConfig } from "vite";

export default defineConfig({
  // GitHub Pages などサブパス配信を想定して相対パスで出力する。
  base: "./",
  build: {
    outDir: "dist",
    // メッシュ GeoJSON は public/ 経由でそのまま配信する（バンドルに含めない）。
    assetsInlineLimit: 0,
  },
  server: {
    port: 5173,
  },
});
