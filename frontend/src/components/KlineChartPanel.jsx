import React, { useEffect, useMemo, useRef, useState } from 'react'
import { CandlestickSeries, createChart, createSeriesMarkers } from 'lightweight-charts'

import {
  buildSignalMarkers,
  candleTimesOf,
  fromChartTime,
  groupSignalsByTime,
} from '../chart/klines.js'
import { formatNumber, symbolLabel } from '../format.js'

/**
 * K 线图 + 信号叠加（Stage 18）。
 *
 * 职责边界：
 *   - 只负责「画」和「展示」：K 线数据来自 App（/klines），信号来自 App（/strategies/{id}/signals）
 *   - 不做策略判断、不生成信号、不算持仓；不新增任何技术指标
 *   - 缩放 / 拖动 / 十字光标由 lightweight-charts 自带能力提供；数据范围用 fitContent 自动适配
 *
 * 信号与 K 线的对应关系：**按 timestamp 精确匹配**（见 chart/klines.js），
 * 匹配不上的信号数量会显示出来，绝不按下标对应。
 */

/** K 线图高度（px） */
const CHART_HEIGHT = 420

/** 蜡烛涨跌配色（与页面其它地方的涨跌色一致） */
const UP_COLOR = '#1a7f37'
const DOWN_COLOR = '#b42318'

