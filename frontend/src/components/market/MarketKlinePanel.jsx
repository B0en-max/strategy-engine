import React, { useEffect, useRef, useState } from 'react'
import { CandlestickSeries, createChart } from 'lightweight-charts'

import { fromChartTime } from '../../chart/klines.js'
import { formatPercent, formatQuote, formatSigned, quoteTone } from '../../market/view.js'

/**
 * 市场页的历史 K 线（Stage 20 Step 3）。
 *
 * 与 Stage 18 的 KlineChartPanel **业务隔离**：
 *   市场 K 线：K 线数据 → K 线图（本组件）
 *   执行器 K 线：K 线 + Signal + Trade → Stage 18 的 KlineChartPanel（未改动）
 * 本组件不接收也不读取任何 signal / trade / 持仓，只复用 lightweight-charts
 * 与 chart/klines.js 的纯换算能力（candles 由 MarketPage 用 buildCandles 生成）。
 */

/**
 * 市场工作区遵循 A 股习惯：**红涨绿跌**（与市场页股票表格 / 板块行情颜色语义一致）。
 * 仅用于本组件；Stage 18 的 KlineChartPanel 颜色语义保持不变，两者互不影响。
 */
const UP_COLOR = '#b42318'
const DOWN_COLOR = '#1a7f37'

export default function MarketKlinePanel({ symbol, candles, loading, error }) {
  const containerRef = useRef(null)
  const chartRef = useRef(null)
  const seriesRef = useRef(null)
  const [hovered, setHovered] = useState(null)

  useEffect(() => {
    const container = containerRef.current
    if (!container) return undefined
    const chart = createChart(container, {
      autoSize: true,
      layout: { background: { color: '#ffffff' }, textColor: '#1f2328' },
      grid: { vertLines: { color: '#eef1f5' }, horzLines: { color: '#eef1f5' } },
      rightPriceScale: { borderColor: '#d8dde4' },
      timeScale: { borderColor: '#d8dde4', timeVisible: true, secondsVisible: false, rightOffset: 3 },
    })
    const series = chart.addSeries(CandlestickSeries, {
      upColor: UP_COLOR,
      downColor: DOWN_COLOR,
      borderVisible: false,
      wickUpColor: UP_COLOR,
      wickDownColor: DOWN_COLOR,
      priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
    })

    const handleMove = (param) => {
      if (!param || !param.time) {
        setHovered(null)
        return
      }
      const data = param.seriesData ? param.seriesData.get(series) : null
      setHovered(data ? { time: param.time, ohlc: data } : null)
    }
    chart.subscribeCrosshairMove(handleMove)
    chartRef.current = chart
    seriesRef.current = series

    return () => {
      chart.unsubscribeCrosshairMove(handleMove)
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
    }
  }, [])

  useEffect(() => {
    const series = seriesRef.current
    const chart = chartRef.current
    if (!series || !chart) return
    series.setData(
      candles.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })),
    )
    if (candles.length > 0) chart.timeScale().fitContent()
    setHovered(null)
  }, [candles])

  const lastCandle = candles.length > 0 ? candles[candles.length - 1] : null
  const shown = hovered && hovered.ohlc ? hovered : lastCandle ? { time: lastCandle.time, ohlc: lastCandle } : null

  return (
    <section className="panel market-kline-panel">
      <div className="panel-header">
        <h2>历史 K 线</h2>
        {symbol ? (
          <span className="hint">
            {symbol.name} · <b>{symbol.display_code}</b> · 最新价 {formatQuote(symbol.latest_price)} · 涨跌{' '}
            <span className={`tone-${quoteTone(symbol.change) || 'flat'}`}>{formatSigned(symbol.change)}</span> ·
            涨跌幅{' '}
            <span className={`tone-${quoteTone(symbol.change_percent) || 'flat'}`}>
              {formatPercent(symbol.change_percent)}
            </span>
          </span>
        ) : (
          <span className="hint">在上方列表点击一行查看历史 K 线</span>
        )}
      </div>

      {error ? <p className="error-text">{error}</p> : null}
      {!symbol ? <p className="hint">尚未选择标的。</p> : null}
      {symbol && loading ? <p className="hint">K 线加载中…</p> : null}
      {symbol && !loading && candles.length === 0 ? (
        <p className="hint">
          该标的在 {symbol.timeframe} 周期没有 K 线数据（缺失即显示为空，不伪造行情）。
        </p>
      ) : null}

      <div className="chart-body" ref={containerRef} />

      {shown ? (
        <p className="hint chart-legend">
          {fromChartTime(shown.time)} UTC · O {formatQuote(shown.ohlc.open)} H {formatQuote(shown.ohlc.high)} L{' '}
          {formatQuote(shown.ohlc.low)} C {formatQuote(shown.ohlc.close)} · 共 <b>{candles.length}</b> 根
        </p>
      ) : null}
    </section>
  )
}
