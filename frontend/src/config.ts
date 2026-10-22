/**
 * 构建时配置：使用 Vite 环境变量，参见仓库根目录的 .env.example。
 *
 * VITE_API_BASE_URL：前后端使用不同地址时的后端来源地址。
 * 留空时使用相对 /api 路径，本地开发由 Vite 代理到后端。
 */
const env = import.meta.env;

export const API_BASE = (env.VITE_API_BASE_URL ?? "").replace(/\/+$/, "");

export const apiUrl = (path: string) => `${API_BASE}${path}`;
