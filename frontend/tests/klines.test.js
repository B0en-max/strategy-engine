/**
 * 前端最小测试（Stage 18）：K 线与信号叠加的纯计算逻辑。
 *
 * 固定样本 REAL_15M_ROWS / REAL_15M_LAST 直接抄自本机真实接口响应
 * （GET /klines?symbol_id=1&timeframe=15m&limit=320，600519 贵州茅台），
 * 用于保证「界面用的是接口返回的 OHLC」以及「信号按 timestamp 精确匹配」是被断言过的。
 */

import { describe, expect, it } from 'vitest'

import {
  SIGNAL_STATUS_STYLE,
  buildCandles,
  buildSignalMarkers,
  candleTimesOf,
  fromChartTime,
  groupSignalsByTime,
  markerColor,
  markerPosition,
  markerShape,
  splitByChart,
  toChartTime,
} from '../src/chart/klines.js'

// --- 真实接口样本（截取字段，数值原样）-------------------------------------
const REAL_15M_ROWS = [
  {
    timestamp: '2026-09-30 14:15:00',
    open: 1264.56,
    high: 1264.56,
    low: 1258.24,
    close: 1259.7,
    volume: 342570.0,
  },
  {
    timestamp: '2026-09-30 14:30:00',
    open: 1259.42,
    high: 1260.03,
    low: 1257.51,
    close: 1258.19,
    volume: 203032.0,
  },
  {
    timestamp: '2026-09-30 14:45:00',
    open: 1258.19,
    high: 1258.62,
    low: 1255.86,
    close: 1258.62,
    volume: 291636.0,
  },
]

const T_1415 = toChartTime('2026-09-30 14:15:00')
const T_1430 = toChartTime('2026-09-30 14:30:00')
const T_1445 = toChartTime('2026-09-30 14:45:00')

describe('toChartTime / fromChartTime', () => {
  it('把 UTC 时间字符串换算成秒级时间戳', () => {
    expect(T_1445).toBe(Date.UTC(2026, 8, 30, 14, 45, 0) / 1000)
    expect(toChartTime('2026-09-30T14:45:00')).toBe(T_1445)
    expect(toChartTime('  2026-09-30 14:45:00  ')).toBe(T_1445)
  })

  it('非法输入返回 null（不会静默当成 0）', () => {
    expect(toChartTime('2026/09/30 14:45:00')).toBeNull()
    expect(toChartTime('')).toBeNull()
    expect(toChartTime(null)).toBeNull()
  })

  it('换算可逆（用于界面上显示 UTC 时间）', () => {
    expect(fromChartTime(toChartTime('2026-09-30 14:45:00'))).toBe('2026-09-30 14:45:00')
    expect(fromChartTime(T_1415)).toBe('2026-09-30 14:15:00')
    expect(fromChartTime(null)).toBe('-')
  })
})

describe('buildCandles', () => {
  it('真实接口行 → 蜡烛数据，OHLC 原样保留', () => {
    const candles = buildCandles(REAL_15M_ROWS)
    expect(candles).toHaveLength(3)
    expect(candles.map((candle) => candle.time)).toEqual([T_1415, T_1430, T_1445])
    expect(candles[2]).toMatchObject({
      open: 1258.19,
      high: 1258.62,
      low: 1255.86,
      close: 1258.62,
      timestamp: '2026-09-30 14:45:00',
    })
  })

  it('按时间升序（图表库要求），输入乱序也正确', () => {
    const candles = buildCandles([REAL_15M_ROWS[2], REAL_15M_ROWS[0], REAL_15M_ROWS[1]])
    expect(candles.map((candle) => candle.time)).toEqual([T_1415, T_1430, T_1445])
  })

  it('丢弃时间戳或 OHLC 非法的行，不抛异常', () => {
    const candles = buildCandles([
      REAL_15M_ROWS[0],
      { timestamp: 'bad', open: 1, high: 1, low: 1, close: 1 },
      { timestamp: '2026-09-30 15:00:00', open: 1, high: Number.NaN, low: 1, close: 1 },
      { timestamp: '2026-09-30 15:15:00', open: 1, high: 2, low: 0.5, close: 1.5 },
    ])
    expect(candles).toHaveLength(2)
    expect(candles.map((candle) => candle.timestamp)).toEqual([
      '2026-09-30 14:15:00',
      '2026-09-30 15:15:00',
    ])
  })

  it('空输入返回空数组', () => {
    expect(buildCandles(null)).toEqual([])
    expect(buildCandles([])).toEqual([])
  })
})

