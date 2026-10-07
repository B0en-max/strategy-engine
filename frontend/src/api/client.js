/**
 * API 客户端（Stage 17）。
 *
 * 唯一职责：把「后端 HTTP 接口」封装成函数，供 React 组件调用。
 * **这里没有任何策略 / 订单 / 成交 / 持仓逻辑**：
 *   方向映射、持仓计算、信号状态变更全部由后端 Stage 13/16 的实现负责。
 *
 * 接口对应关系（全部来自 Stage 16 的 API 层）：
 *   GET  /health
 *   GET  /strategies                     GET  /symbols
 *   GET  /klines
 *   POST /strategies/{id}/run            GET  /strategies/{id}/signals
 *   POST /execution/signals/{id}
 *   GET  /positions  GET /orders  GET /trades
 */

import { API_BASE_URL } from '../config.js'

/** 统一的 API 错误类型：带上 HTTP 状态码与后端原始 detail，便于上层区分展示。 */
export class ApiError extends Error {
  constructor(message, { status = 0, detail = null, url = '', cause = null } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.url = url
    this.cause = cause
  }

  /** status === 0 表示请求根本没到达后端（后端没启动 / 地址不对 / 网络不可达） */
  get isConnectionError() {
    return this.status === 0
  }
}

/** 拼接基础地址 + 路径 + 查询串；空值（undefined / null / ''）会被丢弃。 */
export function buildUrl(path, query) {
  const base = `${API_BASE_URL}${path.startsWith('/') ? path : `/${path}`}`
  const search = new URLSearchParams()
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null || value === '') continue
      search.append(key, String(value))
    }
  }
  const queryString = search.toString()
  return queryString ? `${base}?${queryString}` : base
}

/** 从 FastAPI 的错误响应体里取出可读信息（detail 可能是字符串或校验错误数组）。 */
export function detailFrom(payload) {
  if (!payload || typeof payload !== 'object') return null
  const detail = payload.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail
      .map((item) => (item && typeof item === 'object' ? item.msg ?? JSON.stringify(item) : String(item)))
      .join('; ')
  }
  return null
}

/** 把 HTTP 状态码翻译成用户能看懂的提示（不隐藏后端原始信息）。 */
export function errorMessage(status, detail) {
  if (status === 404) return `资源不存在：${detail ?? '找不到请求的策略 / 标的 / 信号'}`
  if (status === 409) return `操作被拒绝：${detail ?? '状态冲突（例如信号已执行）'}`
  if (status === 400) return `参数错误：${detail ?? '请求参数不合法'}`
  if (status === 422) return `参数格式不正确：${detail ?? '请求体或查询参数不符合接口要求'}`
  return `API 返回错误（HTTP ${status}）：${detail ?? '未知错误'}`
}

/**
 * 发起一次请求并返回解析后的 JSON。
 * 失败时统一抛 ApiError（连接失败 → status 0；HTTP 错误 → 保留状态码与 detail）。
 */
export async function request(path, { method = 'GET', body, query, headers } = {}) {
  const url = buildUrl(path, query)
  const options = { method, headers: { Accept: 'application/json', ...(headers ?? {}) } }
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json'
    options.body = JSON.stringify(body)
  }

  let response
  try {
    response = await fetch(url, options)
  } catch (error) {
    throw new ApiError(
      `无法连接 API（${API_BASE_URL}）：请确认后端已启动，且地址配置正确。原始错误：${
        error?.message ?? error
      }`,
      { url, cause: error },
    )
  }

  const text = await response.text()
  let payload = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = null
    }
  }

  if (!response.ok) {
    const detail = detailFrom(payload)
    throw new ApiError(errorMessage(response.status, detail), {
      status: response.status,
      detail,
      url,
    })
  }
  return payload
}

// --------------------------------------------------------------------- 接口
export const getHealth = () => request('/health')

export const listStrategies = ({ status } = {}) => request('/strategies', { query: { status } })

export const listSymbols = ({ market, activeOnly = false } = {}) =>
  request('/symbols', { query: { market, active_only: activeOnly ? true : undefined } })

export const listKlines = ({ symbolId, timeframe, limit } = {}) =>
  request('/klines', { query: { symbol_id: symbolId, timeframe, limit } })

export const listSignals = ({ strategyId, symbolId, timeframe, status, limit } = {}) =>
  request(`/strategies/${strategyId}/signals`, {
    query: { symbol_id: symbolId, timeframe, status, limit },
  })

export const runStrategy = (strategyId, { symbolId, timeframe, startTimestamp, endTimestamp } = {}) =>
  request(`/strategies/${strategyId}/run`, {
    method: 'POST',
    body: {
      symbol_id: symbolId,
      timeframe,
      start_timestamp: startTimestamp || null,
      end_timestamp: endTimestamp || null,
    },
  })

export const executeSignal = (signalId, { quantity, price = null } = {}) =>
  request(`/execution/signals/${signalId}`, {
    method: 'POST',
    body: { quantity, price: price === undefined ? null : price },
  })

export const listPositions = ({ symbolId } = {}) =>
  request('/positions', { query: { symbol_id: symbolId } })

export const listOrders = ({ symbolId, signalId, status, limit } = {}) =>
  request('/orders', { query: { symbol_id: symbolId, signal_id: signalId, status, limit } })

export const listTrades = ({ symbolId, signalId, side, limit } = {}) =>
  request('/trades', { query: { symbol_id: symbolId, signal_id: signalId, side, limit } })
