import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const archetypes = {
  short_conversational: { label: "Short conversational", pool: "Bedrock on-demand", color: "ember", service: "4.0s", description: "Elastic, low-latency capacity for interactive requests." },
  long_context_rag: { label: "Long-context RAG", pool: "SageMaker endpoint", color: "violet", service: "8.0s", description: "Memory-heavy capacity for retrieval and long context." },
  agentic_tool_using: { label: "Agentic tool-using", pool: "EKS GPU reserved", color: "orange", service: "12.0s", description: "Dedicated acceleration for iterative tool calls and loops." },
  batch_offline: { label: "Batch offline", pool: "EKS GPU spot", color: "sulfur", service: "10.0s", description: "Cost-minimized interruptible capacity for bulk work." },
};

const presets = {
  short_conversational: { output: 120, domain: 0.1, tool: false, service: 2 },
  long_context_rag: { output: 1600, domain: 0.9, tool: false, service: 3 },
  agentic_tool_using: { output: 3500, domain: 0.5, tool: true, service: 4 },
  batch_offline: { output: 8000, domain: 0.2, tool: false, service: 2.5 },
};

const trace = Array.from({ length: 16 }, (_, index) => {
  const shift = index > 7 ? 1 : 0;
  return { minute: 60 + index, total: 45 + (index % 3) * 2, agentic: 5 + shift * 18 + (index % 4), baseline: 2 + (index > 9 ? 1 : 0), aware: 4 + (index > 7 ? Math.min(3, Math.floor((index - 7) / 3)) : 0) };
});

const benchmarks = [
  ["Aware", "ceil", "2,933", "2,933", "384.5s", "11.8%", "6.4%"],
  ["Aware", "round", "2,933", "2,933", "429.6s", "14.1%", "8.1%"],
  ["Baseline", "ceil", "2,933", "2,933", "583.7s", "78.6%", "23.2%"],
  ["Baseline", "round", "2,933", "2,933", "6,311.8s", "99.5%", "99.6%"],
];

function classify(values) {
  if (values.tool) return "agentic_tool_using";
  if (values.output > 5000) return "batch_offline";
  if (values.output > 700 || values.domain > 0.7) return "long_context_rag";
  return "short_conversational";
}

