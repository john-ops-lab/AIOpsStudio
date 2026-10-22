import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    proxy: {
      "/api": {
        target: process.env.EASYOPS_API_URL || "http://127.0.0.1:8000",
        changeOrigin: false,
      },
    },
  },
  build: { target: "es2022", chunkSizeWarningLimit: 800 },
});
