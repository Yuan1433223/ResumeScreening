import { useEffect, useState } from 'react'
import { api } from './api'
import BatchesPage from './pages/BatchesPage'
import DashboardPage from './pages/DashboardPage'
import LoginPage from './pages/LoginPage'
import ReviewPage from './pages/ReviewPage'
import SchedulingPage from './pages/SchedulingPage'
import TracePage from './pages/TracePage'

type Route =
  | { page: 'login' }
  | { page: 'batches' }
  | { page: 'scheduling' }
  | { page: 'review'; documentId: string }
  | { page: 'dashboard' }
  | { page: 'traces' }

function parseHash(): Route {
  const h = window.location.hash.replace(/^#\/?/, '')
  if (h === 'login') return { page: 'login' }
  if (h.startsWith('review/')) {
    return { page: 'review', documentId: decodeURIComponent(h.slice('review/'.length)) }
  }
  if (h === 'scheduling') return { page: 'scheduling' }
  if (h === 'dashboard') return { page: 'dashboard' }
  if (h === 'traces') return { page: 'traces' }
  return { page: 'batches' }
}

export default function App() {
  const [route, setRoute] = useState<Route>(parseHash)

  useEffect(() => {
    const onHash = () => setRoute(parseHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  // 登录页为独立全屏页，不显示侧栏；其余页面布局不变
  if (route.page === 'login') return <LoginPage />

  const onLogout = () => {
    api.logout().finally(() => {
      window.location.hash = '#/login'
    })
  }

  const navItems: { key: string; href: string; label: string; active: boolean }[] = [
    { key: 'dashboard', href: '#/dashboard', label: '看板', active: route.page === 'dashboard' },
    { key: 'batches', href: '#/batches', label: '批次', active: route.page === 'batches' },
    { key: 'review', href: '#/batches', label: '复核', active: route.page === 'review' },
    { key: 'traces', href: '#/traces', label: '追踪', active: route.page === 'traces' },
    { key: 'scheduling', href: '#/scheduling', label: '排期', active: route.page === 'scheduling' },
  ]

  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">简历筛选工作台</div>
        <nav>
          {navItems.map((n) => (
            <a key={n.key} href={n.href} className={n.active ? 'active' : ''}>
              {n.label}
            </a>
          ))}
        </nav>
        <div className="foot">
          <button type="button" className="link logout-btn" onClick={onLogout}>
            退出登录
          </button>
          <div style={{ marginTop: 8 }}>规则评分 · 人工审批 · 幂等排期</div>
        </div>
      </aside>
      <main className="main">
        {route.page === 'dashboard' && <DashboardPage />}
        {route.page === 'batches' && <BatchesPage />}
        {route.page === 'scheduling' && <SchedulingPage />}
        {route.page === 'review' && <ReviewPage documentId={route.documentId} />}
        {route.page === 'traces' && <TracePage />}
      </main>
    </div>
  )
}
