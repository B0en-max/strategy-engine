/**
 * 前端测试（Stage 22）：市场工作区组件渲染（react-dom/server，不引入 jsdom）。
 *
 * 覆盖：板块 Tile（名称 / 涨跌幅 / 成分数 / 涨跌家数 / 覆盖提示 / 红涨绿跌中性）、
 * 市场总览（摘要、类型标签、Tile 网格、搜索表格、loading / empty / error）、
 * 板块详情（头部读数 + 成分股表）、个股详情（行情字段 + K 线面板 + 缺失显示 —）、
 * 标的表（字段 / display_code 原样 / null → — / 排序指示 / 三态）、
 * 历史 K 线面板（含空态与错误）、MarketPage 首屏结构、顶层工作区入口与默认页。
 *
 * 交互类行为（点击 Tile / 点击股票 / 面包屑回退 / 搜索输入 / 真实网络）无法用静态渲染覆盖，
 * 由 view.js 的纯逻辑测试 + 无头浏览器验收（verify/）负责。
 */

import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import App from '../src/App.jsx'
import MarketKlinePanel from '../src/components/market/MarketKlinePanel.jsx'
import MarketOverview from '../src/components/market/MarketOverview.jsx'
import MarketPage from '../src/components/market/MarketPage.jsx'
import SectorDetail from '../src/components/market/SectorDetail.jsx'
import SectorTile from '../src/components/market/SectorTile.jsx'
import SymbolDetail from '../src/components/market/SymbolDetail.jsx'
import SymbolTable from '../src/components/market/SymbolTable.jsx'
import { toChartTime } from '../src/chart/klines.js'

const render = (element) => renderToStaticMarkup(element)

const noop = () => {}

/** 后端 /market/groups 返回的板块（含 Stage 22 聚合字段）*/
const UP_SECTOR = {
  id: 1,
  name: '半导体',
  group_type: 'industry',
  market: 'CN',
  is_active: 1,
  symbol_count: 50,
  quoted_count: 42,
  up_count: 38,
  down_count: 4,
  flat_count: 0,
  avg_change_percent: 3.24,
  quote_coverage: 0.84,
  timeframe: '1d',
}

const DOWN_SECTOR = {
  ...UP_SECTOR,
  id: 2,
  name: '白酒',
  group_type: 'industry',
  symbol_count: 31,
  quoted_count: 30,
  up_count: 9,
  down_count: 21,
  flat_count: 0,
  avg_change_percent: -0.72,
  quote_coverage: 30 / 31,
}

const NO_QUOTE_SECTOR = {
  ...UP_SECTOR,
  id: 3,
  name: '暂无行情',
  group_type: 'concept',
  symbol_count: 10,
  quoted_count: 0,
  up_count: 0,
  down_count: 0,
  flat_count: 0,
  avg_change_percent: null,
  quote_coverage: 0,
}

const EMPTY_SECTOR = {
  ...UP_SECTOR,
  id: 4,
  name: '空板块',
  group_type: 'custom',
  symbol_count: 0,
  quoted_count: 0,
  up_count: 0,
  down_count: 0,
  flat_count: 0,
  avg_change_percent: null,
  quote_coverage: null,
}

const GROUPS = [UP_SECTOR, DOWN_SECTOR, NO_QUOTE_SECTOR, EMPTY_SECTOR]

const ROWS = [
  {
    id: 1,
    symbol: '600519',
    name: '贵州茅台',
    market: 'CN',
    asset_type: 'STOCK',
    display_code: '600519.SH',
    timeframe: '1d',
    latest_price: 1452,
    previous_close: 1420,
    change: 32,
    change_percent: 2.25,
    volume: 3833098,
    amount: null,
    timestamp: '2026-09-30 00:00:00',
  },
  {
    id: 3,
    symbol: 'BTCUSDT',
    name: 'Bitcoin',
    market: 'CRYPTO',
    asset_type: 'CRYPTO',
    display_code: 'BTCUSDT',
    timeframe: '1d',
    latest_price: null,
    previous_close: null,
    change: null,
    change_percent: null,
    volume: null,
    amount: null,
    timestamp: null,
  },
]

const CANDLES = [
  { time: toChartTime('2026-09-29 00:00:00'), open: 100, high: 106, low: 99, close: 105, timestamp: '2026-09-29 00:00:00' },
  { time: toChartTime('2026-09-30 00:00:00'), open: 105, high: 112, low: 104, close: 110, timestamp: '2026-09-30 00:00:00' },
]

