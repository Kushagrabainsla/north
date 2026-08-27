import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/app/",
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    port: 5173,
    proxy: {
      "/web": "http://127.0.0.1:8000",
      "/orchestrator": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000"
    }
  }
});
