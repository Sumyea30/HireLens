import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Dev server proxy (npm run dev outside Docker). In Docker, Nginx does this job.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Quick tunnels send Host: *.trycloudflare.com; Vite 8 blocks unknown hosts by default.
    allowedHosts: [".trycloudflare.com", "localhost"],
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", rewrite: (p) => p.replace(/^\/api/, "") },
      "/ws": { target: "http://127.0.0.1:8000", ws: true },
    },
  },
  test: { environment: "node", include: ["src/**/*.test.{ts,tsx}"], env: { VITE_USE_MOCK: "true" } },
});
