import { useState } from 'react'
import type { FormEvent } from 'react'
import { api } from '../api'

export default function LoginPage() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    if (submitting) return
    setSubmitting(true)
    setError('')
    try {
      await api.login(username.trim(), password)
      // 登录成功 → 回工作台首页
      window.location.hash = '#/'
    } catch (err) {
      // 401 失败：展示后端 detail（如“用户名或密码错误”），网络等其他错误给兜底文案
      const msg = (err as Error).message
      setError(msg && !/^请求失败（HTTP/.test(msg) ? msg : '用户名或密码错误')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="login-wrap">
      <form className="login-card card" onSubmit={onSubmit}>
        <div className="brand" style={{ borderBottom: 'none', padding: 0, marginBottom: 16 }}>
          简历筛选工作台
        </div>
        <p className="page-sub" style={{ marginBottom: 18 }}>
          请登录后继续使用。
        </p>
        {error && <p className="error">{error}</p>}
        <label className="login-field">
          <span>用户名</span>
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
            autoComplete="username"
            disabled={submitting}
            required
          />
        </label>
        <label className="login-field">
          <span>密码</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            disabled={submitting}
            required
          />
        </label>
        <button className="btn primary login-submit" type="submit" disabled={submitting}>
          {submitting ? '登录中…' : '登 录'}
        </button>
      </form>
    </div>
  )
}
