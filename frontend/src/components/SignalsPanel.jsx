import React from 'react'

import {
  DEFAULT_SIGNAL_FILTERS,
  RECORD_KIND_LABEL,
  SIGNAL_FILTER_OPTIONS,
  describeFilters,
  recordKind,
} from '../filters/signals.js'
import { formatNumber, symbolLabel } from '../format.js'

/**
 * 信号区（中间）：展示最新 Signals，并对 pending 信号提供「模拟执行」按钮。
 *
 * 说明：只做展示与回调，信号的状态判断（谁可执行）由后端保证；
 * 这里显示 status 是为了让用户看清哪些信号还能执行。
 */
export default function SignalsPanel({
  signals,
  symbolsById,
  loading,
  error,
  autoRefresh,
  lastUpdated,
  selectedSignalId,
  busySignalId,
  onToggleAutoRefresh,
  onRefresh,
  onSelect,
  onExecute,
  filters = DEFAULT_SIGNAL_FILTERS,
  onFilterChange,
  loadedCount,
  filteredCount,
  offChartCount,
}) {
  return (
    <section className="panel panel-wide">
      <div className="panel-header">
        <h2>信号（Signals）</h2>
        <div className="panel-actions">
          <label className="checkbox">
            <input type="checkbox" checked={autoRefresh} onChange={onToggleAutoRefresh} />
            <span>自动刷新（10s）</span>
          </label>
          <button type="button" className="link-button" onClick={onRefresh} disabled={loading}>
            {loading ? '刷新中…' : '立即刷新'}
          </button>
        </div>
      </div>

      <div className="filter-bar">
        {SIGNAL_FILTER_OPTIONS.map((option) => (
          <label key={option.key} className="checkbox">
            <input
              type="checkbox"
              name={option.key}
              checked={Boolean(filters[option.key])}
              onChange={(event) => onFilterChange(option.key, event.target.checked)}
            />
            <span>{option.label}</span>
          </label>
        ))}
      </div>

      <p className="hint">
        显示 {signals.length} 条
        {typeof loadedCount === 'number'
          ? `（筛选后 ${filteredCount} / 已加载 ${loadedCount}）`
          : ''}
        {typeof offChartCount === 'number' && offChartCount > 0
          ? ` · 其中 ${offChartCount} 条不在当前 K 线范围（可放宽时间范围）`
          : ''}
        {lastUpdated ? ` · 最近刷新：${lastUpdated}` : ''}
        {selectedSignalId ? ` · 已选中信号 #${selectedSignalId}` : ''}
        <br />
        {describeFilters(filters)}
        　本列表与上方 K 线图使用同一份筛选结果
      </p>

      {error ? <p className="error-text">{error}</p> : null}

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>id</th>
              <th>timestamp</th>
              <th>标的</th>
              <th>direction</th>
              <th>price</th>
              <th>status</th>
              <th>reason</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {signals.length === 0 ? (
              <tr>
                <td colSpan={8} className="empty">
                  {filteredCount === 0 && loadedCount > 0
                    ? '当前筛选条件下没有交易信号（可调整上方开关查看其它记录）。'
                    : '暂无信号：选择策略与标的，点击「运行策略」后这里会出现新信号。'}
                </td>
              </tr>
            ) : (
              signals.map((signal) => {
                const selected = signal.id === selectedSignalId
                return (
                  <tr
                    key={signal.id}
                    className={selected ? 'row-selected' : undefined}
                    onClick={() => onSelect(signal)}
                  >
                    <td>{signal.id}</td>
                    <td className="mono">{signal.timestamp}</td>
                    <td>{symbolLabel(symbolsById, signal.symbol_id)}</td>
                    <td className={signal.direction === 'long' ? 'long' : 'short'}>
                      {signal.direction}
                    </td>
                    <td className="num">{formatNumber(signal.price)}</td>
                    <td>
                      <span className={`record-kind record-kind-${recordKind(signal)}`}>
                        {signal.status}
                      </span>
                      <span className="hint"> {RECORD_KIND_LABEL[recordKind(signal)]}</span>
                    </td>
                    <td className="reason">{signal.reason ?? '-'}</td>
                    <td>
                      {signal.status === 'pending' ? (
                        <button
                          type="button"
                          className="primary small"
                          disabled={busySignalId === signal.id}
                          onClick={(event) => {
                            event.stopPropagation()
                            onExecute(signal)
                          }}
                        >
                          {busySignalId === signal.id ? '执行中…' : '模拟执行'}
                        </button>
                      ) : (
                        <span className="hint">已处理</span>
                      )}
                    </td>
                  </tr>
                )
              })
            )}
          </tbody>
        </table>
      </div>
    </section>
  )
}
