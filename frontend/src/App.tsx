import { useEffect, useState } from 'react'
import BatchesPage from './pages/BatchesPage'
import ReviewPage from './pages/ReviewPage'
import SchedulingPage from './pages/SchedulingPage'

type Route =
  | { page: 'batches' }
  | { page: 'scheduling' }
  | { page: 'review'; documentId: string }

function parseHash(): Route {
  const h = window.location.hash.replace(/^#\/?/, '')
  if (h.startsWith('review/')) {
    return { page: 'review', documentId: decodeURIComponent(h.slice('review/'.length)) }
  }
  if (h === 'scheduling') return { page: 'scheduling' }
  return { page: 'batches' }
}

export default function App() {
  const [route, setRoute] = useState<Route>(parseHash)

  useEffect(() => {
    const onHash = () => setRoute(parseHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const navItems: { key: string; href: string; label: string; active: boolean }[] = [
    { key: 'batches', href: '#/batches', label: '批次', active: route.page === 'batches' },
    { key: 'review', href: '#/batches', label: '复核', active: route.page === 'review' },
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
        <div className="foot">规则评分 · 人工审批 · 幂等排期</div>
      </aside>
      <main className="main">
        {route.page === 'batches' && <BatchesPage />}
        {route.page === 'scheduling' && <SchedulingPage />}
        {route.page === 'review' && <ReviewPage documentId={route.documentId} />}
      </main>
    </div>
  )
}
