/// <reference types="vitest/config" />

import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/a2a": "http://127.0.0.1:8000",
      "/.well-known": "http://127.0.0.1:8000",
    },
  },
  test: {
    environment: "happy-dom",
    globals: true,
  },
});
