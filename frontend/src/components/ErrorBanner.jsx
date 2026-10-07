import React from 'react'

/** 统一的错误提示条（不做复杂通知系统，只展示一条消息 + 可手动关闭）。 */
export default function ErrorBanner({ message, onDismiss, title = '请求失败' }) {
  if (!message) return null
  return (
    <div className="banner banner-error" role="alert">
      <div className="banner-body">
        <strong>{title}</strong>
        <span>{message}</span>
      </div>
      {onDismiss ? (
        <button type="button" className="banner-close" onClick={onDismiss} aria-label="关闭提示">
          ×
        </button>
      ) : null}
    </div>
  )
}
