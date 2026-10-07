import React from 'react'
import { formatNumber, symbolLabel } from '../format.js'

/**
 * 执行区：指定 Signal + quantity（可选 price）→ 模拟执行。
 * 两种入口共用同一套参数：
 *   1) 信号区点「模拟执行」按钮（直接执行该信号）
 *   2) 在这里点「执行选中信号」
 * price 留空表示使用信号自带价格（后端执行器默认行为）。
 */
export default function ExecutionPanel({
  selectedSignal,
  symbolsById,
  quantity,
  price,
  onQuantityChange,
  onPriceChange,
  onExecute,
  executing,
  result,
  error,
}) {
  const quantityNumber = Number(quantity)
  const quantityValid = quantity !== '' && Number.isFinite(quantityNumber) && quantityNumber > 0
  const priceNumber = Number(price)
  const priceValid = price === '' || (Number.isFinite(priceNumber) && priceNumber > 0)

  return (
    <section className="panel">
      <h2>模拟执行</h2>

      {selectedSignal ? (
        <ul className="kv">
          <li>
            <span>信号</span>
            <strong>#{selectedSignal.id}</strong>
          </li>
          <li>
            <span>标的</span>
            <strong>{symbolLabel(symbolsById, selectedSignal.symbol_id)}</strong>
          </li>
          <li>
            <span>方向</span>
            <strong>{selectedSignal.direction}</strong>
          </li>
          <li>
            <span>信号价</span>
            <strong>{formatNumber(selectedSignal.price)}</strong>
          </li>
          <li>
            <span>状态</span>
            <strong>{selectedSignal.status}</strong>
          </li>
        </ul>
      ) : (
        <p className="hint">未选中信号：点击信号列表中的一行，或在信号区直接点「模拟执行」。</p>
      )}

      <label className="field">
        <span>数量 quantity（必填，&gt; 0）</span>
        <input
          name="quantity"
          type="number"
          min="0"
          step="1"
          value={quantity}
          onChange={(event) => onQuantityChange(event.target.value)}
        />
      </label>

      <label className="field">
        <span>价格 price（可选，留空用信号价）</span>
        <input
          name="price"
          type="number"
          min="0"
          step="0.01"
          value={price}
          onChange={(event) => onPriceChange(event.target.value)}
        />
      </label>

      <button
        type="button"
        className="primary"
        onClick={onExecute}
        disabled={!selectedSignal || !quantityValid || !priceValid || executing}
      >
        {executing ? '执行中…' : '执行选中信号'}
      </button>

      {!quantityValid ? <p className="hint">数量必须是大于 0 的数字。</p> : null}

      {error ? <p className="error-text">{error}</p> : null}

      {result ? (
        <dl className="result">
          <dt>信号 / 订单 / 成交</dt>
          <dd>
            #{result.signal_id} / #{result.order_id} / #{result.trade_id}
          </dd>
          <dt>方向</dt>
          <dd>{result.side}</dd>
          <dt>成交</dt>
          <dd>
            {formatNumber(result.quantity, 4)} @ {formatNumber(result.price)}
          </dd>
          <dt>当前持仓</dt>
          <dd>
            {formatNumber(result.position_quantity, 4)} @ {formatNumber(result.position_avg_price)}
          </dd>
        </dl>
      ) : null}
    </section>
  )
}
