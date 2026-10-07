/**
 * 前端最小测试 1/2：API 调用逻辑（Stage 17）。
 *
 * 只验证「前端如何调 API」这一层：URL 拼接、请求体、错误翻译。
 * 不模拟后端行为，也不引入 jsdom / testing-library。
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { API_BASE_URL } from '../src/config.js'
import {
  ApiError,
  buildUrl,
  executeSignal,
  listSignals,
  request,
  runStrategy,
} from '../src/api/client.js'

/** 构造一个假的 fetch 响应 */
function jsonResponse(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(payload),
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('buildUrl', () => {
  it('拼接基础地址并丢弃空查询参数', () => {
    expect(buildUrl('/health')).toBe(`${API_BASE_URL}/health`)
    expect(buildUrl('/symbols', { market: undefined, active_only: false, limit: null })).toBe(
      `${API_BASE_URL}/symbols?active_only=false`,
    )
    expect(buildUrl('/klines', { symbol_id: 1, timeframe: '15m' })).toBe(
      `${API_BASE_URL}/klines?symbol_id=1&timeframe=15m`,
    )
  })
})

describe('request', () => {
  it('返回解析后的 JSON', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(200, { status: 'ok' })))
    await expect(request('/health')).resolves.toEqual({ status: 'ok' })
  })

  it('后端不可达时抛出连接错误（status 0）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch')
      }),
    )
    const error = await request('/health').catch((err) => err)
    expect(error).toBeInstanceOf(ApiError)
    expect(error.isConnectionError).toBe(true)
    expect(error.message).toContain('无法连接 API')
    expect(error.message).toContain(API_BASE_URL)
  })

  it('HTTP 错误保留状态码与后端 detail', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(409, { detail: '信号 1 当前状态为 executed，只有 pending 状态可以模拟执行' })),
    )
    const error = await request('/execution/signals/1', { method: 'POST', body: {} }).catch(
      (err) => err,
    )
    expect(error.status).toBe(409)
    expect(error.detail).toContain('只有 pending 状态可以模拟执行')
    expect(error.message).toContain('只有 pending 状态可以模拟执行')
  })

  it('422 校验错误能把 pydantic 的 detail 数组转成可读文字', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(422, { detail: [{ loc: ['body', 'quantity'], msg: 'Input should be greater than 0' }] })),
    )
    const error = await request('/execution/signals/1', { method: 'POST', body: {} }).catch(
      (err) => err,
    )
    expect(error.status).toBe(422)
    expect(error.message).toContain('Input should be greater than 0')
  })
})

describe('接口封装', () => {
  it('executeSignal 用 POST 调用 /execution/signals/{id} 并带上数量与价格', async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200, { signal_id: 7, trade_id: 3 }))
    vi.stubGlobal('fetch', fetchMock)

    const result = await executeSignal(7, { quantity: 2, price: 1250.5 })

    const [url, options] = fetchMock.mock.calls[0]
    expect(url).toBe(`${API_BASE_URL}/execution/signals/7`)
    expect(options.method).toBe('POST')
    expect(JSON.parse(options.body)).toEqual({ quantity: 2, price: 1250.5 })
    expect(result).toEqual({ signal_id: 7, trade_id: 3 })
  })

  it('executeSignal 未传价格时显式发送 null（后端用信号价）', async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200, {}))
    vi.stubGlobal('fetch', fetchMock)

    await executeSignal(7, { quantity: 1 })

    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ quantity: 1, price: null })
  })

  it('runStrategy 用 POST 调用 /strategies/{id}/run，未填时间时发送 null', async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200, { bars: 10, signals: 2 }))
    vi.stubGlobal('fetch', fetchMock)

    await runStrategy(3, { symbolId: 1, timeframe: '15m' })

    const [url, options] = fetchMock.mock.calls[0]
    expect(url).toBe(`${API_BASE_URL}/strategies/3/run`)
    expect(options.method).toBe('POST')
    expect(JSON.parse(options.body)).toEqual({
      symbol_id: 1,
      timeframe: '15m',
      start_timestamp: null,
      end_timestamp: null,
    })
  })

  it('listSignals 拼出策略信号查询的查询串', async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200, []))
    vi.stubGlobal('fetch', fetchMock)

    await listSignals({ strategyId: 1, symbolId: 1, timeframe: '15m', status: 'pending', limit: 20 })

    const [url, options] = fetchMock.mock.calls[0]
    expect(url).toContain('/strategies/1/signals?')
    expect(url).toContain('symbol_id=1')
    expect(url).toContain('timeframe=15m')
    expect(url).toContain('status=pending')
    expect(url).toContain('limit=20')
    expect(options.method).toBe('GET')
  })
})
