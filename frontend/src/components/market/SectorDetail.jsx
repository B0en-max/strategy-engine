import React from 'react'

import {
  formatCoverage,
  formatPercent,
  quoteTone,
  sectorStats,
} from '../../market/view.js'
import SymbolTable from './SymbolTable.jsx'

/**
 * 板块详情（Stage 22）：板块头部（涨跌幅 / 成分数 / 涨跌家数 / 行情覆盖率）+ 成分股列表。
 *
 * 头部读数全部来自 /market/groups 的聚合（与 Tile 同一个数据源，口径一致）；
 * 股票列表来自 /market/groups/{id}/symbols（点击板块时请求一次）。
 * 排序只对已加载数据生效，缺失值排最后（逻辑在 market/view.js）。
 */
export default function SectorDetail({
  group,
  rows,
  sort,
  onSortChange,
  selectedSymbolId,
  onSelectSymbol,
  loading,
  error,
}) {
  const stats = sectorStats(group)
  const coverage = formatCoverage(group)

  return (
    <section className="panel sector-detail">
      <div className="panel-header">
        <h2>{group ? group.name : '板块'}</h2>
        {group ? (
          <span className={`sector-headline tone-${quoteTone(stats.avgChange) || 'flat'}`}>
            {formatPercent(stats.avgChange)}
          </span>
        ) : null}
      </div>

      {group ? (
        <div className="sector-facts">
          <span>
            共 <b>{stats.symbolCount}</b> 只股票
          </span>
          <span className="tone-up">{`上涨 ${stats.upCount}`}</span>
          <span className="tone-down">{`下跌 ${stats.downCount}`}</span>
          <span>{`平盘 ${stats.flatCount}`}</span>
          {coverage ? <span className="hint">{coverage}</span> : null}
          <span className="hint">（等权平均，仅统计有有效行情的成分）</span>
        </div>
      ) : (
        <p className="hint">未选择板块。</p>
      )}

      <SymbolTable
        rows={rows}
        sort={sort}
        onSortChange={onSortChange}
        selectedSymbolId={selectedSymbolId}
        onSelectSymbol={onSelectSymbol}
        loading={loading}
        error={error}
        title="板块成分股"
      />
    </section>
  )
}
