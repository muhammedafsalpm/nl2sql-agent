import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies /api to the FastAPI backend, so the browser only ever talks to one
// origin. That means no CORS in development — CORS_ORIGINS only matters for a deployed build.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        // Matches API_HOST/API_PORT in backend/.env
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
