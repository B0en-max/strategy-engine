/**
 * 信号 / 交易列表筛选（纯计算，可扩展）。
 *
 * 设计目标：让界面能明确区分「策略产生的信号」与「已经执行的交易」，
 * 且筛选必须真正作用于当前列表（不是装饰性 UI）。
 *
 * 记录类型（复用现有数据模型里的 status，不新增字段、不改数据库）：
 *   signal   —— status !== 'executed'（策略产生、尚未执行；含未来的 cancelled）
 *   executed —— status === 'executed'（已经实际执行过的交易）
 *
 * 四个开关的语义（默认值见 DEFAULT_SIGNAL_FILTERS）：
 *   showSignals  = true  列表允许显示「策略信号」（未执行的）
 *   executedOnly = false 开启后只显示「已执行交易」（优先于 showSignals）
 *   longOnly / shortOnly 方向过滤；两者同时开启时视为不限制方向
 *
 * 扩展方式：新增筛选条件时
 *   1）在 DEFAULT_SIGNAL_FILTERS 加默认值
 *   2）在 SIGNAL_FILTER_OPTIONS 加一项（界面自动渲染，无需改组件）
 *   3）在 applySignalFilters 里加一行判断
 * 典型可扩展项：pending / cancelled、TP / SL、Win / Loss、strategy、symbol、时间范围。
 */

/** 记录类型取值 */
export const RECORD_KIND = {
  signal: 'signal',
  executed: 'executed',
}

/** 记录类型的中文说明（列表里用于区分 Signal 与 Executed Trade） */
export const RECORD_KIND_LABEL = {
  signal: '策略信号（未执行）',
  executed: '已执行交易',
}

/** 判断一条记录是「策略信号」还是「已执行交易」 */
export function recordKind(row) {
  return row && row.status === 'executed' ? RECORD_KIND.executed : RECORD_KIND.signal
}

/** 筛选默认值 */
export const DEFAULT_SIGNAL_FILTERS = Object.freeze({
  showSignals: true,
  executedOnly: false,
  longOnly: false,
  shortOnly: false,
})

/** 界面渲染用的开关列表（数据驱动：加条件只需在这里加一项） */
export const SIGNAL_FILTER_OPTIONS = Object.freeze([
  { key: 'showSignals', label: '显示 Signals' },
  { key: 'executedOnly', label: '只显示已执行' },
  { key: 'longOnly', label: '只显示 Long' },
  { key: 'shortOnly', label: '只显示 Short' },
])

/**
 * 需要限制的方向：null 表示不限制。
 * Long 与 Short 同时开启 → 视为「同时显示 Long 和 Short」，不限制方向（不会互相抵消成空列表）。
 */
export function wantedDirection(filters) {
  const { longOnly, shortOnly } = filters ?? {}
  if (longOnly && shortOnly) return null
  if (longOnly) return 'long'
  if (shortOnly) return 'short'
  return null
}

/** 应用筛选：返回满足条件的记录（保持原顺序） */
export function applySignalFilters(rows, filters = DEFAULT_SIGNAL_FILTERS) {
  const { showSignals = true, executedOnly = false } = filters ?? {}
  const direction = wantedDirection(filters)

  return (rows ?? []).filter((row) => {
    const kind = recordKind(row)
    const kindVisible = executedOnly
      ? kind === RECORD_KIND.executed // 只显示已执行：优先于总开关
      : kind === RECORD_KIND.signal && showSignals // 否则只显示未执行的策略信号
    if (!kindVisible) return false
    if (direction && row.direction !== direction) return false
    return true
  })
}

/**
 * 汇总一组记录（供 K 线图与交易日志显示同一份口径的数字）。
 * 返回 total / pending / executed / long / short。
 */
export function summarizeSignals(rows) {
  const summary = { total: 0, pending: 0, executed: 0, long: 0, short: 0 }
  for (const row of rows ?? []) {
    summary.total += 1
    if (recordKind(row) === RECORD_KIND.executed) summary.executed += 1
    else summary.pending += 1
    if (row && row.direction === 'short') summary.short += 1
    else if (row && row.direction === 'long') summary.long += 1
  }
  return summary
}

/** 当前筛选的可读描述（显示在列表上方，避免用户误解为空数据） */
export function describeFilters(filters = DEFAULT_SIGNAL_FILTERS) {
  const record = filters.executedOnly
    ? '仅已执行交易'
    : filters.showSignals
      ? '仅策略信号（未执行）'
      : '不显示任何信号'

  const direction =
    filters.longOnly && filters.shortOnly
      ? 'Long + Short（不限制方向）'
      : filters.longOnly
        ? '仅 Long'
        : filters.shortOnly
          ? '仅 Short'
          : '全部方向'

  return `当前筛选：${record} · ${direction}`
}
