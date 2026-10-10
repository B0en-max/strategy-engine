import React from 'react'

import { EMPTY_DISPLAY, formatPercent, formatQuote, formatSigned, quoteTone } from '../../market/view.js'
import MarketKlinePanel from './MarketKlinePanel.jsx'

/**
 * 个股详情（Stage 22）：个股行情头部 + 真实历史 K 线。
 *
 * 行情字段全部来自 /market/symbols/{id}（缺失一律显示 —，不补 0）；
 * K 线继续复用现有 MarketKlinePanel（市场工作区：红涨绿跌），
 * 与执行器页面的 KlineChartPanel（Stage 18）保持隔离，互不影响。
 */
export default function SymbolDetail({ symbol, candles, loading, error }) {
  const tone = quoteTone(symbol?.change_percent) || 'flat'

  return (
    <section className="panel symbol-detail">
      <div className="panel-header">
        <h2>{symbol ? symbol.name : '个股'}</h2>
        <span className="hint">{symbol ? symbol.display_code : '请从列表中选择个股'}</span>
      </div>

      {symbol ? (
        <>
          <div className="quote-headline">
            <span className="quote-price">{formatQuote(symbol.latest_price)}</span>
            <span className={`quote-change tone-${tone}`}>
              {formatSigned(symbol.change)} / {formatPercent(symbol.change_percent)}
            </span>
          </div>

          <dl className="quote-grid">
            <div className="kv">
              <dt>最新价</dt>
              <dd>{formatQuote(symbol.latest_price)}</dd>
            </div>
            <div className="kv">
              <dt>昨收</dt>
              <dd>{formatQuote(symbol.previous_close)}</dd>
            </div>
            <div className="kv">
              <dt>涨跌</dt>
              <dd className={`tone-${tone}`}>{formatSigned(symbol.change)}</dd>
            </div>
            <div className="kv">
              <dt>涨跌幅</dt>
              <dd className={`tone-${tone}`}>{formatPercent(symbol.change_percent)}</dd>
            </div>
            <div className="kv">
              <dt>成交量</dt>
              <dd>{formatQuote(symbol.volume, 0)}</dd>
            </div>
            <div className="kv">
              <dt>成交额</dt>
              <dd>{formatQuote(symbol.amount, 0)}</dd>
            </div>
          </dl>
          {symbol.timestamp ? null : <p className="hint">该标的暂无行情数据（显示 {EMPTY_DISPLAY}）。</p>}
        </>
      ) : (
        <p className="hint">尚未选择个股。</p>
      )}

      <MarketKlinePanel symbol={symbol} candles={candles} loading={loading} error={error} />
    </section>
  )
}