const overviewProps = {
  groups: GROUPS,
  groupsState: { loading: false, error: null },
  filter: 'all',
  onFilterChange: noop,
  search: '',
  onSearchChange: noop,
  results: ROWS,
  resultsState: { loading: false, error: null, loaded: false },
  sort: { key: null, direction: 'desc' },
  onSortChange: noop,
  selectedSymbolId: null,
  onSelectSymbol: noop,
  onSelectGroup: noop,
  onRefresh: noop,
}

describe('SectorTile（板块 Tile）', () => {
  it('显示板块名称、涨跌幅、成分数、上涨/下跌家数', () => {
    const html = render(<SectorTile group={UP_SECTOR} onSelect={noop} />)
    expect(html).toContain('半导体')
    expect(html).toContain('+3.24%')
    expect(html).toContain('50 只')
    expect(html).toContain('↑ 38')
    expect(html).toContain('↓ 4')
    expect(html).toContain('<button') // 每个板块是独立可点击 Tile
  })

  it('行情覆盖不足时给出提示，不分歧：42 / 50', () => {
    expect(render(<SectorTile group={UP_SECTOR} onSelect={noop} />)).toContain('行情覆盖 42 / 50')
    // 覆盖完整（我只要造一个 quoted === symbol_count 的板块）
    const full = { ...UP_SECTOR, symbol_count: 42, quoted_count: 42, quote_coverage: 1 }
    expect(render(<SectorTile group={full} onSelect={noop} />)).not.toContain('行情覆盖')
  })

  it('上涨 = 红色（sector-tile-up 档位）', () => {
    const html = render(<SectorTile group={UP_SECTOR} onSelect={noop} />)
    expect(html).toContain('sector-tile-up-3') // 3.24% → 第 3 档
    expect(html).not.toContain('sector-tile-down')
  })

  it('下跌 = 绿色（sector-tile-down 档位）', () => {
    const html = render(<SectorTile group={DOWN_SECTOR} onSelect={noop} />)
    expect(html).toContain('sector-tile-down-1') // -0.72% → 第 1 档
    expect(html).toContain('-0.72%')
  })

  it('无行情 = 中性：不着色、涨跌幅显示 —', () => {
    const html = render(<SectorTile group={NO_QUOTE_SECTOR} onSelect={noop} />)
    expect(html).toContain('sector-tile-neutral')
    expect(html).toContain('—')
    expect(html).not.toContain('sector-tile-up')
    expect(html).not.toContain('sector-tile-down')
  })

  it('空板块提示暂无成分股，不显示 0 伪装成行情', () => {
    const html = render(<SectorTile group={EMPTY_SECTOR} onSelect={noop} />)
    expect(html).toContain('暂无成分股')
  })

  it('不渲染任何虚假评分（Sector Strength / Leader Score 仅留槽位）', () => {
    expect(render(<SectorTile group={UP_SECTOR} onSelect={noop} />)).not.toContain('强度')
    expect(render(<SectorTile group={UP_SECTOR} onSelect={noop} />)).not.toContain('龙头')
  })
})

