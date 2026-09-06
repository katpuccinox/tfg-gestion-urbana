import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Misma lista de prefijos que frontend/nginx.conf reenvía al backend en
    // producción -- si se añade una ruta ahí, hay que añadirla aquí también.
    proxy: Object.fromEntries(
      [
        "auth", "health", "afectaciones", "ingesta", "analysis", "hive", "iceberg",
        "api", "admin", "politicas", "gobierno", "catalogo", "contratos", "its", "db", "db-check",
      ].map((prefix) => [`/${prefix}`, "http://127.0.0.1:8000"]),
    ),
  },
});
