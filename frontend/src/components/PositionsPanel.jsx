import React from 'react'
import { formatNumber, formatTimestamp, symbolLabel } from '../format.js'

/** 持仓区：symbol / quantity / avg_price（数值一律来自后端，前端不计算）。 */
export default function PositionsPanel({ positions, symbolsById, loading, error, onRefresh }) {
  return (
    <section className="panel">
      <div className="panel-header">
        <h2>持仓（Positions）</h2>
        <button type="button" className="link-button" onClick={onRefresh} disabled={loading}>
          {loading ? '刷新中…' : '刷新'}
        </button>
      </div>
      {error ? <p className="error-text">{error}</p> : null}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>标的</th>
              <th>quantity</th>
              <th>avg_price</th>
              <th>updated_at</th>
            </tr>
          </thead>
          <tbody>
            {positions.length === 0 ? (
              <tr>
                <td colSpan={4} className="empty">
                  暂无持仓
                </td>
              </tr>
            ) : (
              positions.map((position) => (
                <tr key={position.id}>
                  <td>{symbolLabel(symbolsById, position.symbol_id)}</td>
                  <td className="num">{formatNumber(position.quantity, 4)}</td>
                  <td className="num">{formatNumber(position.avg_price)}</td>
                  <td className="mono">{formatTimestamp(position.updated_at)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  )
}
