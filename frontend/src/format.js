/**
 * 纯展示用的格式化工具（Stage 17）。
 *
 * 只做字符串/数字美化，**不做任何业务计算**（价格、数量、盈亏一律用后端返回值）。
 */

/** 数字美化：null / undefined / NaN → '-'，否则固定小数位并去掉多余的 0 */
export function formatNumber(value, digits = 2) {
  if (value === null || value === undefined || value === '') return '-'
  const number = Number(value)
  if (!Number.isFinite(number)) return '-'
  const fixed = number.toFixed(digits)
  return fixed.includes('.') ? fixed.replace(/0+$/, '').replace(/\.$/, '') : fixed
}

/** 时间戳原样展示（后端统一 UTC 'YYYY-MM-DD HH:MM:SS'），并标注 UTC 以免误读 */
export function formatTimestamp(value) {
  return value ? `${value} UTC` : '-'
}

/** 标的展示名：优先用 symbols 表里的「代码 名称」，查不到就退回 symbol_id */
export function symbolLabel(symbolsById, symbolId) {
  const symbol = symbolsById ? symbolsById[symbolId] : undefined
  if (!symbol) return `#${symbolId}`
  return symbol.name ? `${symbol.symbol} ${symbol.name}` : String(symbol.symbol)
}

/** 把 symbols 列表转成 { id: symbol } 便于按 id 查名字 */
export function indexById(rows) {
  const map = {}
  for (const row of rows ?? []) map[row.id] = row
  return map
}
