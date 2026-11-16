import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backend = `http://127.0.0.1:${process.env.BACKEND_PORT || 8000}`;

export default defineConfig({
  plugins: [react()],
  server: {
    port: Number(process.env.FRONTEND_PORT || 5173),
    proxy: {
      "/api": {
        target: backend,
        changeOrigin: true,
        // Without this, a stopped backend shows up in the browser as a bare "500 Internal Server Error".
        configure: (proxy) => {
          proxy.on("error", (err, _req, res) => {
            if (!res || res.headersSent || typeof res.writeHead !== "function") return;
            res.writeHead(502, { "Content-Type": "application/json" });
            res.end(JSON.stringify({
              detail: `Backend not reachable at ${backend} (${err.code || err.message}). Start it with ./start.sh ` +
                      "or: cd backend && .venv/bin/uvicorn app.main:app --port 8000",
              offline: true,
            }));
          });
        },
      },
    },
  },
});
