import React, { useCallback, useEffect, useMemo, useState } from 'react'

import * as api from '../../api/client.js'
import { buildCandles } from '../../chart/klines.js'
import { MARKET_KLINE_LIMIT, MARKET_TIMEFRAME } from '../../config.js'
import { buildBreadcrumb, nextSortState } from '../../market/view.js'
import MarketOverview from './MarketOverview.jsx'
import SectorDetail from './SectorDetail.jsx'
import SymbolDetail from './SymbolDetail.jsx'

/**
 * 市场 / 选股工作区（Stage 22：市场总览 → 板块 → 个股 → K 线）。
 *
 * 数据流（每次用户动作最多一个请求，绝不逐个板块 / 逐个标的拉取）：
 *   市场总览   GET /market/groups                     一次拿到所有板块 + 行情聚合
 *   搜索       GET /market/symbols                    仅在首次输入搜索词时加载一次，之后本地过滤
 *   板块详情   GET /market/groups/{id}/symbols        点击某个板块时
 *   个股详情   GET /market/symbols/{id} + .../klines  点击某只股票时
 *
 * 状态机：view = overview | sector | symbol，配合 selectedGroupId / selectedSymbolId；
 * 面包屑（市场 > 板块 > 个股）由当前状态生成，点击即可回退到对应层级。
 *
 * 边界：不接触策略 / 执行器 / Signal / Trade；不做选股算法与评分。
 */
const INITIAL_SORT = { key: null, direction: 'desc' }

