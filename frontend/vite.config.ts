import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the browser talks only to Vite (:5173); /api is proxied to FastAPI so
// session cookies and the OAuth callback live on one origin.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: process.env.DOCTHECODE_API ?? "http://127.0.0.1:8000", changeOrigin: false } },
  },
});
