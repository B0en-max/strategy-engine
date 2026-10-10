/**
 * 市场 / 选股页面的**纯展示逻辑**（Stage 20 Step 3；Stage 22 扩展为市场总览）。
 *
 * 只做几件事：
 *   1. 行情数字的显示格式化 —— 缺失一律显示 `—`，**绝不把 null 当 0**
 *   2. 涨跌方向与强度（仅用于着色：红涨绿跌 + 热力档位）
 *   3. 表格排序 —— 只对**已经加载到前端**的数据排序，不额外请求后端
 *   4. 板块聚合读数的整理（sectorStats / summarizeMarket / formatCoverage）
 *   5. 本地搜索过滤与面包屑（不增加后端 API、不产生 N+1）
 *
 * 明确不做（避免与后端职责重叠）：
 *   - 交易所后缀推导：display_code 由后端 MarketService 生成，前端原样使用
 *   - 行情计算：最新价 / 涨跌 / 涨跌幅 / 成交量 / 成交额全部来自后端
 *   - 板块聚合的“计算”：涨跌家数与平均涨跌幅由后端一条 SQL 算出，前端只取用
 *   - 选股算法、指标计算、虚假评分（Sector Strength / Leader Score 仅预留槽位）
 */

import { formatNumber } from '../format.js'

/** 行情缺失时的占位符（注意：不是 0，也不是 '-'） */
export const EMPTY_DISPLAY = '—'

/** 左侧分类的顺序与中文名（type 与后端 group_type 一一对应） */
export const GROUP_SECTIONS = [
  { type: 'industry', label: '行业' },
  { type: 'concept', label: '概念' },
  { type: 'watchlist', label: '自选' },
  { type: 'custom', label: '自定义' },
]

/** 可排序的列（key 与 /market/symbols 返回字段一一对应） */
export const SORTABLE_COLUMNS = [
  { key: 'latest_price', label: '最新价', digits: 2 },
  { key: 'change', label: '涨跌', digits: 2, signed: true },
  { key: 'change_percent', label: '涨跌幅', digits: 2, signed: true, percent: true },
  { key: 'volume', label: '成交量', digits: 0 },
  { key: 'amount', label: '成交额', digits: 0 },
]

/** 表格列数（名称 + 代码 + 可排序列） */
export const TABLE_COLUMN_COUNT = 2 + SORTABLE_COLUMNS.length

function hasValue(value) {
  if (value === null || value === undefined || value === '') return false
  return Number.isFinite(Number(value))
}

/** 行情数字：缺失 → '—'，否则沿用项目现有数字格式（不做千分位等新风格） */
export function formatQuote(value, digits = 2) {
  return hasValue(value) ? formatNumber(Number(value), digits) : EMPTY_DISPLAY
}

/** 带符号数值（涨跌）：缺失 → '—'，正数补 '+' */
export function formatSigned(value, digits = 2) {
  if (!hasValue(value)) return EMPTY_DISPLAY
  const number = Number(value)
  const text = formatNumber(Math.abs(number), digits)
  if (number > 0) return `+${text}`
  if (number < 0) return `-${text}`
  return text
}

/** 涨跌幅：缺失 → '—'，带 % 与符号 */
export function formatPercent(value, digits = 2) {
  if (!hasValue(value)) return EMPTY_DISPLAY
  return `${formatSigned(value, digits)}%`
}

/** 涨跌方向（只用于着色；0 与缺失都不着色） */
export function quoteTone(value) {
  if (!hasValue(value)) return ''
  const number = Number(value)
  if (number > 0) return 'up'
  if (number < 0) return 'down'
  return ''
}

/** 点击表头：同列切换升/降序；换列时数值列默认从大到小 */
export function nextSortState(current, key) {
  if (current && current.key === key) {
    return { key, direction: current.direction === 'asc' ? 'desc' : 'asc' }
  }
  return { key, direction: 'desc' }
}

/** 在已加载的行上排序；行情缺失（null）永远排在最后（与升降序无关） */
export function sortSymbols(rows, sort) {
  const list = [...(rows ?? [])]
  if (!sort || !sort.key) return list
  const factor = sort.direction === 'asc' ? 1 : -1
  return list.sort((left, right) => {
    const a = left ? left[sort.key] : null
    const b = right ? right[sort.key] : null
    const aMissing = !hasValue(a)
    const bMissing = !hasValue(b)
    if (aMissing && bMissing) return 0
    if (aMissing) return 1
    if (bMissing) return -1
    return (Number(a) - Number(b)) * factor
  })
}

/** 单元格文本（表头与单元格共用同一套格式，避免两处规则不一致） */
export function formatCell(row, column) {
  const value = row ? row[column.key] : null
  if (column.percent) return formatPercent(value, column.digits)
  if (column.signed) return formatSigned(value, column.digits)
  return formatQuote(value, column.digits)
}

/** 把分组按类型归类（供左侧分类栏渲染） */
export function groupByType(groups) {
  const result = {}
  for (const section of GROUP_SECTIONS) result[section.type] = []
  for (const group of groups ?? []) {
    if (!result[group.group_type]) result[group.group_type] = []
    result[group.group_type].push(group)
  }
  return result
}

