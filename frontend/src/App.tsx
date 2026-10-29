import { lazy, Suspense, useEffect, useState } from 'react'
import { api } from './api'
import Icon from './components/Icon'
import HomePage from './pages/HomePage'
import BatchesPage from './pages/BatchesPage'
import ReviewPage from './pages/ReviewPage'
import SchedulingPage from './pages/SchedulingPage'
import LoginPage from './pages/LoginPage'
const DashboardPage = lazy(() => import('./pages/DashboardPage'))
const TracePage = lazy(() => import('./pages/TracePage'))

type Route = { page: 'home' | 'login' | 'batches' | 'scheduling' | 'dashboard' | 'traces' } | { page: 'review'; documentId: string; assessmentId: string }
function parseHash(): Route {
  const [path, query = ''] = window.location.hash.replace(/^#\/?/, '').split('?')
  if (path.startsWith('review/')) {
    try { return { page: 'review', documentId: decodeURIComponent(path.slice(7)), assessmentId: new URLSearchParams(query).get('assessment_id') || '' } } catch { return { page: 'home' } }
  }
  return { page: ['login', 'batches', 'scheduling', 'dashboard', 'traces'].includes(path) ? path as 'batches' : 'home' }
}
const titles = { home: '工作台概览', batches: '批次与复核', review: '证据复核', scheduling: '排期与重试', dashboard: '数据概览', traces: '处理记录', login: '登录' }
export default function App() {
  const [route, setRoute] = useState<Route>(parseHash)
  const [authEnabled, setAuthEnabled] = useState(false)
  useEffect(() => { const onHash = () => setRoute(parseHash()); window.addEventListener('hashchange', onHash); return () => window.removeEventListener('hashchange', onHash) }, [])
  useEffect(() => { api.getMe().then(me => setAuthEnabled(me.auth_enabled)).catch(() => {}) }, [route.page])
  if (route.page === 'login') return <LoginPage />
  const items = [
    { key: 'home', label: '工作台概览', icon: 'home' as const, href: '#/' },
    { key: 'batches', label: '批次与复核', icon: 'files' as const, href: '#/batches' },
    { key: 'scheduling', label: '排期与重试', icon: 'calendar' as const, href: '#/scheduling' },
  ]
  return <div className="layout">
    <a href="#main-content" className="skip-link" onClick={e => { e.preventDefault(); document.getElementById('main-content')?.focus() }}>跳到主要内容</a>
    <aside className="sidebar"><a className="brand" href="#/"><span className="brand-mark"><Icon name="check" size={21} /></span><span>ResumeScreening<small>岗位证据审核</small></span></a>
      <div className="nav-label">工作空间</div><nav aria-label="主要导航">{items.map(item => <a key={item.key} href={item.href} aria-current={route.page === item.key || (item.key === 'batches' && route.page === 'review') ? 'page' : undefined} className={route.page === item.key || (item.key === 'batches' && route.page === 'review') ? 'active' : ''}><Icon name={item.icon} />{item.label}</a>)}</nav>
      <div className="nav-label secondary-label">辅助信息</div><nav aria-label="辅助导航"><a href="#/traces" className={route.page === 'traces' ? 'active' : ''}><Icon name="activity" />处理记录</a><a href="#/dashboard" className={route.page === 'dashboard' ? 'active' : ''}><Icon name="chart" />数据概览</a></nav>
      <div className="sidebar-bottom"><div className="sidebar-note"><Icon name="shield" /><span>机器提供建议<br /><strong>人工保留批准权</strong></span></div>{authEnabled && <button className="btn logout-btn" onClick={() => api.logout().finally(() => { window.location.hash = '#/login' })}>退出登录</button>}<span className="local-label"><span className="dot ok" />本地工作台</span></div>
    </aside>
    <div className="workspace"><header className="topbar"><div>工作空间 <span>/</span> <strong>{titles[route.page]}</strong></div><span className="badge neutral">人工审核 · 单岗位</span></header>
      <main className={`main ${route.page === 'review' ? 'main-review' : ''}`} id="main-content" tabIndex={-1}><Suspense fallback={<div className="loading-state">正在加载页面…</div>}>
        {route.page === 'home' && <HomePage />}{route.page === 'batches' && <BatchesPage />}{route.page === 'scheduling' && <SchedulingPage />}
        {route.page === 'review' && <ReviewPage key={`${route.documentId}_${route.assessmentId}`} documentId={route.documentId} assessmentId={route.assessmentId} />}
        {route.page === 'dashboard' && <DashboardPage />}{route.page === 'traces' && <TracePage />}
      </Suspense></main>
    </div>
  </div>
}