export default function MarketPage() {
  // ---- 导航状态 --------------------------------------------------------
  const [view, setView] = useState('overview')
  const [filter, setFilter] = useState('all')
  const [search, setSearch] = useState('')
  const [sort, setSort] = useState(INITIAL_SORT)

  // ---- 板块（总览 + 详情头部的读数）------------------------------------
  const [groups, setGroups] = useState([])
  const [groupsState, setGroupsState] = useState({ loading: true, error: null })
  const [selectedGroupId, setSelectedGroupId] = useState(null)

  // ---- 标的列表（板块成分 / 搜索结果）---------------------------------
  const [sectorRows, setSectorRows] = useState([])
  const [sectorState, setSectorState] = useState({ loading: false, error: null })
  const [allRows, setAllRows] = useState([])
  const [allState, setAllState] = useState({ loading: false, error: null, loaded: false })

  // ---- 个股详情 --------------------------------------------------------
  const [selectedSymbolId, setSelectedSymbolId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [detailState, setDetailState] = useState({ loading: false, error: null })
  const [klines, setKlines] = useState([])
  const [klinesState, setKlinesState] = useState({ loading: false, error: null })

  // ------------------------------------------------------------------ 加载
  const loadGroups = useCallback(async () => {
    setGroupsState({ loading: true, error: null })
    try {
      const rows = await api.listMarketGroups({ activeOnly: true, timeframe: MARKET_TIMEFRAME })
      setGroups(rows)
      setGroupsState({ loading: false, error: null })
    } catch (error) {
      setGroups([])
      setGroupsState({ loading: false, error: error.message })
    }
  }, [])

  const loadSectorRows = useCallback(async (groupId) => {
    if (groupId === null) return
    // 切换板块先清空，避免残留上一个板块的股票
    setSectorRows([])
    setSectorState({ loading: true, error: null })
    try {
      const rows = await api.listMarketGroupSymbols({ groupId, timeframe: MARKET_TIMEFRAME })
      setSectorRows(rows)
      setSectorState({ loading: false, error: null })
    } catch (error) {
      setSectorRows([])
      setSectorState({ loading: false, error: error.message })
    }
  }, [])

  const loadAllRows = useCallback(async () => {
    setAllState({ loading: true, error: null, loaded: true })
    try {
      const rows = await api.listMarketSymbols({ timeframe: MARKET_TIMEFRAME })
      setAllRows(rows)
      setAllState({ loading: false, error: null, loaded: true })
    } catch (error) {
      setAllRows([])
      setAllState({ loading: false, error: error.message, loaded: true })
    }
  }, [])

  const loadSymbolDetail = useCallback(async (symbolId) => {
    setDetail(null)
    setKlines([])
    setDetailState({ loading: true, error: null })
    setKlinesState({ loading: true, error: null })

    try {
      setDetail(await api.getMarketSymbol(symbolId, { timeframe: MARKET_TIMEFRAME }))
      setDetailState({ loading: false, error: null })
    } catch (error) {
      setDetailState({ loading: false, error: error.message })
    }

    try {
      setKlines(
        await api.listMarketKlines(symbolId, {
          timeframe: MARKET_TIMEFRAME,
          limit: MARKET_KLINE_LIMIT,
        }),
      )
      setKlinesState({ loading: false, error: null })
    } catch (error) {
      setKlines([])
      setKlinesState({ loading: false, error: error.message })
    }
  }, [])

  useEffect(() => {
    loadGroups()
  }, [loadGroups])

  // 进入板块视图 → 取该板块成分股
  useEffect(() => {
    if (view !== 'sector' || selectedGroupId === null) return
    loadSectorRows(selectedGroupId)
  }, [view, selectedGroupId, loadSectorRows])

  // 首次输入搜索词 → 加载一次全市场标的；之后输入只在本地过滤
  useEffect(() => {
    if (String(search).trim() === '' || allState.loaded || allState.loading) return
    loadAllRows()
  }, [search, allState.loaded, allState.loading, loadAllRows])

  // 选中个股 → 取行情 + K 线
  useEffect(() => {
    if (selectedSymbolId === null) {
      setDetail(null)
      setKlines([])
      return
    }
    loadSymbolDetail(selectedSymbolId)
  }, [selectedSymbolId, loadSymbolDetail])

  // ------------------------------------------------------------------ 交互
  const handleSelectGroup = useCallback((group) => {
    setSelectedGroupId(group.id)
    setSelectedSymbolId(null)
    setSearch('')
    setSort(INITIAL_SORT)
    setView('sector')
  }, [])

  const handleSelectSymbol = useCallback((row) => {
    setSelectedSymbolId(row.id)
    setView('symbol')
  }, [])

  const backToOverview = useCallback(() => {
    setView('overview')
    setSelectedGroupId(null)
    setSelectedSymbolId(null)
    setSort(INITIAL_SORT)
  }, [])

  const backToSector = useCallback(() => {
    setView('sector')
    setSelectedSymbolId(null)
  }, [])

  const handleSortChange = useCallback((key) => {
    setSort((previous) => nextSortState(previous, key))
  }, [])

  const handleCrumb = useCallback(
    (crumb) => {
      if (crumb.key === 'market') backToOverview()
      else if (crumb.key === 'sector') backToSector()
    },
    [backToOverview, backToSector],
  )

  // ------------------------------------------------------------------ 派生
  const candles = useMemo(() => buildCandles(klines), [klines])
  const selectedGroup = useMemo(
    () => groups.find((group) => group.id === selectedGroupId) ?? null,
    [groups, selectedGroupId],
  )
  const breadcrumbSymbol = useMemo(() => {
    if (selectedSymbolId === null) return null
    if (detail && detail.id === selectedSymbolId) return detail
    return (
      allRows.find((row) => row.id === selectedSymbolId) ??
      sectorRows.find((row) => row.id === selectedSymbolId) ?? {
        id: selectedSymbolId,
        name: '个股',
      }
    )
  }, [selectedSymbolId, detail, allRows, sectorRows])
  const crumbs = useMemo(
    () => buildBreadcrumb({ group: selectedGroup, symbol: breadcrumbSymbol }),
    [selectedGroup, breadcrumbSymbol],
  )

  return (
    <div className="market-workspace">
      <nav className="market-breadcrumb" aria-label="市场层级">
        {crumbs.map((crumb, index) => (
          <React.Fragment key={crumb.key}>
            {index > 0 ? <span className="crumb-sep">›</span> : null}
            <button
              type="button"
              className={index === crumbs.length - 1 ? 'crumb current' : 'crumb'}
              onClick={() => handleCrumb(crumb)}
              disabled={index === crumbs.length - 1}
            >
              {crumb.label}
            </button>
          </React.Fragment>
        ))}
      </nav>

      {view === 'overview' ? (
        <MarketOverview
          groups={groups}
          groupsState={groupsState}
          filter={filter}
          onFilterChange={setFilter}
          search={search}
          onSearchChange={setSearch}
          results={allRows}
          resultsState={allState}
          sort={sort}
          onSortChange={handleSortChange}
          selectedSymbolId={selectedSymbolId}
          onSelectSymbol={handleSelectSymbol}
          onSelectGroup={handleSelectGroup}
          onRefresh={loadGroups}
        />
      ) : view === 'sector' ? (
        <SectorDetail
          group={selectedGroup}
          rows={sectorRows}
          sort={sort}
          onSortChange={handleSortChange}
          selectedSymbolId={selectedSymbolId}
          onSelectSymbol={handleSelectSymbol}
          loading={sectorState.loading}
          error={sectorState.error}
        />
      ) : (
        <SymbolDetail
          symbol={detail}
          candles={candles}
          loading={klinesState.loading || detailState.loading}
          error={klinesState.error || detailState.error}
        />
      )}
    </div>
  )
}
