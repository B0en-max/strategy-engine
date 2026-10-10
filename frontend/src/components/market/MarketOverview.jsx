import React, { useMemo } from 'react'

import {
  EMPTY_DISPLAY,
  VIEW_FILTERS,
  filterGroups,
  filterSymbols,
  formatPercent,
  groupByType,
  quoteTone,
  sortSymbols,
  summarizeMarket,
} from '../../market/view.js'
import SectorTile from './SectorTile.jsx'
import SymbolTable from './SymbolTable.jsx'

/**
 * 市场总览（Stage 22）：行情摘要 + 类型筛选 + 板块热力图（Tile 网格）。
 *
 * 数据来源（全部来自 /market/groups 一次请求，前端不再逐个板块请求股票列表）：
 *   - 摘要：summarizeMarket(groups) —— 上涨 / 下跌 / 平盘板块数 + 最强 / 最弱板块
 *   - Tile：每个板块的等权平均涨跌幅、涨跌家数、成分数、行情覆盖率
 *
 * 搜索：在当前已加载的股票数据里本地过滤（名称 / 代码），不新增后端接口。
 * 明确不显示任何虚假评分（Sector Strength / Leader Score 只留槽位）。
 */
export default function MarketOverview({
  groups,
  groupsState,
  filter,
  onFilterChange,
  search,
  onSearchChange,
  results,
  resultsState,
  sort,
  onSortChange,
  selectedSymbolId,
  onSelectSymbol,
  onSelectGroup,
  onRefresh,
}) {
  const summary = useMemo(() => summarizeMarket(groups), [groups])
  const counts = useMemo(() => groupByType(groups), [groups])
  const tiles = useMemo(() => filterGroups(groups, filter), [groups, filter])
  const searchActive = String(search ?? '').trim() !== ''
  const matched = useMemo(
    () => sortSymbols(filterSymbols(results, search), sort),
    [results, search, sort],
  )

  return (
    <section className="panel market-overview">
      <div className="panel-header">
        <h2>市场总览</h2>
        <div className="overview-tools">
          <input
            type="search"
            className="search-input"
            placeholder="搜索股票名称 / 代码"
            aria-label="搜索股票名称或代码"
            value={search}
            onChange={(event) => onSearchChange(event.target.value)}
          />
          <button type="button" className="link-button" onClick={onRefresh}>
            刷新
          </button>
        </div>
      </div>

      {/* 行情摘要：没有有效数据时显示 —，不显示 0 */}
      <div className="market-summary">
        <span className="summary-item">
          <b className="tone-up">{summary.up}</b> 上涨板块
        </span>
        <span className="summary-item">
          <b className="tone-down">{summary.down}</b> 下跌板块
        </span>
        <span className="summary-item">
          <b>{summary.flat}</b> 平盘板块
        </span>
        <span className="summary-item">
          最强板块{' '}
          <b>{summary.strongest ? summary.strongest.name : EMPTY_DISPLAY}</b>{' '}
          <span className={`tone-${quoteTone(summary.strongest?.avg_change_percent) || 'flat'}`}>
            {summary.strongest ? formatPercent(summary.strongest.avg_change_percent) : EMPTY_DISPLAY}
          </span>
        </span>
        <span className="summary-item">
          最弱板块{' '}
          <b>{summary.weakest ? summary.weakest.name : EMPTY_DISPLAY}</b>{' '}
          <span className={`tone-${quoteTone(summary.weakest?.avg_change_percent) || 'flat'}`}>
            {summary.weakest ? formatPercent(summary.weakest.avg_change_percent) : EMPTY_DISPLAY}
          </span>
        </span>
        <span className="summary-item hint">
          {`（共 ${summary.total} 个板块，其中 ${summary.quoted} 个有有效行情）`}
        </span>
      </div>

      <nav className="sector-tabs">
        {VIEW_FILTERS.map((item) => {
          const count = item.key === 'all' ? groups.length : (counts[item.key] ?? []).length
          return (
            <button
              key={item.key}
              type="button"
              className={filter === item.key ? 'sector-tab active' : 'sector-tab'}
              onClick={() => onFilterChange(item.key)}
            >
              {item.label}
              <span className="tab-count">{count}</span>
            </button>
          )
        })}
      </nav>

      {searchActive ? (
        <SymbolTable
          rows={matched}
          sort={sort}
          onSortChange={onSortChange}
          selectedSymbolId={selectedSymbolId}
          onSelectSymbol={onSelectSymbol}
          loading={resultsState.loading}
          error={resultsState.error}
          title="搜索结果"
        />
      ) : groupsState.loading ? (
        <p className="hint">板块加载中…</p>
      ) : groupsState.error ? (
        <p className="error-text">{groupsState.error}</p>
      ) : tiles.length === 0 ? (
        <p className="hint">当前筛选下没有板块。</p>
      ) : (
        <div className="sector-grid">
          {tiles.map((group) => (
            <SectorTile key={group.id} group={group} onSelect={onSelectGroup} />
          ))}
        </div>
      )}
    </section>
  )
}