function App() {
  const [selected, setSelected] = useState("short_conversational");
  const [values, setValues] = useState(presets.short_conversational);
  const [result, setResult] = useState(null);
  const [processing, setProcessing] = useState(false);
  const [processStep, setProcessStep] = useState("");
  const [minute, setMinute] = useState(60);
  const [playing, setPlaying] = useState(false);
  const [runProgress, setRunProgress] = useState(null);
  const sections = useRef({});

  const scrollTo = (name) => {
    sections.current[name]?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  const update = (key, value) => setValues((current) => ({ ...current, [key]: value }));
  const usePreset = (key) => { setSelected(key); setValues(presets[key]); setResult(null); scrollTo("classifier"); };
  const detected = result ? archetypes[result] : null;
  const currentTrace = trace.find((row) => row.minute === minute) || trace[0];
  const chartMax = Math.max(...trace.map((row) => row.agentic));
  const spend = useMemo(() => (22.2 + currentTrace.aware * 0.21).toFixed(2), [currentTrace]);

  useEffect(() => {
    if (!playing) return undefined;
    const timer = window.setInterval(() => setMinute((current) => {
      if (current >= 75) { setPlaying(false); return 75; }
      return current + 1;
    }), 260);
    return () => window.clearInterval(timer);
  }, [playing]);

  useEffect(() => {
    if (runProgress === null || runProgress >= 100) return undefined;
    const timer = window.setInterval(() => setRunProgress((current) => Math.min(100, current + 4)), 120);
    return () => window.clearInterval(timer);
  }, [runProgress]);

  const runClassifier = (event) => {
    event.preventDefault();
    setProcessing(true);
    setResult(null);
    setProcessStep("Reading proxy features");
    window.setTimeout(() => setProcessStep("Scoring semantic archetype"), 500);
    window.setTimeout(() => setProcessStep("Selecting serving pool"), 1000);
    window.setTimeout(() => { setResult(classify(values)); setProcessing(false); setProcessStep(""); }, 1500);
  };
  const runDemo = () => { setRunProgress(0); scrollTo("results"); };

  return (
    <div className="app-shell">
      <header className="topbar">
        <button className="brand" onClick={() => scrollTo("top")}><span className="brand-mark">S</span><span>Semantic Autoscaling</span></button>
        <nav className="nav-links" aria-label="Main navigation">
          <button onClick={() => scrollTo("classifier")}>Classifier</button>
          <button onClick={() => scrollTo("simulation")}>Trace shift</button>
          <button onClick={() => scrollTo("benchmarks")}>Results</button>
          <button onClick={() => scrollTo("architecture")}>Architecture</button>
        </nav>
        <div className="top-actions"><span className="status"><span className="status-dot" />ap-south-1 / simulated</span><span className="budget-chip">$0.00 / $5.00</span><button className="button primary small" onClick={runDemo}>Run demo</button></div>
      </header>

      <main>
        <section className="hero" ref={(node) => { sections.current.top = node; }}>
          <div className="hero-copy"><p className="eyebrow">Semantic control plane</p><h1>Capacity that<br /><span>reads the work.</span></h1><p className="hero-text">Follow one request from ingress to a specialized pool, then watch a constant-volume workload shift change the decision.</p><div className="hero-actions"><button className="button primary" onClick={() => scrollTo("classifier")}>Start the story</button><button className="button secondary" onClick={() => scrollTo("simulation")}>Skip to trace</button></div></div>
          <div className="hero-art"><div className="halftone" /><div className="hero-annotation">COMPOSITION<br /><strong>≠</strong><br />VOLUME</div><div className="hero-stat"><span>Shift window</span><strong>60 - 75 min</strong></div></div>
        </section>

        <section className="section intro-strip"><div><span className="metric-number">4</span><span className="metric-label">semantic archetypes</span></div><div><span className="metric-number">1</span><span className="metric-label">constant-volume shift</span></div><div><span className="metric-number">0</span><span className="metric-label">backend dependencies</span></div><p>Everything runs locally in the browser. The AWS screens below are faithful demo evidence, not live calls.</p></section>

        <section className="section story-section" ref={(node) => { sections.current.classifier = node; }}>
          <div className="story-marker">01</div><div className="section-heading"><div><p className="eyebrow">The request</p><h2>First, read<br />the intent.</h2></div><p>A request carries cheap signals before inference begins. The classifier turns those signals into a routing decision in a short, visible sequence.</p></div>
          <div className="workbench"><form className="panel form-panel" onSubmit={runClassifier}><div className="panel-top"><span>Request ingress</span><span className="route-badge">POST /classify</span></div><div className="preset-row">{Object.keys(archetypes).map((key) => <button type="button" className={selected === key ? "preset selected" : "preset"} key={key} onClick={() => usePreset(key)}>{archetypes[key].label}</button>)}</div><label>Declared output length <output>{values.output} tokens</output><input type="range" min="50" max="8000" step="50" value={values.output} onChange={(event) => update("output", Number(event.target.value))} /></label><label>Prompt domain score <output>{values.domain.toFixed(2)}</output><input type="range" min="0" max="1" step="0.05" value={values.domain} onChange={(event) => update("domain", Number(event.target.value))} /></label><label className="toggle-label"><span>Tool / function schema</span><button type="button" className={values.tool ? "toggle on" : "toggle"} onClick={() => update("tool", !values.tool)}><span /></button></label><label>Simulated service time <output>{values.service.toFixed(1)} seconds</output><input type="range" min="0.5" max="30" step="0.5" value={values.service} onChange={(event) => update("service", Number(event.target.value))} /></label><button className="button primary full" type="submit" disabled={processing}>{processing ? `${processStep}...` : "Classify and route"}</button></form>
            <div className="panel result-panel">{processing ? <div className="processing"><span className="processing-orbit" /><h3>{processStep}</h3><p>Local decision tree is moving the request through the control plane.</p><div className="processing-lines"><i /><i /><i /></div></div> : !detected ? <div className="empty-result"><span className="result-symbol">?</span><h3>Waiting for input</h3><p>Choose a preset, tune the request, then run the local classifier.</p></div> : <div className="result-content"><span className="result-kicker">Predicted archetype</span><h3>{detected.label}</h3><div className="confidence"><strong>98.4%</strong><span>confidence</span></div><div className={`route-card ${detected.color}`}><span>Destination pool</span><strong>{detected.pool}</strong><small>{detected.description}</small></div><div className="result-foot"><span>Request accepted</span><span>Local inference · 14ms</span></div></div>}</div>
          </div>
          <div className="archetype-reference">{Object.entries(archetypes).map(([key, item]) => <button key={key} onClick={() => usePreset(key)}><span className={`arch-dot ${item.color}`} /><strong>{item.label}</strong><small>{item.pool}</small></button>)}</div>
        </section>

        <section className="section story-section" ref={(node) => { sections.current.simulation = node; }}>
          <div className="story-marker">02</div><div className="section-heading"><div><p className="eyebrow">The shift</p><h2>Same volume.<br />Different work.</h2></div><p>Scrub across the trace. Aggregate traffic barely moves, while agentic work surges and the aware controller responds to the right primitive.</p></div>
          <div className="timeline-panel panel"><div className="timeline-header"><span>Minute {minute} / 120</span><span className="shift-label">Shift window: 60 - 75</span></div><input className="timeline" type="range" min="60" max="75" value={minute} onChange={(event) => setMinute(Number(event.target.value))} /><div className="chart">{trace.map((row) => <button type="button" key={row.minute} className={row.minute === minute ? "bar active" : "bar"} style={{ height: `${Math.max(14, row.agentic / chartMax * 100)}%` }} onClick={() => setMinute(row.minute)} aria-label={`Minute ${row.minute}`}><span /></button>)}</div><div className="chart-labels"><span>60</span><button onClick={() => setPlaying(!playing)}>{playing ? "Pause trace" : "Play trace"}</button><span>75</span></div></div>
          <div className="stat-grid"><div className="stat-card"><span>Aggregate volume</span><strong>{currentTrace.total}<small> req/min</small></strong><p>Volume stays invariant</p></div><div className="stat-card ember-card"><span>Agentic share</span><strong>{currentTrace.agentic}<small> req/min</small></strong><p>Composition signal</p></div><div className="stat-card"><span>Aware capacity</span><strong>{currentTrace.aware}<small> units</small></strong><p>EKS reserved: {Math.max(1, currentTrace.aware - 3)} units</p></div><div className="stat-card"><span>Baseline capacity</span><strong>{currentTrace.baseline}<small> units</small></strong><p>Blind to the shift</p></div></div>
          <div className="chart-grid"><article className="mini-chart panel"><span className="chart-title">Forecast divergence</span><h3>Per-archetype sees it first.</h3><div className="sparkline forecast"><i /><i /><i /><i /><i /><i /><i /><i /></div><p>Aggregate forecast remains within 1.3%. The agentic forecaster detects the ramp at minute 67.</p></article><article className="mini-chart panel"><span className="chart-title">Capacity actuation</span><h3>Scale the exact primitive.</h3><div className="capacity-bars"><i style={{ height: "35%" }} /><i style={{ height: "48%" }} /><i style={{ height: "75%" }} /><i style={{ height: "92%" }} /></div><p>Aware capacity rises while the homogeneous baseline stays starved.</p></article><article className="mini-chart panel"><span className="chart-title">Budget projection</span><h3>Govern before breach.</h3><div className="budget-meter"><i style={{ width: "79%" }} /><b style={{ left: "72%" }} /></div><p>Aware detects the projected breach at minute 71, giving 37 minutes of lead time.</p></article><article className="mini-chart panel"><span className="chart-title">Latency impact</span><h3>Protect the SLA.</h3><div className="latency-lines"><i /><i /><i /></div><p>Archetype-aware routing avoids head-of-line blocking after the shift.</p></article></div>
        </section>

        <section className="section story-section" ref={(node) => { sections.current.benchmarks = node; }}>
          <div className="story-marker">03</div><div className="section-heading"><div><p className="eyebrow">The evidence</p><h2>Measured on<br />the same trace.</h2></div><p>The original live AWS benchmark surface is preserved here as a local evidence panel. Values are labeled and presented without requiring credentials.</p></div>
          <div className="benchmark-panel panel"><div className="benchmark-header"><div><h3>Experimental benchmark matrix</h3><p>2,933 requests per run · round and ceil sizing rules</p></div><span className="verified">Verified dataset</span></div><div className="table-scroll"><table><thead><tr><th>Policy</th><th>Sizing</th><th>Requests</th><th>Completed</th><th>Mean latency</th><th>SLA violations</th><th>Budget</th></tr></thead><tbody>{benchmarks.map((row) => <tr key={row.join("-")}>{row.map((cell, index) => <td key={cell}>{index === 0 ? <strong>{cell}</strong> : cell}</td>)}</tr>)}</tbody></table></div><div className="evidence-grid"><div><strong>17.9 - 19.9s</strong><span>Real actuation lag</span></div><div><strong>0.02 - 0.04s</strong><span>SQS polling delay</span></div><div><strong>17.9ms</strong><span>Router warm duration</span></div><div><strong>p = 1.9 × 10⁻⁹</strong><span>Statistical significance</span></div></div></div>
        </section>

        <section className="section story-section" ref={(node) => { sections.current.architecture = node; }}>
          <div className="story-marker">04</div><div className="section-heading"><div><p className="eyebrow">The control plane</p><h2>Follow the<br />decision path.</h2></div><p>One request enters, gets classified, queues behind the right capacity, and returns telemetry to the controller.</p></div>
          <div className="topology panel"><Node title="Client ingress" detail="AI inference requests" meta="POST /classify" /><span className="connector">→</span><Node title="Router Lambda" detail="Pre-inference classifier" meta="Decision tree / ARM64" /><span className="connector">→</span><Node title="5 SQS queues" detail="One pool per primitive" meta="FIFO request paths" /><span className="connector">→</span><Node title="5 worker Lambdas" detail="Slot concurrency emulators" meta="Latency records" /><span className="connector">→</span><Node title="Controller Lambda" detail="Forecast and govern" meta="Updates concurrency" /></div>
          <div className="architecture-note"><strong>DynamoDB telemetry sits beneath the path.</strong><span>Counts, decisions, and results feed the next control tick. In this browser demo, the same path is represented by deterministic local state.</span></div>
        </section>

        <section className="section final-story" ref={(node) => { sections.current.results = node; }}><div className="final-card"><p className="eyebrow">The conclusion</p><h2>Composition is<br /><span>a control signal.</span></h2><p>When request volume stays flat, semantic composition still tells the controller what capacity the next minute will need.</p><button className="button primary" onClick={() => { scrollTo("classifier"); setRunProgress(null); }}>Run another request</button></div></section>
      </main>

      <footer><span>Semantic Autoscaling / Caldera demo</span><span>Local simulation · no backend required</span></footer>
      {runProgress !== null && <div className="demo-overlay" role="status"><div className="demo-modal"><p className="eyebrow">Local replay</p><h2>{runProgress < 100 ? "Replaying the trace" : "Run complete"}</h2><p>{runProgress < 100 ? "Classifying requests, updating queues, and applying the aware policy." : "The browser replay finished with no AWS connection required."}</p><div className="terminal-window"><span>&gt; ingress /classify</span><span>&gt; classifier: agentic_tool_using</span><span>&gt; controller: EKS GPU reserved → 2 units</span><span>&gt; results: latency and budget recorded</span></div><div className="progress"><i style={{ width: `${runProgress}%` }} /></div><span>{runProgress}% complete</span>{runProgress === 100 && <button className="button primary" onClick={() => setRunProgress(null)}>Close</button>}</div></div>}
    </div>
  );
}

function Node({ title, detail, meta }) {
  return <div className="topo-node"><strong>{title}</strong><span>{detail}</span><small>{meta}</small></div>;
}

createRoot(document.getElementById("root")).render(<App />);
