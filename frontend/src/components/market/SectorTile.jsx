import React from 'react'

import {
  EMPTY_DISPLAY,
  formatCoverage,
  formatPercent,
  heatClass,
  quoteTone,
  sectorStats,
} from '../../market/view.js'

/**
 * 板块 Tile（Stage 22）——市场总览里每个板块一个独立卡片。
 *
 * 只展示后端 /market/groups 已经算好的聚合读数，前端不做任何行情计算：
 *   板块名称 / 涨跌幅 / 股票数量 / 上涨数量 / 下跌数量（+ 覆盖不足时的提示）
 *
 * 配色遵循中国市场习惯：上涨红、下跌绿、接近 0 与无行情中性；
 * 颜色深浅由 view.js 的 heatClass() 按涨跌幅强度给出档位。
 *
 * 预留（不显示假分数）：Sector Strength / Leader Score 槽位。
 * 后续阶段把板块强度、龙头评分接进来时，只需让数据里带上
 * sector_strength / leader_score 数字字段即可，组件结构不需要改。
 */
export default function SectorTile({ group, onSelect }) {
  const stats = sectorStats(group)
  const coverage = formatCoverage(group)
  const tone = quoteTone(stats.avgChange)

  return (
    <button
      type="button"
      className={`sector-tile ${heatClass(stats.avgChange)}`}
      onClick={() => onSelect(group)}
      title={`${group.name} · 成分 ${stats.symbolCount} 只 · 有行情 ${stats.quotedCount} 只`}
    >
      <span className="tile-name">{group.name}</span>
      <span className={`tile-change tone-${tone || 'flat'}`}>{formatPercent(stats.avgChange)}</span>

      <span className="tile-counts">
        <span className="tile-up">{`↑ ${stats.upCount}`}</span>
        <span className="tile-down">{`↓ ${stats.downCount}`}</span>
        <span className="tile-flat">{`= ${stats.flatCount}`}</span>
      </span>

      <span className="tile-meta">
        {`${stats.symbolCount} 只${coverage ? ` · ${coverage}` : ''}`}
      </span>

      {/* 预留槽位：Sector Strength / Leader Score（Stage 23+ 接入，当前无数据则不渲染） */}
      {typeof group.sector_strength === 'number' || typeof group.leader_score === 'number' ? (
        <span className="tile-scores">
          {typeof group.sector_strength === 'number' ? `强度 ${group.sector_strength}` : null}
          {typeof group.leader_score === 'number' ? `龙头 ${group.leader_score}` : null}
        </span>
      ) : null}

      {stats.symbolCount === 0 ? <span className="tile-empty-hint">{`${EMPTY_DISPLAY} 暂无成分股`}</span> : null}
    </button>
  )
}
