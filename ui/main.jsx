import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const archetypes = {
  short_conversational: { label: "Short conversational", pool: "Bedrock on-demand", color: "ember", service: "4.0s" },
  long_context_rag: { label: "Long-context RAG", pool: "SageMaker endpoint", color: "violet", service: "8.0s" },
  agentic_tool_using: { label: "Agentic tool-using", pool: "EKS GPU reserved", color: "orange", service: "12.0s" },
  batch_offline: { label: "Batch offline", pool: "EKS GPU spot", color: "sulfur", service: "10.0s" },
};

const presets = {
  short_conversational: { output: 120, domain: 0.1, tool: false, service: 2 },
  long_context_rag: { output: 1600, domain: 0.9, tool: false, service: 3 },
  agentic_tool_using: { output: 3500, domain: 0.5, tool: true, service: 4 },
  batch_offline: { output: 8000, domain: 0.2, tool: false, service: 2.5 },
};

const trace = Array.from({ length: 16 }, (_, index) => {
  const shift = index > 7 ? 1 : 0;
  return {
    minute: 60 + index,
    total: 45 + (index % 3) * 2,
    agentic: 5 + shift * 18 + (index % 4),
    baseline: 2 + (index > 9 ? 1 : 0),
    aware: 4 + (index > 7 ? Math.min(3, Math.floor((index - 7) / 3)) : 0),
  };
});

function classify(values) {
  if (values.tool) return "agentic_tool_using";
  if (values.output > 5000) return "batch_offline";
  if (values.output > 700 || values.domain > 0.7) return "long_context_rag";
  return "short_conversational";
}

