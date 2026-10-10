import React from 'react'

import { EMPTY_DISPLAY, SORTABLE_COLUMNS, TABLE_COLUMN_COUNT, formatCell, quoteTone } from '../../market/view.js'

/**
 * 市场标的表（Stage 20 Step 3；Stage 22 支持自定义标题）。
 *
 * 列：名称 / 代码 / 最新价 / 涨跌 / 涨跌幅 / 成交量 / 成交额。
 * - 代码直接用后端返回的 display_code（前端不推导 .SH / .SZ / .BJ）
 * - 行情缺失显示 `—`（不转成 0）
 * - 后五列点击表头排序（只在已加载数据上排序，不请求后端）
 */
export default function SymbolTable({
  rows,
  sort,
  onSortChange,
  selectedSymbolId,
  onSelectSymbol,
  loading,
  error,
  title = '标的列表',
}) {
  return (
    <section className="panel market-table-panel">
      <div className="panel-header">
        <h2>{title}</h2>
        <span className="hint">
          共 <b>{rows.length}</b> 个标的 · 行情缺失显示 {EMPTY_DISPLAY} · 点击表头排序（仅对已加载数据）
        </span>
      </div>
      {error ? <p className="error-text">{error}</p> : null}

      <div className="table-wrap market-table-wrap">
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>代码</th>
              {SORTABLE_COLUMNS.map((column) => (
                <th key={column.key} className="num">
                  <button
                    type="button"
                    className="sort-header"
                    onClick={() => onSortChange(column.key)}
                    title={`按${column.label}排序`}
                  >
                    {column.label}
                    {sort && sort.key === column.key ? (
                      <span className="sort-indicator">{sort.direction === 'asc' ? '↑' : '↓'}</span>
                    ) : null}
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={TABLE_COLUMN_COUNT} className="empty">
                  {loading ? '标的加载中…' : '当前条件下没有标的'}
                </td>
              </tr>
            ) : (
              rows.map((row) => (
                <tr
                  key={row.id}
                  className={selectedSymbolId === row.id ? 'row-selected' : ''}
                  onClick={() => onSelectSymbol(row)}
                >
                  <td>{row.name ?? '-'}</td>
                  <td className="mono">{row.display_code ?? '-'}</td>
                  {SORTABLE_COLUMNS.map((column) => (
                    <td
                      key={column.key}
                      className={`num tone-${quoteTone(row[column.key]) || 'flat'}`}
                    >
                      {formatCell(row, column)}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      <p className="hint">代码列直接使用后端返回的 display_code；前端不推导交易所后缀。</p>
    </section>
  )
}
