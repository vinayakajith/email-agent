import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // In development the API runs separately (`email-agent app --no-browser`).
    proxy: { "/api": "http://127.0.0.1:8765" },
  },
  test: { environment: "node" },
});