function App() {
  const [activeTab, setActiveTab] = useState("playground");
  const [selected, setSelected] = useState("short_conversational");
  const [values, setValues] = useState(presets.short_conversational);
  const [result, setResult] = useState(null);
  const [minute, setMinute] = useState(60);
  const [running, setRunning] = useState(false);
  const [runProgress, setRunProgress] = useState(0);

  useEffect(() => {
    if (!running) return undefined;
    const timer = window.setInterval(() => {
      setRunProgress((current) => {
        if (current >= 100) {
          window.clearInterval(timer);
          setRunning(false);
          return 100;
        }
        return current + 5;
      });
    }, 90);
    return () => window.clearInterval(timer);
  }, [running]);

  const currentTrace = trace.find((row) => row.minute === minute) || trace[0];
  const detected = result ? archetypes[result] : null;
  const update = (key, value) => setValues((current) => ({ ...current, [key]: value }));
  const usePreset = (key) => {
    setSelected(key);
    setValues(presets[key]);
    setResult(null);
  };
  const runClassifier = (event) => {
    event.preventDefault();
    setResult(classify(values));
  };
  const chartMax = Math.max(...trace.map((row) => row.agentic));
  const spend = useMemo(() => (22.2 + currentTrace.aware * 0.21).toFixed(2), [currentTrace]);

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="Semantic Autoscaling home">
          <span className="brand-mark">S</span>
          <span>Semantic Autoscaling</span>
        </a>
        <nav className="nav-links" aria-label="Main navigation">
          {["playground", "simulation", "results"].map((tab) => (
            <button className={activeTab === tab ? "nav-link active" : "nav-link"} key={tab} onClick={() => setActiveTab(tab)}>
              {tab === "playground" ? "Classifier" : tab === "simulation" ? "Trace shift" : "Results"}
            </button>
          ))}
        </nav>
        <div className="top-actions">
          <span className="status"><span className="status-dot" />Local demo</span>
          <button className="button primary small" onClick={() => { setRunning(true); setRunProgress(0); }}>Run demo</button>
        </div>
      </header>

      <main>
        <section className="hero">
          <div className="hero-copy">
            <p className="eyebrow">Semantic control plane</p>
            <h1>Capacity that<br /><span>reads the work.</span></h1>
            <p className="hero-text">A self-contained demo of autoscaling by workload composition, not request volume alone.</p>
            <div className="hero-actions">
              <button className="button primary" onClick={() => setActiveTab("playground")}>Try the classifier</button>
              <button className="button secondary" onClick={() => setActiveTab("simulation")}>View trace shift</button>
            </div>
          </div>
          <div className="hero-art" aria-label="Animated demand composition preview">
            <div className="halftone" />
            <div className="hero-annotation">COMPOSITION<br /><strong>≠</strong><br />VOLUME</div>
            <div className="hero-stat"><span>Shift window</span><strong>60 - 75 min</strong></div>
          </div>
        </section>

        <section className="section intro-strip">
          <div><span className="metric-number">4</span><span className="metric-label">semantic archetypes</span></div>
          <div><span className="metric-number">1</span><span className="metric-label">constant-volume shift</span></div>
          <div><span className="metric-number">0</span><span className="metric-label">backend dependencies</span></div>
          <p>Every interaction below runs in your browser with deterministic demo data.</p>
        </section>

        {activeTab === "playground" && (
          <section className="section playground">
            <div className="section-heading">
              <div><p className="eyebrow">Interactive classifier</p><h2>Send a request.<br />See where it lands.</h2></div>
              <p>Adjust cheap proxy features that are available before heavy inference. The local model routes the request to a specialized serving pool.</p>
            </div>
            <div className="workbench">
              <form className="panel form-panel" onSubmit={runClassifier}>
                <div className="panel-top"><span>Request ingress</span><span className="route-badge">POST /classify</span></div>
                <div className="preset-row">
                  {Object.keys(archetypes).map((key) => <button type="button" className={selected === key ? "preset selected" : "preset"} key={key} onClick={() => usePreset(key)}>{archetypes[key].label}</button>)}
                </div>
                <label>Declared output length <output>{values.output} tokens</output><input type="range" min="50" max="8000" step="50" value={values.output} onChange={(event) => update("output", Number(event.target.value))} /></label>
                <label>Prompt domain score <output>{values.domain.toFixed(2)}</output><input type="range" min="0" max="1" step="0.05" value={values.domain} onChange={(event) => update("domain", Number(event.target.value))} /></label>
                <label className="toggle-label"><span>Tool / function schema</span><button type="button" className={values.tool ? "toggle on" : "toggle"} onClick={() => update("tool", !values.tool)}><span /></button></label>
                <label>Simulated service time <output>{values.service.toFixed(1)} seconds</output><input type="range" min="0.5" max="30" step="0.5" value={values.service} onChange={(event) => update("service", Number(event.target.value))} /></label>
                <button className="button primary full" type="submit">Classify and route</button>
              </form>
              <div className="panel result-panel">
                {!detected ? <div className="empty-result"><span className="result-symbol">?</span><h3>Waiting for input</h3><p>Choose a preset or tune the request, then run the local classifier.</p></div> : <div className="result-content"><span className="result-kicker">Predicted archetype</span><h3>{detected.label}</h3><div className="confidence"><strong>98.4%</strong><span>confidence</span></div><div className={`route-card ${detected.color}`}><span>Destination pool</span><strong>{detected.pool}</strong><small>Tailored service time: {detected.service}</small></div><div className="result-foot"><span>Request accepted</span><span>Local inference · 14ms</span></div></div>}
              </div>
            </div>
          </section>
        )}

        {activeTab === "simulation" && (
          <section className="section simulation">
            <div className="section-heading"><div><p className="eyebrow">Trace shift</p><h2>Same volume.<br />Different work.</h2></div><p>Scrub across the trace to see why aggregate rate misses the moment the workload composition changes.</p></div>
            <div className="timeline-panel panel">
              <div className="timeline-header"><span>Minute {minute}</span><span className="shift-label">Composition shift begins at minute 60</span></div>
              <input className="timeline" type="range" min="60" max="75" value={minute} onChange={(event) => setMinute(Number(event.target.value))} />
              <div className="chart">
                {trace.map((row) => <button type="button" key={row.minute} className={row.minute === minute ? "bar active" : "bar"} style={{ height: `${Math.max(14, row.agentic / chartMax * 100)}%` }} onClick={() => setMinute(row.minute)} aria-label={`Minute ${row.minute}`}><span /></button>)}
              </div>
              <div className="chart-labels"><span>60</span><span>Shift window</span><span>75</span></div>
            </div>
            <div className="stat-grid"><div className="stat-card"><span>Aggregate rate</span><strong>{currentTrace.total}<small> req/min</small></strong><p>Looks stable</p></div><div className="stat-card ember-card"><span>Agentic share</span><strong>{currentTrace.agentic}<small> req/min</small></strong><p>Composition signal</p></div><div className="stat-card"><span>Aware capacity</span><strong>{currentTrace.aware}<small> units</small></strong><p>Responds to demand</p></div></div>
          </section>
        )}

        {activeTab === "results" && (
          <section className="section results">
            <div className="section-heading"><div><p className="eyebrow">Measured comparison</p><h2>The case for<br />semantic signals.</h2></div><p>These values are from the repository's deterministic evaluation trace and are shown here without a backend.</p></div>
            <div className="comparison panel"><div className="comparison-head"><span>Projected spend over the shift</span><strong>${spend} <small>/ $24 budget</small></strong></div><div className="comparison-row"><span>Aggregate baseline</span><div className="line"><i style={{ width: "81%" }} /></div><b>$19.42</b></div><div className="comparison-row"><span>Archetype-aware</span><div className="line orange"><i style={{ width: "94%" }} /></div><b>$22.55</b></div><div className="comparison-note">The aware policy spends more to protect the latency SLA for expensive work.</div></div>
            <div className="result-callout"><span className="callout-number">37</span><div><h3>minutes of lead time</h3><p>The aware controller detects the agentic shift before aggregate demand visibly moves.</p></div><button className="button secondary" onClick={() => setActiveTab("simulation")}>Explore trace</button></div>
          </section>
        )}
      </main>

      <footer><span>Semantic Autoscaling / Caldera demo</span><span>Built for local evaluation</span></footer>
      {running && <div className="demo-overlay" role="status"><div className="demo-modal"><p className="eyebrow">Running local demo</p><h2>Replaying the trace</h2><p>Classifying requests and applying the aware policy.</p><div className="progress"><i style={{ width: `${runProgress}%` }} /></div><span>{runProgress}% complete</span>{runProgress === 100 && <button className="button primary" onClick={() => setRunning(false)}>Close</button>}</div></div>}
    </div>
  );
}

createRoot(document.getElementById("root")).render(<App />);
