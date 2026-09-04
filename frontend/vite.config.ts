import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    // Bind explicitly to IPv4 loopback. Without this Vite binds to IPv6 (::1)
    // only, while the backend (uvicorn) listens on IPv4 127.0.0.1 — browsers
    // that resolve localhost to 127.0.0.1 then hang/fail.
    host: "127.0.0.1",
    port: 3000,
    proxy: {
      "/api": {
        // 127.0.0.1 (not "localhost") so the proxy never resolves to ::1,
        // which the IPv4-only backend does not listen on.
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
