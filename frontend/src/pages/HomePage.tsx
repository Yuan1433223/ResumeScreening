import Icon from '../components/Icon'

export default function HomePage() {
  return <div className="home-page">
    <div className="eyebrow">RESUME REVIEW · 人工审核工作台</div>
    <section className="home-hero">
      <div><span className="badge neutral">海外项目负责人 · 钢结构 / MiC</span>
        <h1>让每个判断有据可查，<br />让关键决定始终由人掌握。</h1>
        <p>帮助审核者快速核对岗位证据，<br className="desktop-break" />并在信息不足或流程失败时保留人工控制。</p>
        <div className="hero-actions"><a className="btn primary" href="#/batches">进入批次与复核 <Icon name="arrow" /></a><a className="btn" href="#workflow">了解审核流程</a></div>
      </div>
      <div className="review-principle"><div className="principle-icon"><Icon name="shield" size={26} /></div><span className="eyebrow">审核原则</span><h2>建议有边界，<br />决定有确认。</h2><p>模型提取事实，程序执行规则。<br />缺少证据的信息标记为待确认，<br />所有候选人都等待人工决定。</p><div className="principle-foot"><span className="dot ok" />证据核对 → 人工批准 → 排期</div></div>
    </section>
    <section className="outcomes" aria-label="三个核心成果">
      <a href="#/batches" className="outcome-card"><span className="step-number">01</span><Icon name="search" size={23} /><h2>从判断回到原文</h2><p>事实与原文并列。点击评分依据，查看同一次评估保存的引用位置。</p><span className="card-action">核对岗位证据 <Icon name="arrow" size={16} /></span></a>
      <a href="#/batches" className="outcome-card"><span className="step-number">02</span><Icon name="shield" size={23} /><h2>中断后继续审批</h2><p>审批决定先保存。流程恢复失败时，保留决定并允许继续恢复。</p><span className="card-action">人工控制处理过程 <Icon name="arrow" size={16} /></span></a>
      <a href="#/scheduling" className="outcome-card"><span className="step-number">03</span><Icon name="retry" size={23} /><h2>重试只补失败动作</h2><p>沿用已有预约与会议。通知失败后重试，成功动作保持原有记录。</p><span className="card-action">查看排期与重试 <Icon name="arrow" size={16} /></span></a>
    </section>
    <section id="workflow" className="workflow-card"><div className="section-heading"><div><span className="eyebrow">审核流程</span><h2>从简历到人工批准，一条清晰的路径</h2></div><span className="badge neutral">信息不足，保留待确认</span></div><div className="workflow-steps">{['解析原文', '提取事实与引用', '校验与规则评分', '人工核对与决定', '批准后模拟排期'].map((s, i) => <div key={s}><span>{String(i + 1).padStart(2, '0')}</span><strong>{s}</strong>{i < 4 && <Icon name="arrow" size={16} />}</div>)}</div></section>
    <div className="scope-note"><Icon name="files" /><span>企业场景工程案例 · 单岗位、本地工作台；会议和通知为模拟服务。</span></div>
  </div>
}
