/**
 * 前端测试（Stage 20 Step 3）：市场 / 选股页的纯展示逻辑。
 *
 * 覆盖：null → '—'（不转 0）、涨跌符号、涨跌幅格式、着色方向、
 * 表头排序（含缺失值排最后、同列升降序切换）、分组归类。
 */

import { describe, expect, it } from 'vitest'

import {
  EMPTY_DISPLAY,
  GROUP_SECTIONS,
  HEAT_THRESHOLDS,
  MARKET_VIEWS,
  SORTABLE_COLUMNS,
  TABLE_COLUMN_COUNT,
  VIEW_FILTERS,
  buildBreadcrumb,
  filterGroups,
  filterSymbols,
  formatCell,
  formatCoverage,
  formatPercent,
  formatQuote,
  formatSigned,
  groupByType,
  heatClass,
  heatLevel,
  nextSortState,
  quoteTone,
  sectorStats,
  sortSymbols,
  summarizeMarket,
} from '../src/market/view.js'

const ROWS = [
  { id: 1, symbol: '600519', display_code: '600519.SH', latest_price: 110, change: 5, change_percent: 4.76, volume: 1000, amount: null },
  { id: 2, symbol: '300750', display_code: '300750.SZ', latest_price: 210, change: -3, change_percent: -1.41, volume: 2000, amount: 12345 },
  { id: 3, symbol: 'BTCUSDT', display_code: 'BTCUSDT', latest_price: null, change: null, change_percent: null, volume: null, amount: null },
]

describe('行情格式化（缺失一律 —，绝不当作 0）', () => {
  it('formatQuote：null / undefined / 空串 → —', () => {
    expect(formatQuote(null)).toBe(EMPTY_DISPLAY)
    expect(formatQuote(undefined)).toBe(EMPTY_DISPLAY)
    expect(formatQuote('')).toBe(EMPTY_DISPLAY)
    expect(formatQuote(Number.NaN)).toBe(EMPTY_DISPLAY)
    expect(EMPTY_DISPLAY).toBe('—')
  })

  it('formatQuote：0 是真实值，仍然显示 0', () => {
    expect(formatQuote(0)).toBe('0')
    expect(formatQuote(110)).toBe('110')
    expect(formatQuote(1258.625)).toBe('1258.63')
    expect(formatQuote(342570, 0)).toBe('342570')
  })

  it('formatSigned：正数补 +、负数补 -、0 原样、缺失 —', () => {
    expect(formatSigned(5)).toBe('+5')
    expect(formatSigned(-3)).toBe('-3')
    expect(formatSigned(0)).toBe('0')
    expect(formatSigned(null)).toBe(EMPTY_DISPLAY)
  })

  it('formatPercent：带 % 与符号', () => {
    expect(formatPercent(4.76)).toBe('+4.76%')
    expect(formatPercent(-1.41)).toBe('-1.41%')
    expect(formatPercent(null)).toBe(EMPTY_DISPLAY)
  })

  it('quoteTone：只给方向，0 与缺失都不着色', () => {
    expect(quoteTone(1)).toBe('up')
    expect(quoteTone(-1)).toBe('down')
    expect(quoteTone(0)).toBe('')
    expect(quoteTone(null)).toBe('')
  })

  it('formatCell 与表头定义共用同一套格式', () => {
    const columns = Object.fromEntries(SORTABLE_COLUMNS.map((column) => [column.key, column]))
    expect(formatCell(ROWS[0], columns.latest_price)).toBe('110')
    expect(formatCell(ROWS[0], columns.change)).toBe('+5')
    expect(formatCell(ROWS[0], columns.change_percent)).toBe('+4.76%')
    expect(formatCell(ROWS[2], columns.latest_price)).toBe(EMPTY_DISPLAY)
    expect(formatCell(ROWS[0], columns.amount)).toBe(EMPTY_DISPLAY)
  })
})

