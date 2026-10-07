import React from 'react'
import { formatNumber, symbolLabel } from '../format.js'

/** 订单区：最近 Orders（订单由后端执行器创建，前端只读）。 */
export default function OrdersPanel({ orders, symbolsById, loading, error, onRefresh }) {
  return (
    <section className="panel panel-wide">
      <div className="panel-header">
        <h2>订单（Orders）</h2>
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
              <th>order_type</th>
              <th>quantity</th>
              <th>price</th>
              <th>status</th>
              <th>created_at</th>
            </tr>
          </thead>
          <tbody>
            {orders.length === 0 ? (
              <tr>
                <td colSpan={9} className="empty">
                  暂无订单
                </td>
              </tr>
            ) : (
              orders.map((order) => (
                <tr key={order.id}>
                  <td>{order.id}</td>
                  <td>{order.signal_id ?? '-'}</td>
                  <td>{symbolLabel(symbolsById, order.symbol_id)}</td>
                  <td className={order.side === 'buy' ? 'long' : 'short'}>{order.side}</td>
                  <td>{order.order_type}</td>
                  <td className="num">{formatNumber(order.quantity, 4)}</td>
                  <td className="num">{formatNumber(order.price)}</td>
                  <td>{order.status}</td>
                  <td className="mono">{order.created_at}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  )
}