describe('MarketOverview（市场总览）', () => {
  it('渲染行情摘要：上涨 / 下跌 / 平盘板块 + 最强 / 最弱板块', () => {
    const html = render(<MarketOverview {...overviewProps} />)
    expect(html).toContain('上涨板块')
    expect(html).toContain('下跌板块')
    expect(html).toContain('平盘板块')
    expect(html).toContain('最强板块')
    expect(html).toContain('最弱板块')
    expect(html).toContain('半导体')
    expect(html).toContain('+3.24%')
    expect(html).toContain('-0.72%')
  })

  it('没有有效行情时摘要显示 —（不是 0）', () => {
    const html = render(
      <MarketOverview {...overviewProps} groups={[NO_QUOTE_SECTOR]} />,
    )
    expect(html).toContain('—')
    expect(html).toContain('其中 0 个有有效行情')
  })

  it('类型标签含全部 + 四个分类，并显示各分类数量', () => {
    const html = render(<MarketOverview {...overviewProps} />)
    for (const label of ['全部', '行业', '概念', '自选', '自定义']) expect(html).toContain(label)
    expect(html).toContain('sector-tabs')
    expect(html).toContain('tab-count')
  })

  it('每个板块渲染成一个独立 Tile（数量 = 板块数）', () => {
    const html = render(<MarketOverview {...overviewProps} />)
    expect(html.match(/class="sector-tile/g) ?? []).toHaveLength(GROUPS.length)
  })

  it('类型筛选只显示该类型板块（摘要仍为全市场口径）', () => {
    const html = render(<MarketOverview {...overviewProps} filter="concept" />)
    expect(html).toContain('暂无行情')
    // 网格里只剩 1 个 Tile（半导体/白酒/空板块都被筛掉）
    expect(html.match(/class="sector-tile /g) ?? []).toHaveLength(1)
    expect(html).not.toContain('<span class="tile-name">白酒</span>')
  })

  it('搜索时渲染本地过滤后的股票表，不发新请求', () => {
    const html = render(<MarketOverview {...overviewProps} search="茅台" />)
    expect(html).toContain('搜索结果')
    expect(html).toContain('贵州茅台')
    expect(html).not.toContain('Bitcoin')
  })

  it('搜索无匹配时显示空态文案', () => {
    const html = render(<MarketOverview {...overviewProps} search="不存在的股票" />)
    expect(html).toContain('当前条件下没有标的')
  })

  it('loading / error / 空板块三态都不白屏', () => {
    expect(
      render(<MarketOverview {...overviewProps} groupsState={{ loading: true, error: null }} />),
    ).toContain('板块加载中')
    expect(
      render(<MarketOverview {...overviewProps} groupsState={{ loading: false, error: 'API 返回错误（HTTP 500）' }} />),
    ).toContain('API 返回错误')
    expect(render(<MarketOverview {...overviewProps} groups={[]} />)).toContain('没有板块')
  })
})

describe('SectorDetail（板块详情）', () => {
  const props = {
    group: UP_SECTOR,
    rows: ROWS,
    sort: { key: null, direction: 'desc' },
    onSortChange: noop,
    selectedSymbolId: null,
    onSelectSymbol: noop,
    loading: false,
    error: null,
  }

  it('显示板块名、涨跌幅、成分数与涨跌家数', () => {
    const html = render(<SectorDetail {...props} />)
    expect(html).toContain('半导体')
    expect(html).toContain('+3.24%')
    expect(html).toContain('共 <b>50</b> 只股票')
    expect(html).toContain('上涨 38')
    expect(html).toContain('下跌 4')
    expect(html).toContain('行情覆盖 42 / 50')
  })

  it('下方是成分股列表（可排序表头）', () => {
    const html = render(<SectorDetail {...props} />)
    expect(html).toContain('板块成分股')
    for (const column of ['名称', '代码', '最新价', '涨跌', '涨跌幅', '成交量', '成交额']) {
      expect(html).toContain(column)
    }
    expect(html).toContain('600519.SH')
  })

  it('加载中与错误都有提示', () => {
    expect(render(<SectorDetail {...props} rows={[]} loading />)).toContain('标的加载中')
    expect(render(<SectorDetail {...props} rows={[]} error="API 返回错误（HTTP 404）" />)).toContain('API 返回错误')
  })

  it('未选择板块时不崩溃', () => {
    expect(render(<SectorDetail {...props} group={null} rows={[]} />)).toContain('未选择板块')
  })
})

describe('SymbolDetail（个股详情）', () => {
  it('显示最新价 / 昨收 / 涨跌 / 涨跌幅 / 成交量 / 成交额与 K 线面板', () => {
    const html = render(<SymbolDetail symbol={ROWS[0]} candles={CANDLES} loading={false} error={null} />)
    expect(html).toContain('贵州茅台')
    expect(html).toContain('600519.SH')
    expect(html).toContain('1452')
    expect(html).toContain('1420')
    expect(html).toContain('+32')
    expect(html).toContain('+2.25%')
    expect(html).toContain('3833098')
    expect(html).toContain('历史 K 线')
  })

  it('行情缺失字段显示 —（不补 0）', () => {
    const html = render(<SymbolDetail symbol={ROWS[1]} candles={[]} loading={false} error={null} />)
    expect(html).toContain('—')
    expect(html).toContain('暂无行情数据')
  })

  it('未选个股 / K 线加载中 / 无 K 线 / 错误都有提示', () => {
    expect(render(<SymbolDetail symbol={null} candles={[]} loading={false} error={null} />)).toContain('尚未选择个股')
    expect(render(<SymbolDetail symbol={ROWS[0]} candles={[]} loading error={null} />)).toContain('K 线加载中')
    expect(render(<SymbolDetail symbol={ROWS[0]} candles={[]} loading={false} error={null} />)).toContain('没有 K 线数据')
  })
})

describe('SymbolTable（标的列表）', () => {
  const props = {
    sort: { key: null, direction: 'desc' },
    onSortChange: noop,
    selectedSymbolId: 1,
    onSelectSymbol: noop,
  }

  it('表头包含需求中的七列，单元格显示字段值', () => {
    const html = render(<SymbolTable {...props} rows={ROWS} loading={false} error={null} />)
    for (const label of ['名称', '代码', '最新价', '涨跌', '涨跌幅', '成交量', '成交额']) {
      expect(html).toContain(label)
    }
    expect(html).toContain('贵州茅台')
    expect(html).toContain('+32')
    expect(html).toContain('+2.25%')
  })

  it('display_code 直接使用后端返回值（前端不推导后缀）', () => {
    const html = render(<SymbolTable {...props} rows={ROWS} loading={false} error={null} />)
    expect(html).toContain('600519.SH')
    expect(html).toContain('BTCUSDT')
  })

  it('行情缺失显示 —（不是 0）', () => {
    const html = render(<SymbolTable {...props} rows={[ROWS[1]]} loading={false} error={null} />)
    expect(html).toContain('—')
    expect(html).not.toContain('>0<')
  })

  it('排序状态在表头显示箭头', () => {
    const desc = render(
      <SymbolTable {...props} sort={{ key: 'change_percent', direction: 'desc' }} rows={ROWS} loading={false} error={null} />,
    )
    expect(desc).toContain('↓')
    const asc = render(
      <SymbolTable {...props} sort={{ key: 'change_percent', direction: 'asc' }} rows={ROWS} loading={false} error={null} />,
    )
    expect(asc).toContain('↑')
  })

  it('加载中 / 空数据 / 错误三种状态都有明确文案', () => {
    expect(render(<SymbolTable {...props} rows={[]} loading error={null} />)).toContain('标的加载中')
    expect(render(<SymbolTable {...props} rows={[]} loading={false} error={null} />)).toContain('当前条件下没有标的')
    expect(render(<SymbolTable {...props} rows={[]} loading={false} error="API 返回错误（HTTP 500）" />)).toContain(
      'API 返回错误',
    )
  })

  it('选中行带高亮样式，标题可定制', () => {
    const html = render(<SymbolTable {...props} rows={ROWS} loading={false} error={null} title="板块成分股" />)
    expect(html).toContain('row-selected')
    expect(html).toContain('板块成分股')
  })
})

describe('MarketKlinePanel（历史 K 线）', () => {
  it('展示标的信息、图例与根数', () => {
    const html = render(<MarketKlinePanel symbol={ROWS[0]} candles={CANDLES} loading={false} error={null} />)
    expect(html).toContain('历史 K 线')
    expect(html).toContain('贵州茅台')
    expect(html).toContain('600519.SH')
    expect(html).toContain('1452')
    expect(html).toContain('2026-09-30 00:00:00 UTC')
    expect(html).toContain('共')
  })

  it('未选标的 / 加载中 / 无 K 线 / 错误都有提示', () => {
    expect(render(<MarketKlinePanel symbol={null} candles={[]} loading={false} error={null} />)).toContain('尚未选择标的')
    expect(render(<MarketKlinePanel symbol={ROWS[0]} candles={[]} loading error={null} />)).toContain('K 线加载中')
    expect(render(<MarketKlinePanel symbol={ROWS[0]} candles={[]} loading={false} error={null} />)).toContain('没有 K 线数据')
    expect(
      render(<MarketKlinePanel symbol={ROWS[0]} candles={[]} loading={false} error="API 返回错误（HTTP 404）" />),
    ).toContain('API 返回错误')
  })
})

describe('MarketPage（首屏 / 结构）', () => {
  it('首屏 = 市场总览：面包屑 + 摘要 + 板块热力图容器，不白屏', () => {
    const html = render(<MarketPage />)
    expect(html).toContain('market-workspace')
    expect(html).toContain('market-breadcrumb')
    expect(html).toContain('市场总览')
    expect(html).toContain('market-summary')
    expect(html).toContain('上涨板块')
    expect(html).toContain('sector-tabs')
    expect(html).toContain('搜索股票名称 / 代码')
  })
})

describe('顶层工作区（市场选股 ↔ 策略执行）', () => {
  it('两个入口都在，默认停留在「策略执行」页（不破坏既有行为）', () => {
    const html = render(<App />)
    expect(html).toContain('策略执行')
    expect(html).toContain('市场选股')
    expect(html).toContain('workspace-tab active')
    // 默认页 = 策略执行：现有 Stage 17/18 的组件仍在
    expect(html).toContain('K 线（Klines）')
    expect(html).toContain('模拟执行')
    expect(html).toContain('持仓（Positions）')
    // 市场页默认不渲染
    expect(html).not.toContain('市场总览')
  })
})