describe('buildSignalMarkers（必须按 timestamp 匹配，不能按数组下标）', () => {
  const candles = buildCandles(REAL_15M_ROWS)
  const times = candleTimesOf(candles)

  const signals = [
    // 故意把「最后一根 K 线上的信号」放在数组最前面：若按下标对应就会匹配错
    { id: 302, timestamp: '2026-09-30 14:45:00', direction: 'short', price: 1258.62, status: 'executed', reason: 'close 1258.19 -> 1258.62' },
    { id: 300, timestamp: '2026-09-30 14:30:00', direction: 'long', price: 1258.19, status: 'pending', reason: 'close 1259.42 -> 1258.19' },
    // 不在当前 K 线范围内 / 时间戳非法 → 计为未匹配
    { id: 900, timestamp: '2026-09-29 14:00:00', direction: 'long', price: 1, status: 'pending', reason: null },
    { id: 901, timestamp: 'nonsense', direction: 'short', price: 1, status: 'pending', reason: null },
  ]

  it('按 timestamp 精确匹配，并按时间排序', () => {
    const { markers, matched, unmatched } = buildSignalMarkers(signals, times)
    expect(matched).toBe(2)
    expect(unmatched).toBe(2)
    expect(markers.map((marker) => marker.time)).toEqual([T_1430, T_1445])
    expect(markers.map((marker) => marker.id)).toEqual(['300', '302'])
  })

  it('long → 向上箭头且在 K 线下方；short → 向下箭头且在上方', () => {
    const { markers } = buildSignalMarkers(signals, times)
    const long = markers.find((marker) => marker.id === '300')
    const short = markers.find((marker) => marker.id === '302')
    expect(long.shape).toBe('arrowUp')
    expect(long.position).toBe('belowBar')
    expect(short.shape).toBe('arrowDown')
    expect(short.position).toBe('aboveBar')
  })

  it('pending / executed 表现不同（颜色 + 勾）', () => {
    const { markers } = buildSignalMarkers(signals, times)
    const pending = markers.find((marker) => marker.id === '300')
    const executed = markers.find((marker) => marker.id === '302')
    expect(pending.color).toBe(SIGNAL_STATUS_STYLE.pending.long)
    expect(executed.color).toBe(SIGNAL_STATUS_STYLE.executed.short)
    expect(pending.color).not.toBe(executed.color)
    expect(pending.text).toBe('300L')
    expect(executed.text).toBe('302S✓')
  })

  it('辅助函数与状态表一致', () => {
    expect(markerShape('long')).toBe('arrowUp')
    expect(markerShape('short')).toBe('arrowDown')
    expect(markerPosition('long')).toBe('belowBar')
    expect(markerColor('pending', 'long')).toBe(SIGNAL_STATUS_STYLE.pending.long)
    expect(markerColor('unknown-status', 'short')).toBe(SIGNAL_STATUS_STYLE.pending.short)
  })

  it('没有 K 线时全部计为未匹配（切换数据后旧标记应消失）', () => {
    const { markers, matched, unmatched } = buildSignalMarkers(signals, candleTimesOf([]))
    expect(markers).toEqual([])
    expect(matched).toBe(0)
    expect(unmatched).toBe(4)
  })
})

describe('splitByChart（K 线图与日志共用的一致性核对）', () => {
  const candles = buildCandles(REAL_15M_ROWS)
  const times = candleTimesOf(candles)
  const rows = [
    { id: 1, timestamp: '2026-09-30 14:15:00' },
    { id: 2, timestamp: '2026-09-30 14:45:00' },
    { id: 3, timestamp: '2026-09-01 09:30:00' },
    { id: 4, timestamp: 'bad' },
  ]

  it('按 timestamp 拆成「图上」与「不在当前 K 线范围」', () => {
    const { onChart, offChart } = splitByChart(rows, times)
    expect(onChart.map((row) => row.id)).toEqual([1, 2])
    expect(offChart.map((row) => row.id)).toEqual([3, 4])
  })

  it('不重不漏：onChart + offChart 恒等于输入总数', () => {
    const { onChart, offChart } = splitByChart(rows, times)
    expect(onChart.length + offChart.length).toBe(rows.length)
    const ids = [...onChart, ...offChart].map((row) => row.id).sort()
    expect(ids).toEqual([1, 2, 3, 4])
  })

  it('空输入 / 无 K 线时全部计入「不在图上」', () => {
    expect(splitByChart(null, times).onChart).toEqual([])
    expect(splitByChart(rows, new Set()).offChart).toHaveLength(4)
    expect(splitByChart(rows, new Set()).onChart).toEqual([])
  })
})

describe('groupSignalsByTime', () => {
  const candles = buildCandles(REAL_15M_ROWS)
  const times = candleTimesOf(candles)

  it('同一时间戳的多条信号归到一组并按 id 排序', () => {
    const byTime = groupSignalsByTime(
      [
        { id: 5, timestamp: '2026-09-30 14:45:00' },
        { id: 3, timestamp: '2026-09-30 14:45:00' },
        { id: 4, timestamp: '2026-09-30 14:15:00' },
        { id: 9, timestamp: '2026-09-01 09:30:00' },
      ],
      times,
    )
    expect(byTime.size).toBe(2)
    expect(byTime.get(T_1445).map((signal) => signal.id)).toEqual([3, 5])
    expect(byTime.get(T_1415).map((signal) => signal.id)).toEqual([4])
    expect(byTime.has(toChartTime('2026-09-01 09:30:00'))).toBe(false)
  })

  it('不传 K 线时间集合时按时间戳分组（不丢弃）', () => {
    const byTime = groupSignalsByTime([{ id: 1, timestamp: '2026-09-30 14:45:00' }], null)
    expect(byTime.get(T_1445)).toHaveLength(1)
  })
})
