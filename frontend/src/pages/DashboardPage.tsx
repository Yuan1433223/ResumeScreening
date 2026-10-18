import { useEffect, useMemo, useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Funnel,
  FunnelChart,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api } from '../api'
import type { Dashboard } from '../types'

const DIM_LABELS: Record<string, string> = {
  steel_mic_experience: '钢结构经验',
  steel_full_flow: '钢结构全流程',
  overseas: '海外经验',
  management: '管理经验',
  quality_safety: '质量安全',
  english: '英语能力',
  project_scale: '项目规模',
  mobility: '可出差',
}

const ACCENT = '#1f5f54'
const FUNNEL_COLORS = ['#1f5f54', '#2f7a6c', '#439585', '#63ad9f', '#8fc6bc', '#bcd9d3']

interface KpiCard {
  key: string
  title: string
  value: string
  unit?: string
}

export default function DashboardPage() {
  const [data, setData] = useState<Dashboard | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api
      .getDashboard()
      .then(setData)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  const kpis: KpiCard[] = useMemo(() => {
    const k = data?.kpis
    return [
      { key: 'docs', title: '简历总数', value: String(k?.total_documents ?? 0) },
      { key: 'batches', title: '批次数', value: String(k?.total_batches ?? 0) },
      { key: 'recommended', title: '推荐', value: String(k?.recommended ?? 0) },
      { key: 'needs_review', title: '待复核', value: String(k?.needs_review ?? 0) },
      { key: 'hold', title: '暂缓', value: String(k?.hold ?? 0) },
      { key: 'approved', title: '已通过', value: String(k?.approved ?? 0) },
      {
        key: 'avg',
        title: '平均分',
        value: k?.avg_score == null ? '—' : String(Math.round(k.avg_score * 10) / 10),
      },
      {
        key: 'rate',
        title: '复核率',
        value: k ? `${Math.round(k.review_rate * 100)}%` : '—',
      },
    ]
  }, [data])

  const totalScoreData = useMemo(
    () => data?.score_distribution.total_score ?? [],
    [data]
  )

  const dimRows = useMemo(() => {
    const dims = data?.score_distribution.dimensions ?? {}
    return Object.entries(dims).map(([key, levels]) => {
      const total = levels.reduce((s, l) => s + l.count, 0)
      const weighted = levels.reduce((s, l) => s + l.level * l.count, 0)
      return {
        key,
        label: DIM_LABELS[key] ?? key,
        count: total,
        avg: total > 0 ? Math.round((weighted / total) * 10) / 10 : 0,
      }
    })
  }, [data])

  const funnelData = useMemo(
    () =>
      (data?.funnel ?? []).map((f, i) => ({
        name: f.label,
        value: f.count,
        fill: FUNNEL_COLORS[i % FUNNEL_COLORS.length],
      })),
    [data]
  )

  if (loading) return <p className="hint">加载看板…</p>

  return (
    <div>
      <h1 className="page-title">看板</h1>
      <p className="page-sub">
        汇总本地评测与合成数据的筛选全貌；指标随批次运行实时累积。
      </p>

      {error && <p className="error">看板加载失败：{error}</p>}

      {/* KPI 卡 */}
      <div className="kpi-grid">
        {kpis.map((c) => (
          <div key={c.key} className="kpi-card">
            <div className="kpi-title">{c.title}</div>
            <div className="kpi-value num">
              {c.value}
              {c.unit && <span className="kpi-unit">{c.unit}</span>}
            </div>
          </div>
        ))}
      </div>

      <div className="dash-cols">
        {/* 评分分布 */}
        <div className="card">
          <div className="card-title">总分分布</div>
          {totalScoreData.length === 0 ? (
            <div className="dash-empty">暂无数据</div>
          ) : (
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={totalScoreData} margin={{ top: 8, right: 8, left: -16, bottom: 0 }}>
                <CartesianGrid stroke="#eee" vertical={false} />
                <XAxis dataKey="bucket" tick={{ fontSize: 11, fill: '#6d6d66' }} />
                <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: '#6d6d66' }} />
                <Tooltip formatter={(v: unknown) => [`${v} 人`, '人数']} />
                <Bar dataKey="count" fill={ACCENT} radius={[3, 3, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          )}
        </div>

        {/* 漏斗 */}
        <div className="card">
          <div className="card-title">处理漏斗</div>
          {funnelData.length === 0 ? (
            <div className="dash-empty">暂无数据</div>
          ) : (
            <ResponsiveContainer width="100%" height={220}>
              <FunnelChart>
                <Tooltip formatter={(v: unknown) => [`${v} 人`]} />
                <Funnel dataKey="value" data={funnelData} isAnimationActive={false}>
                  <LabelList dataKey="name" position="right" fill="#6d6d66" fontSize={11} />
                </Funnel>
              </FunnelChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>

      {/* 八维分布 */}
      <div className="card">
        <div className="card-title">八维覆盖人数</div>
        {dimRows.length === 0 ? (
          <div className="dash-empty">暂无数据</div>
        ) : (
          <ResponsiveContainer width="100%" height={Math.max(180, dimRows.length * 34)}>
            <BarChart
              data={dimRows}
              layout="vertical"
              margin={{ top: 0, right: 24, left: 8, bottom: 0 }}
            >
              <CartesianGrid stroke="#eee" horizontal={false} />
              <XAxis type="number" allowDecimals={false} tick={{ fontSize: 11, fill: '#6d6d66' }} />
              <YAxis
                type="category"
                dataKey="label"
                width={90}
                tick={{ fontSize: 12, fill: '#33332e' }}
              />
              <Tooltip
                formatter={(v: unknown, _n: unknown, item: unknown) => {
                  const row = item as { payload?: { avg?: number } }
                  return [`${v} 人（均分 ${row?.payload?.avg ?? '—'}）`, '覆盖']
                }}
              />
              <Bar dataKey="count" fill={ACCENT} radius={[0, 3, 3, 0]} />
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  )
}
