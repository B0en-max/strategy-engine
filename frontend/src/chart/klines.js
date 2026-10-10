/**
 * K 线与信号叠加所需的**纯计算**（Stage 18）。
 *
 * 这一层刻意与图表库、React 都无关，只做数据换算，便于单测：
 *   时间戳 ↔ 秒级时间戳、/klines 行 → 蜡烛数据、信号 → 图形标记（按时间戳精确匹配）
 *
 * 明确不做：策略判断、订单/成交/持仓计算、技术指标（MA/MACD/…）。
 */

/** 默认加载的 K 线根数（不拉全量，避免「无限数据」） */
export const DEFAULT_KLINE_LIMIT = 320

/** 图上叠加信号时的一次性拉取上限 */
export const CHART_SIGNAL_LIMIT = 1000

const TIMESTAMP_PATTERN = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})$/

/** 'YYYY-MM-DD HH:MM:SS'（UTC）→ 秒级 UNIX 时间戳；非法输入返回 null。 */
export function toChartTime(timestamp) {
  if (typeof timestamp !== 'string') return null
  const match = TIMESTAMP_PATTERN.exec(timestamp.trim())
  if (!match) return null
  const [, year, month, day, hour, minute, second] = match
  const milliseconds = Date.UTC(
    Number(year),
    Number(month) - 1,
    Number(day),
    Number(hour),
    Number(minute),
    Number(second),
  )
  return Number.isFinite(milliseconds) ? Math.floor(milliseconds / 1000) : null
}

/** 秒级 UNIX 时间戳 → 'YYYY-MM-DD HH:MM:SS'（UTC），用于界面展示。 */
export function fromChartTime(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return '-'
  const date = new Date(seconds * 1000)
  const pad = (value) => String(value).padStart(2, '0')
  return (
    `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())} ` +
    `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}`
  )
}

const isFiniteNumber = (value) => typeof value === 'number' && Number.isFinite(value)

/**
 * /klines 返回的行 → 蜡烛数据。
 * 非法时间戳 / 非数字 OHLC 的行会被丢弃；结果按时间升序（图表库要求）。
 */
export function buildCandles(klineRows) {
  const candles = []
  for (const row of klineRows ?? []) {
    const time = toChartTime(row && row.timestamp)
    if (time === null) continue
    const { open, high, low, close } = row
    if (![open, high, low, close].every(isFiniteNumber)) continue
    candles.push({
      time,
      open,
      high,
      low,
      close,
      timestamp: row.timestamp,
      volume: isFiniteNumber(row.volume) ? row.volume : null,
    })
  }
  candles.sort((left, right) => left.time - right.time)
  return candles
}

/** 蜡烛时间集合（用于「信号时间戳能否落到某根 K 线上」的精确匹配）。 */
export function candleTimesOf(candles) {
  return new Set((candles ?? []).map((candle) => candle.time))
}

/** 信号状态 → 图上表现：方向用绿/红，状态用深浅 + 勾（pending 实色、executed 浅色带 ✓） */
export const SIGNAL_STATUS_STYLE = {
  pending: { suffix: '', long: '#1a7f37', short: '#b42318' },
  executed: { suffix: '✓', long: '#96bda2', short: '#d7a9a3' },
}

export function markerShape(direction) {
  return direction === 'short' ? 'arrowDown' : 'arrowUp'
}

export function markerPosition(direction) {
  // long 放在 K 线下方、short 放在上方，避免标记压住蜡烛
  return direction === 'short' ? 'aboveBar' : 'belowBar'
}

export function markerColor(status, direction) {
  const style = SIGNAL_STATUS_STYLE[status] ?? SIGNAL_STATUS_STYLE.pending
  return direction === 'short' ? style.short : style.long
}

/** 标记上的短文本：信号 id + 方向（L/S）+ 已执行勾 */
export function markerText(signal) {
  const style = SIGNAL_STATUS_STYLE[signal.status] ?? SIGNAL_STATUS_STYLE.pending
  const side = signal.direction === 'short' ? 'S' : 'L'
  return `${signal.id}${side}${style.suffix}`
}

/**
 * 信号 → 图表标记。
 *
 * **按 signal.timestamp 与 K 线 timestamp 精确匹配**（毫秒级换算成秒后比较），
 * 绝不按数组下标对应：匹配不上的信号单独计数，界面会显示出来。
 */
export function buildSignalMarkers(signals, candleTimes) {
  const times = candleTimes ?? new Set()
  const matched = []
  let unmatched = 0

  for (const signal of signals ?? []) {
    const time = toChartTime(signal && signal.timestamp)
    if (time === null || !times.has(time)) {
      unmatched += 1
      continue
    }
    matched.push({ time, signal })
  }

  matched.sort((left, right) => left.time - right.time || left.signal.id - right.signal.id)

  const markers = matched.map(({ time, signal }) => ({
    time,
    position: markerPosition(signal.direction),
    shape: markerShape(signal.direction),
    color: markerColor(signal.status, signal.direction),
    text: markerText(signal),
    size: 1,
    id: String(signal.id),
  }))

  return { markers, matched: matched.length, unmatched }
}

/**
 * 把信号按「是否落在当前 K 线上」拆分（按 timestamp 精确匹配）。
 * 供统一派生层与数据一致性核对复用：onChart + offChart 恒等于输入总数（不重不漏）。
 */
export function splitByChart(signals, candleTimes) {
  const times = candleTimes ?? new Set()
  const onChart = []
  const offChart = []
  for (const signal of signals ?? []) {
    const time = toChartTime(signal && signal.timestamp)
    if (time !== null && times.has(time)) onChart.push(signal)
    else offChart.push(signal)
  }
  return { onChart, offChart }
}

/** 时间戳 → 该 K 线上的信号列表（悬停 / 点击时展示详情）。 */
export function groupSignalsByTime(signals, candleTimes) {
  const byTime = new Map()
  for (const signal of signals ?? []) {
    const time = toChartTime(signal && signal.timestamp)
    if (time === null) continue
    if (candleTimes && !candleTimes.has(time)) continue
    if (!byTime.has(time)) byTime.set(time, [])
    byTime.get(time).push(signal)
  }
  for (const list of byTime.values()) list.sort((left, right) => left.id - right.id)
  return byTime
}