export default function KlineChartPanel({
  candles,
  signals,
  symbolsById,
  symbolId,
  timeframe,
  loading,
  error,
  onRefresh,
  onSelectSignal,
}) {
  const containerRef = useRef(null)
  const chartRef = useRef(null)
  const seriesRef = useRef(null)
  const markersApiRef = useRef(null)

  // 鼠标位置（十字光标）对应的那根 K 线
  const [hovered, setHovered] = useState(null)
  // 点击选中的那根 K 线
  const [focusTime, setFocusTime] = useState(null)

  const candleTimes = useMemo(() => candleTimesOf(candles), [candles])
  const { markers, matched, unmatched } = useMemo(
    () => buildSignalMarkers(signals, candleTimes),
    [signals, candleTimes],
  )
  const signalsByTime = useMemo(
    () => groupSignalsByTime(signals, candleTimes),
    [signals, candleTimes],
  )

  // ---------------------------------------------------------------- 建图
  useEffect(() => {
    const container = containerRef.current
    if (!container) return undefined

    const chart = createChart(container, {
      autoSize: true,
      layout: {
        background: { color: '#ffffff' },
        textColor: '#1f2328',
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: '#eef1f5' },
        horzLines: { color: '#eef1f5' },
      },
      rightPriceScale: { borderColor: '#d8dde4' },
      timeScale: {
        borderColor: '#d8dde4',
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 4,
      },
    })

    const series = chart.addSeries(CandlestickSeries, {
      upColor: UP_COLOR,
      downColor: DOWN_COLOR,
      borderVisible: false,
      wickUpColor: UP_COLOR,
      wickDownColor: DOWN_COLOR,
      priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
    })
    const markersApi = createSeriesMarkers(series, [])

    const handleCrosshairMove = (param) => {
      if (!param || param.time === undefined) {
        setHovered(null)
        return
      }
      const data = param.seriesData.get(series)
      setHovered({
        time: param.time,
        ohlc:
          data && typeof data.open === 'number'
            ? { open: data.open, high: data.high, low: data.low, close: data.close }
            : null,
      })
    }
    const handleClick = (param) => {
      if (!param || param.time === undefined) return
      setFocusTime(param.time)
    }

    chart.subscribeCrosshairMove(handleCrosshairMove)
    chart.subscribeClick(handleClick)

    chartRef.current = chart
    seriesRef.current = series
    markersApiRef.current = markersApi

    return () => {
      chart.unsubscribeCrosshairMove(handleCrosshairMove)
      chart.unsubscribeClick(handleClick)
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
      markersApiRef.current = null
    }
  }, [])

  // ------------------------------------------------------- K 线数据写入
  // 切换标的 / 周期 / 时间范围时，App 会先清空 candles（旧 K 线随之消失），再写入新数据。
  useEffect(() => {
    const chart = chartRef.current
    const series = seriesRef.current
    if (!chart || !series) return

    series.setData(
      candles.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })),
    )
    if (candles.length > 0) chart.timeScale().fitContent() // 自动适配数据范围
    setHovered(null)
    setFocusTime(null)
  }, [candles])

  // ------------------------------------------------------- 信号标记更新
  // 「运行策略」/「模拟执行」后只刷新标记，不重置视口，保持当前 K 线
  useEffect(() => {
    const markersApi = markersApiRef.current
    if (!markersApi) return
    markersApi.setMarkers(markers)
  }, [markers])

  // ---------------------------------------------------------------- 展示
  const lastCandle = candles.length > 0 ? candles[candles.length - 1] : null
  const legendTime = hovered && hovered.time !== undefined ? hovered.time : lastCandle?.time ?? null
  const legendOhlc = hovered?.ohlc ?? (lastCandle
    ? { open: lastCandle.open, high: lastCandle.high, low: lastCandle.low, close: lastCandle.close }
    : null)

  const activeTime = (hovered && hovered.time !== undefined ? hovered.time : null) ?? focusTime
  const activeSignals =
    activeTime === null || activeTime === undefined ? [] : signalsByTime.get(activeTime) ?? []

  const changePercent =
    legendOhlc && legendOhlc.open !== 0
      ? ((legendOhlc.close - legendOhlc.open) / legendOhlc.open) * 100
      : null

  return (
    <section className="panel chart-panel">
      <div className="panel-header">
        <h2>
          K 线（Klines）
          <span className="hint">
            {' '}
            {symbolLabel(symbolsById, symbolId)} · {timeframe} · 最近 {candles.length} 根
          </span>
        </h2>
        <div className="panel-actions">
          <span className="hint">滚轮缩放 / 拖动平移 / 十字光标查看 · 点击标记看信号详情</span>
          <button type="button" className="link-button" onClick={onRefresh} disabled={loading}>
            {loading ? '刷新中…' : '刷新 K 线'}
          </button>
        </div>
      </div>

      {error ? <p className="error-text">K 线加载失败：{error}</p> : null}

      <div className="chart-legend">
        <span className="mono">{fromChartTime(legendTime)} UTC</span>
        {legendOhlc ? (
          <>
            <span>
              O <b className="num">{formatNumber(legendOhlc.open)}</b>
            </span>
            <span>
              H <b className="num">{formatNumber(legendOhlc.high)}</b>
            </span>
            <span>
              L <b className="num">{formatNumber(legendOhlc.low)}</b>
            </span>
            <span>
              C{' '}
              <b className={changePercent !== null && changePercent < 0 ? 'num short' : 'num long'}>
                {formatNumber(legendOhlc.close)}
              </b>
            </span>
            {changePercent !== null ? (
              <span className={changePercent < 0 ? 'short' : 'long'}>
                {changePercent >= 0 ? '+' : ''}
                {formatNumber(changePercent)}%
              </span>
            ) : null}
          </>
        ) : (
          <span className="hint">{loading ? '加载中…' : '暂无数据'}</span>
        )}
        <span className="hint chart-stats">
          信号 {signals.length} 条：已落在图上 <b>{matched}</b> 条
          {unmatched > 0 ? ` · 不在当前 K 线范围 ${unmatched} 条` : ''}
          {markers.length > 0 ? '（标记：↑绿=long / ↓红=short；实色=待执行，浅色带 ✓=已执行）' : ''}
        </span>
      </div>

      <div className="chart-body" ref={containerRef} style={{ height: `${CHART_HEIGHT}px` }} />

      {candles.length === 0 && !loading && !error ? (
        <p className="hint">该标的 / 周期在所选时间范围内没有 K 线数据（可换 15m / 1d 或清除时间范围）。</p>
      ) : null}

      {activeSignals.length > 0 ? (
        <div className="signal-detail">
          <div className="signal-detail-head">
            {fromChartTime(activeTime)} 上的信号（{activeSignals.length} 条）
            {hovered && hovered.time !== undefined ? ' · 悬停' : ' · 已点击'}
          </div>
          <table>
            <thead>
              <tr>
                <th>id</th>
                <th>timestamp</th>
                <th>direction</th>
                <th>price</th>
                <th>status</th>
                <th>reason</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {activeSignals.map((signal) => (
                <tr key={signal.id}>
                  <td>{signal.id}</td>
                  <td className="mono">{signal.timestamp}</td>
                  <td className={signal.direction === 'long' ? 'long' : 'short'}>{signal.direction}</td>
                  <td className="num">{formatNumber(signal.price)}</td>
                  <td>{signal.status}</td>
                  <td className="reason">{signal.reason ?? '-'}</td>
                  <td>
                    <button type="button" className="link-button" onClick={() => onSelectSignal(signal)}>
                      选中该信号
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  )
}
