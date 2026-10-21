import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 本地开发将 /api 代理到后端；可通过 API_PROXY_TARGET 指向其他后端地址。
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: process.env.API_PROXY_TARGET || "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