describe('表头排序（只对已加载数据排序）', () => {
  it('可排序列就是需求里的 5 列，且列数正确', () => {
    expect(SORTABLE_COLUMNS.map((column) => column.key)).toEqual([
      'latest_price',
      'change',
      'change_percent',
      'volume',
      'amount',
    ])
    expect(TABLE_COLUMN_COUNT).toBe(7)
  })

  it('点击同一列切换升/降序；换列默认从降序开始', () => {
    expect(nextSortState({ key: null, direction: 'desc' }, 'change')).toEqual({
      key: 'change',
      direction: 'desc',
    })
    expect(nextSortState({ key: 'change', direction: 'desc' }, 'change')).toEqual({
      key: 'change',
      direction: 'asc',
    })
    expect(nextSortState({ key: 'change', direction: 'asc' }, 'change')).toEqual({
      key: 'change',
      direction: 'desc',
    })
    expect(nextSortState({ key: 'change', direction: 'asc' }, 'volume')).toEqual({
      key: 'volume',
      direction: 'desc',
    })
  })

  it('按最新价 / 涨跌 / 涨跌幅 / 成交量 / 成交额排序', () => {
    const ids = (sort) => sortSymbols(ROWS, sort).map((row) => row.id)
    expect(ids({ key: 'latest_price', direction: 'desc' })).toEqual([2, 1, 3])
    expect(ids({ key: 'latest_price', direction: 'asc' })).toEqual([1, 2, 3])
    expect(ids({ key: 'change', direction: 'desc' })).toEqual([1, 2, 3])
    expect(ids({ key: 'change_percent', direction: 'asc' })).toEqual([2, 1, 3])
    expect(ids({ key: 'volume', direction: 'desc' })).toEqual([2, 1, 3])
    expect(ids({ key: 'amount', direction: 'desc' })).toEqual([2, 1, 3])
  })

  it('行情缺失的行永远排在最后（无论升序还是降序）', () => {
    expect(sortSymbols(ROWS, { key: 'latest_price', direction: 'desc' })[2].id).toBe(3)
    expect(sortSymbols(ROWS, { key: 'latest_price', direction: 'asc' })[2].id).toBe(3)
    expect(sortSymbols(ROWS, { key: 'amount', direction: 'desc' })[2].id).toBe(3)
  })

  it('未指定排序时保持后端返回的顺序，且不改动入参', () => {
    const original = ROWS.map((row) => row.id)
    expect(sortSymbols(ROWS, { key: null }).map((row) => row.id)).toEqual([1, 2, 3])
    expect(sortSymbols(ROWS, null).map((row) => row.id)).toEqual([1, 2, 3])
    expect(ROWS.map((row) => row.id)).toEqual(original)
  })
})

describe('左侧分组归类', () => {
  it('按 industry / concept / watchlist / custom 四段归类，顺序固定', () => {
    expect(GROUP_SECTIONS.map((section) => section.type)).toEqual([
      'industry',
      'concept',
      'watchlist',
      'custom',
    ])
    const sections = groupByType([
      { id: 1, name: '半导体', group_type: 'industry', symbol_count: 2 },
      { id: 2, name: 'AI', group_type: 'concept', symbol_count: 1 },
      { id: 3, name: '我的自选', group_type: 'watchlist', symbol_count: 3 },
    ])
    expect(sections.industry.map((group) => group.name)).toEqual(['半导体'])
    expect(sections.concept.map((group) => group.name)).toEqual(['AI'])
    expect(sections.watchlist.map((group) => group.name)).toEqual(['我的自选'])
    expect(sections.custom).toEqual([])
  })

  it('空输入不报错', () => {
    expect(groupByType(null).industry).toEqual([])
    expect(groupByType([]).custom).toEqual([])
  })
})

describe('板块热力（Stage 22：红涨绿跌 + 深浅档位）', () => {
  it('涨跌幅强度分档：接近 0 / 缺失都是 0（中性）', () => {
    expect(heatLevel(null)).toBe(0)
    expect(heatLevel(0)).toBe(0)
    expect(heatLevel(0.05)).toBe(0)
    expect(heatLevel(0.2)).toBe(1)
    expect(heatLevel(1.5)).toBe(2)
    expect(heatLevel(3)).toBe(3)
    expect(heatLevel(5)).toBe(4)
    expect(heatLevel(-9.9)).toBe(4)
    expect(HEAT_THRESHOLDS).toHaveLength(4)
  })

  it('heatClass：上涨红、下跌绿、中性不着色', () => {
    expect(heatClass(3.24)).toBe('sector-tile-up sector-tile-up-3')
    expect(heatClass(-0.72)).toBe('sector-tile-down sector-tile-down-1')
    expect(heatClass(0)).toBe('sector-tile-neutral')
    expect(heatClass(null)).toBe('sector-tile-neutral')
    expect(heatClass(undefined)).toBe('sector-tile-neutral')
  })

  it('sectorStats：把后端聚合读数归一，缺失不补 0', () => {
    const stats = sectorStats({
      symbol_count: 50,
      quoted_count: 42,
      up_count: 38,
      down_count: 4,
      flat_count: 0,
      avg_change_percent: 3.24,
      quote_coverage: 0.84,
    })
    expect(stats).toEqual({
      symbolCount: 50,
      quotedCount: 42,
      upCount: 38,
      downCount: 4,
      flatCount: 0,
      avgChange: 3.24,
      coverage: 0.84,
    })
    const empty = sectorStats({ symbol_count: 0, quoted_count: 0, avg_change_percent: null })
    expect(empty.avgChange).toBeNull()
    expect(empty.coverage).toBeNull()
    expect(empty.symbolCount).toBe(0)
  })

  it('formatCoverage：覆盖不足才提示，覆盖完整 / 空板块不啰嗦', () => {
    expect(formatCoverage({ symbol_count: 50, quoted_count: 42 })).toBe('行情覆盖 42 / 50')
    expect(formatCoverage({ symbol_count: 42, quoted_count: 42 })).toBeNull()
    expect(formatCoverage({ symbol_count: 0, quoted_count: 0 })).toBe('暂无成分股')
  })
})

