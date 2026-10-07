import React from 'react'
import { formatNumber, symbolLabel } from '../format.js'

/** 成交区：最近 Trades（成交由后端执行器写入，前端只读）。 */
export default function TradesPanel({ trades, symbolsById, loading, error, onRefresh }) {
  return (
    <section className="panel panel-wide">
      <div className="panel-header">
        <h2>成交（Trades）</h2>
        <button type="button" className="link-button" onClick={onRefresh} disabled={loading}>
          {loading ? '刷新中…' : '刷新'}
        </button>
      </div>
      {error ? <p className="error-text">{error}</p> : null}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>id</th>
              <th>signal</th>
              <th>标的</th>
              <th>side</th>
              <th>quantity</th>
              <th>price</th>
              <th>fee</th>
              <th>timestamp</th>
            </tr>
          </thead>
          <tbody>
            {trades.length === 0 ? (
              <tr>
                <td colSpan={8} className="empty">
                  暂无成交
                </td>
              </tr>
            ) : (
              trades.map((trade) => (
                <tr key={trade.id}>
                  <td>{trade.id}</td>
                  <td>{trade.signal_id ?? '-'}</td>
                  <td>{symbolLabel(symbolsById, trade.symbol_id)}</td>
                  <td className={trade.side === 'buy' ? 'long' : 'short'}>{trade.side}</td>
                  <td className="num">{formatNumber(trade.quantity, 4)}</td>
                  <td className="num">{formatNumber(trade.price)}</td>
                  <td className="num">{formatNumber(trade.fee, 4)}</td>
                  <td className="mono">{trade.timestamp}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  )
}
