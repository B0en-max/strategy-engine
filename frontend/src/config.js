/**
 * 前端统一配置（Stage 17）。
 *
 * 唯一职责：给出 API 基础地址，避免地址散落在各个组件里。
 * 取值顺序：VITE_API_BASE_URL（.env / .env.local / 构建时注入）> 开发默认值。
 */

/** 开发环境默认后端地址（本机 FastAPI） */
export const DEFAULT_API_BASE_URL = 'http://127.0.0.1:8000'

/** 信号自动刷新间隔（毫秒） */
export const SIGNALS_REFRESH_INTERVAL_MS = 10000

/** 市场页默认行情周期与历史 K 线根数（与后端 /market/* 默认值一致） */
export const MARKET_TIMEFRAME = '1d'
export const MARKET_KLINE_LIMIT = 300

/**
 * 读取 Vite 注入的环境变量。
 * 非 Vite 环境（例如测试里直接 import）拿不到 import.meta.env，返回 undefined。
 */
export function readEnv(name) {
  try {
    const env = typeof import.meta === 'object' && import.meta ? import.meta.env : undefined
    const value = env ? env[name] : undefined
    return typeof value === 'string' && value.trim() !== '' ? value.trim() : undefined
  } catch {
    return undefined
  }
}

/** 去掉结尾斜杠，避免拼出 `//health` 这种路径 */
export function normalizeBaseUrl(value) {
  return String(value).trim().replace(/\/+$/, '')
}

/** 实际使用的 API 基础地址 */
export const API_BASE_URL = normalizeBaseUrl(readEnv('VITE_API_BASE_URL') ?? DEFAULT_API_BASE_URL)