describe('市场摘要（Stage 22）', () => {
  const groups = [
    { id: 1, name: '半导体', group_type: 'industry', avg_change_percent: 3.24 },
    { id: 2, name: '银行', group_type: 'industry', avg_change_percent: 1.18 },
    { id: 3, name: '白酒', group_type: 'concept', avg_change_percent: -0.72 },
    { id: 4, name: '平盘', group_type: 'custom', avg_change_percent: 0 },
    { id: 5, name: '无行情', group_type: 'concept', avg_change_percent: null },
  ]

  it('统计上涨 / 下跌 / 平盘板块数，并给出最强 / 最弱板块', () => {
    const summary = summarizeMarket(groups)
    expect(summary.up).toBe(2)
    expect(summary.down).toBe(1)
    expect(summary.flat).toBe(1)
    expect(summary.strongest.name).toBe('半导体')
    expect(summary.weakest.name).toBe('白酒')
    expect(summary.quoted).toBe(4)
    expect(summary.total).toBe(5)
  })

  it('没有有效数据时最强 / 最弱为 null（展示层显示 —）', () => {
    const summary = summarizeMarket([{ id: 1, name: '无行情', avg_change_percent: null }])
    expect(summary.strongest).toBeNull()
    expect(summary.weakest).toBeNull()
    expect(summary.quoted).toBe(0)
    expect(summarizeMarket(null).total).toBe(0)
  })
})

describe('本地搜索与筛选（Stage 22：不新增后端接口）', () => {
  it('按名称 / 代码 / 展示代码过滤，大小写不敏感', () => {
    const withName = [{ id: 1, name: '贵州茅台', symbol: '600519', display_code: '600519.SH' }, ...ROWS.slice(2)]
    expect(filterSymbols(withName, '茅台').map((row) => row.id)).toEqual([1])
    expect(filterSymbols(ROWS, '600519').map((row) => row.id)).toEqual([1])
    expect(filterSymbols(ROWS, '600519.sh').map((row) => row.id)).toEqual([1])
    expect(filterSymbols(ROWS, 'btcusdt').map((row) => row.id)).toEqual([3])
    expect(filterSymbols(ROWS, '  ').map((row) => row.id)).toEqual([1, 2, 3])
    expect(filterSymbols(ROWS, '不存在')).toEqual([])
    expect(filterSymbols(null, 'x')).toEqual([])
  })

  it('按类型筛选板块（all = 全部）', () => {
    const groups = [
      { id: 1, name: '半导体', group_type: 'industry' },
      { id: 2, name: 'AI', group_type: 'concept' },
    ]
    expect(filterGroups(groups, 'all').map((g) => g.id)).toEqual([1, 2])
    expect(filterGroups(groups, 'concept').map((g) => g.id)).toEqual([2])
    expect(filterGroups(groups, 'watchlist')).toEqual([])
    expect(filterGroups(null, 'all')).toEqual([])
  })

  it('类型标签 = 全部 + 四个分类（复用 GROUP_SECTIONS 的顺序与中文名）', () => {
    expect(VIEW_FILTERS.map((item) => item.key)).toEqual(['all', 'industry', 'concept', 'watchlist', 'custom'])
    expect(VIEW_FILTERS.map((item) => item.label)).toEqual(['全部', '行业', '概念', '自选', '自定义'])
  })
})

describe('面包屑（Stage 22）', () => {
  it('市场 > 板块 > 个股，随状态增长', () => {
    expect(buildBreadcrumb({}).map((crumb) => crumb.label)).toEqual(['市场'])
    expect(buildBreadcrumb({ group: { id: 7, name: '半导体' } }).map((crumb) => crumb.label)).toEqual([
      '市场',
      '半导体',
    ])
    const crumbs = buildBreadcrumb({
      group: { id: 7, name: '半导体' },
      symbol: { id: 1, name: '贵州茅台', display_code: '600519.SH' },
    })
    expect(crumbs.map((crumb) => crumb.label)).toEqual(['市场', '半导体', '贵州茅台'])
    expect(crumbs.map((crumb) => crumb.key)).toEqual(['market', 'sector', 'symbol'])
  })

  it('直接从搜索结果进入个股时，面包屑退化为 市场 > 个股（不假造板块层级）', () => {
    const crumbs = buildBreadcrumb({ symbol: { id: 1, display_code: '600519.SH' } })
    expect(crumbs.map((crumb) => crumb.label)).toEqual(['市场', '600519.SH'])
  })

  it('MARKET_VIEWS 覆盖三层视图', () => {
    expect(MARKET_VIEWS).toEqual(['overview', 'sector', 'symbol'])
  })
})
