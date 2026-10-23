import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { apiUpstream } from "./environment.mjs";
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    proxy: {
      "/api": {
        target: apiUpstream(),
        changeOrigin: false,
      },
    },
  },
  build: { target: "es2022", chunkSizeWarningLimit: 800 },
});