// ---------------------------------------------------------------------------
// Stage 22：市场总览（板块 Tile / 行情摘要 / 搜索 / 面包屑）
// ---------------------------------------------------------------------------

/** 市场工作区的视图层级（MarketPage 的状态机取值） */
export const MARKET_VIEWS = ['overview', 'sector', 'symbol']

/** 类型筛选标签：全部 + 四个 group_type（复用 GROUP_SECTIONS 的中文名，不另起一套） */
export const VIEW_FILTERS = [
  { key: 'all', label: '全部' },
  ...GROUP_SECTIONS.map((section) => ({ key: section.type, label: section.label })),
]

/** 热力档位阈值（涨跌幅绝对值，%）：低于第一档视为「接近 0」→ 中性 */
export const HEAT_THRESHOLDS = [0.2, 1.5, 3, 5]

/** 涨跌幅强度档位：0（中性）~ 4（最强）。缺失 / 接近 0 都返回 0。 */
export function heatLevel(changePercent) {
  if (!hasValue(changePercent)) return 0
  const magnitude = Math.abs(Number(changePercent))
  for (let level = HEAT_THRESHOLDS.length; level > 0; level -= 1) {
    if (magnitude >= HEAT_THRESHOLDS[level - 1]) return level
  }
  return 0
}

/** 板块 Tile 的热力类名（红涨绿跌 + 深浅档位；中性时不着色） */
export function heatClass(changePercent) {
  const level = heatLevel(changePercent)
  const tone = quoteTone(changePercent)
  if (level === 0 || !tone) return 'sector-tile-neutral'
  return `sector-tile-${tone} sector-tile-${tone}-${level}`
}

/** 把后端板块聚合读数统一成数字（缺失一律 0 / null，不补 0 到 avg 上） */
export function sectorStats(group) {
  const toCount = (value) => (hasValue(value) ? Number(value) : 0)
  const symbolCount = toCount(group?.symbol_count)
  const quotedCount = toCount(group?.quoted_count)
  return {
    symbolCount,
    quotedCount,
    upCount: toCount(group?.up_count),
    downCount: toCount(group?.down_count),
    flatCount: toCount(group?.flat_count),
    // 等权平均涨跌幅：没有有效行情时为 null（不编造 0）
    avgChange: hasValue(group?.avg_change_percent) ? Number(group.avg_change_percent) : null,
    coverage: hasValue(group?.quote_coverage) ? Number(group.quote_coverage) : null,
  }
}

/** 行情覆盖不足时的提示文案；覆盖完整或没有成员时返回 null（不啰嗦、不误导） */
export function formatCoverage(group) {
  const stats = sectorStats(group)
  if (stats.symbolCount === 0) return '暂无成分股'
  if (stats.quotedCount >= stats.symbolCount) return null
  return `行情覆盖 ${stats.quotedCount} / ${stats.symbolCount}`
}

/**
 * 市场摘要：上涨 / 下跌 / 平盘板块数 + 最强 / 最弱板块。
 *
 * 口径说明：只统计**有有效行情**的板块（avg_change_percent 非 null），
 * 不做市值加权（当前没有流通市值数据），最强 / 最弱按等权平均涨跌幅比较。
 * 没有任何有效数据时，最强 / 最弱均为 null，由展示层显示「—」。
 */
export function summarizeMarket(groups) {
  const quoted = (groups ?? []).filter((group) => hasValue(group?.avg_change_percent))
  let up = 0
  let down = 0
  let flat = 0
  let strongest = null
  let weakest = null
  for (const group of quoted) {
    const value = Number(group.avg_change_percent)
    if (value > 0) up += 1
    else if (value < 0) down += 1
    else flat += 1
    if (strongest === null || value > Number(strongest.avg_change_percent)) strongest = group
    if (weakest === null || value < Number(weakest.avg_change_percent)) weakest = group
  }
  return { up, down, flat, strongest, weakest, quoted: quoted.length, total: (groups ?? []).length }
}

/** 本地股票搜索：名称 / 裸代码 / 展示代码，大小写不敏感；不请求后端 */
export function filterSymbols(rows, query) {
  const text = String(query ?? '').trim().toLowerCase()
  const list = rows ?? []
  if (!text) return [...list]
  return list.filter((row) =>
    [row?.name, row?.symbol, row?.display_code].some((field) =>
      String(field ?? '').toLowerCase().includes(text),
    ),
  )
}

/** 面包屑：市场 > 板块 > 个股（层级由当前状态决定，不额外请求） */
export function buildBreadcrumb({ group = null, symbol = null } = {}) {
  const crumbs = [{ key: 'market', label: '市场' }]
  if (group) crumbs.push({ key: 'sector', label: group.name, groupId: group.id })
  if (symbol) {
    crumbs.push({ key: 'symbol', label: symbol.name || symbol.display_code, symbolId: symbol.id })
  }
  return crumbs
}

/** 按类型筛选板块（'all' = 全部） */
export function filterGroups(groups, filter) {
  const list = groups ?? []
  if (!filter || filter === 'all') return [...list]
  return list.filter((group) => group.group_type === filter)
}
