import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev server: http://127.0.0.1:5173, API calls proxied to the backend (make run).
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
