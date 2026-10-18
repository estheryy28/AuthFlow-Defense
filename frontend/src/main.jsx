import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

const API = import.meta.env.VITE_API_BASE || 'http://localhost:8000';
const stages = [
  ['load_artifacts', 'Load evidence'],
  ['policy_and_application_analysis', 'Analyze policy and application'],
  ['policy_evidence_audit', 'Audit cross-section evidence'],
  ['test_planning', 'Plan bounded test'],
  ['skeptical_review', 'Model critique (advisory)'],
  ['live_execution', 'Run against live target'],
  ['verified', 'Read persisted state'],
];
const stageAliases = {
  policy_ambiguous: 'policy_and_application_analysis',
  no_policy_candidate: 'policy_and_application_analysis',
  policy_candidate_selection: 'policy_evidence_audit',
  test_plan_validation: 'test_planning',
  skeptical_review_complete: 'skeptical_review',
};

function App() {
  const [health, setHealth] = useState(null);
  const [mode, setMode] = useState('vulnerable');
  const [run, setRun] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function refreshHealth() {
    try {
      const response = await fetch(`${API}/api/health`);
      setHealth(await response.json());
    } catch {
      setHealth(null);
    }
  }

  useEffect(() => {
    refreshHealth();
    const timer = setInterval(refreshHealth, 5000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!run?.id || ['complete', 'error', 'needs_review'].includes(run.status)) return;
    let alive = true;
    const poll = async () => {
      try {
        const response = await fetch(`${API}/api/runs/${run.id}`);
        const data = await response.json();
        if (alive) setRun(data);
      } catch (e) {
        if (alive) setError(`Could not read run state: ${e.message}`);
      }
    };
    poll();
    const timer = setInterval(poll, 1800);
    return () => { alive = false; clearInterval(timer); };
  }, [run?.id, run?.status]);

  async function startRun() {
    setBusy(true);
    setRun(null);
    setError('');
    try {
      const response = await fetch(`${API}/api/runs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not start run');
      setRun(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const checks = health?.checks || {};
  const ready = Object.keys(checks).length === 3 && Object.values(checks).every((v) => v === 'ok');
  const execution = run?.result?.execution;
  const timelineStage = stageAliases[run?.stage] || run?.stage;
  const stageIndex = stages.findIndex(([key]) => key === timelineStage);
  const currentStage = stages.find(([key]) => key === timelineStage)?.[1] || run?.stage || 'Ready';
  const payment = execution?.state_snapshot?.payment;
  const ledgerEntry = execution?.state_snapshot?.ledger?.at(-1);
  const ambiguities = run?.result?.invariant?.ambiguities || [];

  return (
    <main className="shell">
      <header className="topbar">
        <a className="brand" href="#top"><span className="brand-mark">P</span> POLICY<span>TO</span>PROOF</a>
        <div className="top-status"><span className={`pulse ${ready ? 'good' : ''}`} />{ready ? 'LOCAL SYSTEM READY' : 'CHECKING LOCAL SERVICES'}</div>
      </header>

      <section className="hero" id="top">
        <div className="eyebrow">AI-ASSISTED APPLICATION SECURITY</div>
        <h1>Turn workflow intent<br /><em>into security proof.</em></h1>
        <p className="hero-copy">Specialist agents analyze policy and application structure, propose a bounded test, and let the live target state decide the result.</p>
        <div className="hero-tags"><span>Policy grounded</span><span>Local model</span><span>Live database oracle</span></div>
      </section>

      <section className="grid">
        <article className="panel setup-panel">
          <div className="panel-head"><div><div className="eyebrow">DEMO WORKFLOW</div><h2>Approval-to-settlement integrity</h2></div><span className="step-chip">LOCAL TARGET</span></div>
          <p className="panel-copy">Can the settlement state be reconciled to what the reviewer actually authorized? The test sequence is proposed from the supplied policy and application evidence.</p>
          <div className="flow">
            {['Policy', 'API + source', 'Agent test plan', 'Live state oracle'].map((x, i) => <React.Fragment key={x}><span className={i === 2 ? 'flow-hot' : ''}>{x}</span>{i < 3 && <b>→</b>}</React.Fragment>)}
          </div>
          <div className="control-row">
            <label htmlFor="mode">Target build</label>
            <select id="mode" value={mode} onChange={(e) => setMode(e.target.value)}>
              <option value="vulnerable">Vulnerable</option>
              <option value="fixed">Fixed</option>
            </select>
            <button className="primary" onClick={startRun} disabled={busy || !ready}>
              {busy ? 'Starting…' : 'Run live analysis'} <span>↗</span>
            </button>
          </div>
          {!ready && <p className="hint">Start Docker Compose and Ollama, then refresh this page. The UI will not substitute generated demo results.</p>}
          {error && <div className="error-box">{error}</div>}
        </article>

        <article className="panel services-panel">
          <div className="eyebrow">LOCAL DEPENDENCIES</div><h2>Service health</h2>
          <Service name="API and platform database" value={checks.database} />
          <Service name="Vulnerable app" value={checks.target} />
          <Service name={`Ollama · ${health?.model || 'local model'}`} value={checks.ollama} />
          <button className="link-button" onClick={refreshHealth}>Refresh status <span>↻</span></button>
        </article>
      </section>

      <section className="panel run-panel">
        <div className="panel-head"><div><div className="eyebrow">RUN EVIDENCE</div><h2>{run ? `Run ${run.id.slice(0, 8)}` : 'Live analysis trace'}</h2></div><span className={`status-pill ${run?.status === 'complete' ? 'complete' : ''}`}>{run?.status || 'WAITING'}</span></div>
        {run ? <>
          <div className="run-meta"><span>Current stage</span><strong>{currentStage}</strong><span>Target mode</span><strong>{run.result?.mode || run.mode || mode}</strong></div>
          {stageIndex >= 0 && <div className="stage-list" aria-label="Run stages">
            {stages.map(([key, label], index) => {
              const state = run.status === 'complete' || index < stageIndex ? 'done' : index === stageIndex ? 'active' : 'pending';
              return <div className={`stage-item ${state}`} key={key}>
                <span className="stage-marker">{state === 'done' ? '✓' : String(index + 1).padStart(2, '0')}</span>
                <span>{label}</span>
              </div>;
            })}
          </div>}
          {run.error && <div className="error-box">{run.error}</div>}
          {run.result?.failure && <div className="error-box">{run.result.failure.detail}</div>}
          {ambiguities.length > 0 && <div className="review-box"><strong>Policy needs human clarification</strong><span>{ambiguities.join(' · ')}</span></div>}
          {run.result?.review && <div className="review-box"><strong>Validation gate: {run.result.review.decision} · model critique: {run.result.review.advisory_decision || 'unavailable'}</strong><span>{run.result.review.reasons?.join(' · ')}</span></div>}
          {execution && <>
            <div className={`verdict ${execution.verdict === 'PROVEN' ? 'danger' : 'safe'}`}><div><span>DATABASE STATE ORACLE</span><strong>{execution.verdict}</strong></div><span>{execution.verdict === 'PROVEN' ? 'The ledger contains terms different from the approval snapshot.' : 'No mismatch between approved terms and a persisted ledger entry was observed.'}</span></div>
            <div className="state-compare">
              <div><span>APPROVED TERMS</span><strong>{payment?.approved_payee || 'No active approval'}</strong><small>{payment?.approved_amount ?? '—'} {payment?.approved_currency?.trim() || ''}</small></div>
              <div><span>PAYMENT ROW</span><strong>{payment?.payee || 'No payment row'}</strong><small>{payment ? `${payment.amount} ${payment.currency.trim()} · ${payment.status}` : 'No payment persisted'}</small></div>
              <div><span>LEDGER ENTRY</span><strong>{ledgerEntry?.payee || 'No ledger entry'}</strong><small>{ledgerEntry ? `${ledgerEntry.amount} ${ledgerEntry.currency.trim()}` : 'No settlement persisted'}</small></div>
            </div>
            <div className="request-trace"><div className="eyebrow">LIVE HTTP REQUESTS</div>
              {execution.requests.map((request, index) => <details className="request-row" key={`${request.method}-${request.path}-${index}`}>
                <summary><span>{String(index + 1).padStart(2, '0')}</span><strong>{request.method}</strong><code>{request.path}</code><b className={request.status < 300 ? 'http-ok' : 'http-denied'}>{request.status}</b></summary>
                <pre>{JSON.stringify(request.body, null, 2)}</pre>
              </details>)}
            </div>
          </>}
          <details className="evidence-json"><summary>Inspect raw run evidence</summary><pre>{JSON.stringify(run.result, null, 2)}</pre></details>
        </> : <div className="empty-state"><span className="empty-icon">⌁</span><p>Run the target to display agent outputs, actual HTTP requests, and the database-backed verdict.</p></div>}
      </section>

      <section className="agent-strip">
        <div><span>01</span><strong>Policy Analyst</strong><small>Extracts supported rules</small></div>
        <div><span>02</span><strong>Evidence Auditor</strong><small>Finds section-backed clauses</small></div>
        <div><span>03</span><strong>Application Mapper</strong><small>Maps routes and state</small></div>
        <div><span>04</span><strong>Test Planner</strong><small>Proposes a bounded sequence</small></div>
        <div><span>05</span><strong>Model Critic</strong><small>Advisory critique; verified gates decide</small></div>
      </section>

      <footer><span>POLICY-TO-PROOF · LOCAL LAB</span><span>AI proposes. Verified evidence and runtime state decide.</span></footer>
    </main>
  );
}

function Service({ name, value }) {
  return <div className="service-row"><span className={`service-dot ${value === 'ok' ? 'up' : ''}`} /><span>{name}</span><strong>{value || 'checking'}</strong></div>;
}

createRoot(document.getElementById('root')).render(<App />);
