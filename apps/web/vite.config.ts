/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// dev 代理到本地 server（boss_web/config.py 的 DEFAULT_PORT = 8787）
const SERVER = "http://127.0.0.1:8787";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: SERVER, changeOrigin: true, ws: true },
    },
  },
  build: {
    // 产物直接落 boss_web/static/dist，PyInstaller 照常收集（改造文档第八节）
    outDir: "../../boss_web/static/dist",
    emptyOutDir: true,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    globals: false,
  },
});
