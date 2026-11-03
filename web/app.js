/* ==========================================================================
   SEMANTIC AUTOSCALING DASHBOARD — CLIENT LOGIC & CHARTS
   ========================================================================== */

document.addEventListener('DOMContentLoaded', () => {
  // Global State
  const state = {
    currentMinute: 60,
    isPlaying: false,
    playInterval: null,
    traceData: null,
    charts: {},
    presets: {
      short_conversational: { outputLen: 120, domainScore: 0.10, hasTool: false, service: 2.0 },
      long_context_rag: { outputLen: 1600, domainScore: 0.90, hasTool: false, service: 3.0 },
      agentic_tool_using: { outputLen: 3500, domainScore: 0.50, hasTool: true, service: 4.0 },
      batch_offline: { outputLen: 8000, domainScore: 0.20, hasTool: false, service: 2.5 },
    },
    archetypeMeta: {
      short_conversational: {
        name: 'Short Conversational',
        pool: 'bedrock_on_demand',
        primitive: 'AWS Bedrock On-Demand',
        baseService: '4.0s',
        desc: 'Tailored for interactive, low-latency conversational queries with high elasticity and zero idle cost.',
      },
      long_context_rag: {
        name: 'Long-Context RAG',
        pool: 'sagemaker_endpoint',
        primitive: 'SageMaker Real-Time Endpoint',
        baseService: '8.0s',
        desc: 'Optimized for memory-heavy document embeddings and retrieval context processing.',
      },
      agentic_tool_using: {
        name: 'Agentic Tool-Using',
        pool: 'eks_gpu_reserved',
        primitive: 'EKS GPU Reserved Pool',
        baseService: '12.0s',
        desc: 'Dedicated GPU acceleration for iterative multi-step tool calls, code execution, and autonomous loops.',
      },
      batch_offline: {
        name: 'Batch Offline',
        pool: 'eks_gpu_spot',
        primitive: 'EKS GPU Spot Pool',
        baseService: '10.0s',
        desc: 'Cost-minimized interruptible spot capacity for latency-insensitive bulk data processing.',
      },
    }
  };

  // Elements
  const tabs = document.querySelectorAll('.nav-tab');
  const panes = document.querySelectorAll('.tab-pane');
  const sliderOutputLen = document.getElementById('input-output-len');
  const valOutputLen = document.getElementById('val-output-len');
  const sliderDomainScore = document.getElementById('input-domain-score');
  const valDomainScore = document.getElementById('val-domain-score');
  const checkHasTool = document.getElementById('input-has-tool');
  const inputSimService = document.getElementById('input-sim-service');
  const formClassify = document.getElementById('form-classify');
  const presetButtons = document.querySelectorAll('[data-preset]');

  const timelineSlider = document.getElementById('timeline-slider');
  const currentMinuteVal = document.getElementById('current-minute-val');
  const btnPlayPause = document.getElementById('btn-play-pause');
  const iconPlay = document.getElementById('icon-play');
  const iconPause = document.getElementById('icon-pause');
  const playPauseLabel = document.getElementById('play-pause-label');
  const btnResetTimeline = document.getElementById('btn-reset-timeline');

  // Modal elements
  const btnRunLiveDemo = document.getElementById('btn-run-live-demo');
  const demoModal = document.getElementById('demo-modal');
  const btnCloseModal = document.getElementById('btn-close-modal');
  const btnModalDismiss = document.getElementById('btn-modal-dismiss');
  const demoTerminalOutput = document.getElementById('demo-terminal-output');

  // 1. Navigation Tabs
  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      tabs.forEach(t => t.classList.remove('active'));
      panes.forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      const targetPane = document.getElementById(tab.dataset.tab);
      if (targetPane) {
        targetPane.classList.add('active');
        // Trigger resize on charts when tab becomes visible
        if (tab.dataset.tab === 'tab-simulation') {
          setTimeout(() => {
            Object.values(state.charts).forEach(c => c && c.resize());
          }, 50);
        }
      }
    });
  });

  // 2. Sliders and Presets
  sliderOutputLen.addEventListener('input', (e) => {
    valOutputLen.textContent = `${e.target.value} tokens`;
  });

  sliderDomainScore.addEventListener('input', (e) => {
    valDomainScore.textContent = parseFloat(e.target.value).toFixed(2);
  });

  presetButtons.forEach(btn => {
    btn.addEventListener('click', () => {
      presetButtons.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const presetKey = btn.dataset.preset;
      const p = state.presets[presetKey];
      if (p) {
        sliderOutputLen.value = p.outputLen;
        valOutputLen.textContent = `${p.outputLen} tokens`;
        sliderDomainScore.value = p.domainScore;
        valDomainScore.textContent = p.domainScore.toFixed(2);
        checkHasTool.checked = p.hasTool;
        inputSimService.value = p.service;
      }
    });
  });

  // 3. Classifier Request Submission
  formClassify.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btnSend = document.getElementById('btn-send-request');
    const spinner = document.getElementById('btn-spinner');
    const badgeStatus = document.getElementById('badge-response-status');
    const resArchetype = document.getElementById('res-archetype');
    const resConfidence = document.getElementById('res-confidence');
    const resRtt = document.getElementById('res-rtt');
    const nodeDestPool = document.getElementById('node-dest-pool');
    const destPoolName = document.getElementById('dest-pool-name');
    const poolTagPrimitive = document.getElementById('pool-tag-primitive');
    const poolDescText = document.getElementById('pool-desc-text');

    btnSend.disabled = true;
    spinner.style.display = 'inline-block';
    badgeStatus.textContent = 'Classifying...';
    badgeStatus.className = 'badge badge-warning';

    const payload = {
      run_id: `ui-live-${Date.now()}`,
      policy: 'aware',
      request_id: Math.floor(Math.random() * 9000) + 1000,
      minute: state.currentMinute,
      declared_output_len: parseFloat(sliderOutputLen.value),
      has_tool_schema: checkHasTool.checked ? 1 : 0,
      prompt_domain_score: parseFloat(sliderDomainScore.value),
      service_s: parseFloat(inputSimService.value),
      true_archetype: 'agentic_tool_using'
    };

    try {
      const resp = await fetch('/api/classify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await resp.json();

      if (resp.ok) {
        const arch = data.predicted_archetype || 'short_conversational';
        const meta = state.archetypeMeta[arch] || state.archetypeMeta.short_conversational;

        badgeStatus.textContent = `HTTP ${resp.status} Accepted`;
        badgeStatus.className = 'badge badge-success';
        resArchetype.textContent = arch;
        resConfidence.textContent = `${((data.confidence || 1.0) * 100).toFixed(1)}%`;
        resRtt.textContent = `${(data._elapsed_s || 0.25).toFixed(3)}s`;

        destPoolName.textContent = data.pool || meta.pool;
        poolTagPrimitive.textContent = meta.primitive;
        poolDescText.textContent = meta.desc;

        nodeDestPool.classList.add('destination');
      } else {
        badgeStatus.textContent = `Error ${resp.status}`;
        badgeStatus.className = 'badge badge-danger';
      }
    } catch (err) {
      badgeStatus.textContent = 'Connection Error';
      badgeStatus.className = 'badge badge-danger';
    } finally {
      btnSend.disabled = false;
      spinner.style.display = 'none';
    }
  });

  // 4. Trace Timeline & Scrubber
  function updateTimeline(min) {
    state.currentMinute = min;
    timelineSlider.value = min;
    currentMinuteVal.textContent = min;

    if (!state.traceData || !state.traceData.minutes) return;

    const idx = state.traceData.minutes.indexOf(min);
    if (idx === -1) return;

    // Update stat cards
    const aggRate = state.traceData.actual_aggregate[idx] || 50;
    const agenticRate = state.traceData.actual_agentic[idx] || 5;
    const share = ((agenticRate / aggRate) * 100).toFixed(1);
    const awareCap = state.traceData.aware_capacity_total[idx] || 4.0;
    const baseCap = state.traceData.baseline_capacity[idx] || 2.0;

    document.getElementById('stat-agg-rate').textContent = `${aggRate.toFixed(1)} req/min`;
    document.getElementById('stat-agentic-share').textContent = `${share}%`;
    document.getElementById('stat-aware-cap').textContent = `${awareCap.toFixed(1)} units`;
    document.getElementById('stat-base-cap').textContent = `${baseCap.toFixed(1)} units`;

    // Highlight vertical line or scrubber position on charts
    Object.values(state.charts).forEach(chart => {
      if (chart && chart.update) {
        chart.update('none');
      }
    });
  }

  timelineSlider.addEventListener('input', (e) => {
    updateTimeline(parseInt(e.target.value, 10));
  });

  btnPlayPause.addEventListener('click', () => {
    if (state.isPlaying) {
      clearInterval(state.playInterval);
      state.isPlaying = false;
      iconPlay.style.display = 'block';
      iconPause.style.display = 'none';
      playPauseLabel.textContent = 'Play Trace';
    } else {
      state.isPlaying = true;
      iconPlay.style.display = 'none';
      iconPause.style.display = 'block';
      playPauseLabel.textContent = 'Pause';

      state.playInterval = setInterval(() => {
        let nextMin = state.currentMinute + 1;
        if (nextMin > 119) nextMin = 0;
        updateTimeline(nextMin);
      }, 350);
    }
  });

  btnResetTimeline.addEventListener('click', () => {
    if (state.isPlaying) {
      clearInterval(state.playInterval);
      state.isPlaying = false;
      iconPlay.style.display = 'block';
      iconPause.style.display = 'none';
      playPauseLabel.textContent = 'Play Trace';
    }
    updateTimeline(60);
  });

  // 5. Initialize Charts (Chart.js)
  function initCharts(data) {
    const minutes = data.minutes;

    // Common Chart Options
    const commonOptions = {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { color: '#94a3b8', font: { family: 'Inter', size: 11 } } },
        tooltip: {
          backgroundColor: 'rgba(15, 23, 42, 0.9)',
          borderColor: 'rgba(255, 255, 255, 0.1)',
          borderWidth: 1,
          titleFont: { family: 'Inter', size: 12 },
          bodyFont: { family: 'JetBrains Mono', size: 11 }
        }
      },
      scales: {
        x: {
          grid: { color: 'rgba(255, 255, 255, 0.05)' },
          ticks: { color: '#64748b', font: { size: 10 } }
        },
        y: {
          grid: { color: 'rgba(255, 255, 255, 0.05)' },
          ticks: { color: '#64748b', font: { size: 10 } }
        }
      }
    };

    // Chart 1: Forecast Divergence
    const ctxForecast = document.getElementById('chart-forecast').getContext('2d');
    state.charts.forecast = new Chart(ctxForecast, {
      type: 'line',
      data: {
        labels: minutes,
        datasets: [
          {
            label: 'Per-Archetype Agentic Forecast',
            data: data.forecast_agentic,
            borderColor: '#f43f5e',
            backgroundColor: 'rgba(244, 63, 94, 0.1)',
            borderWidth: 2,
            pointRadius: 0,
            fill: true,
          },
          {
            label: 'Baseline Aggregate Forecast',
            data: data.forecast_aggregate,
            borderColor: '#64748b',
            borderWidth: 1.5,
            borderDash: [4, 4],
            pointRadius: 0,
          }
        ]
      },
      options: commonOptions
    });

    // Chart 2: Capacity Allocation
    const ctxCapacity = document.getElementById('chart-capacity').getContext('2d');
    state.charts.capacity = new Chart(ctxCapacity, {
      type: 'line',
      data: {
        labels: minutes,
        datasets: [
          {
            label: 'Aware Total Capacity',
            data: data.aware_capacity_total,
            borderColor: '#10b981',
            borderWidth: 2,
            pointRadius: 0,
            stepped: true
          },
          {
            label: 'Aware EKS GPU (Reserved)',
            data: data.aware_capacity_agentic,
            borderColor: '#06b6d4',
            borderWidth: 1.5,
            pointRadius: 0,
            stepped: true
          },
          {
            label: 'Baseline Capacity (Homogeneous)',
            data: data.baseline_capacity,
            borderColor: '#94a3b8',
            borderWidth: 1.5,
            borderDash: [3, 3],
            pointRadius: 0,
            stepped: true
          }
        ]
      },
      options: commonOptions
    });

    // Chart 3: Cost & Budget
    const ctxBudget = document.getElementById('chart-budget').getContext('2d');
    state.charts.budget = new Chart(ctxBudget, {
      type: 'line',
      data: {
        labels: minutes,
        datasets: [
          {
            label: 'Aware Projected Spend',
            data: data.projected_spend_aware,
            borderColor: '#f59e0b',
            borderWidth: 2,
            pointRadius: 0
          },
          {
            label: 'Baseline Projected Spend',
            data: data.projected_spend_baseline,
            borderColor: '#94a3b8',
            borderWidth: 1.5,
            pointRadius: 0
          },
          {
            label: 'Budget Ceiling ($24.00)',
            data: minutes.map(() => 24.0),
            borderColor: '#ef4444',
            borderWidth: 1.5,
            borderDash: [5, 5],
            pointRadius: 0
          }
        ]
      },
      options: commonOptions
    });

    // Chart 4: Latency & SLA Violations
    const ctxLatency = document.getElementById('chart-latency').getContext('2d');
    const baseSlaViol = minutes.map(m => (m >= 60 ? 0.99 : 0.78));
    const awareSlaViol = minutes.map(m => (m >= 60 ? 0.15 : 0.05));
    state.charts.latency = new Chart(ctxLatency, {
      type: 'line',
      data: {
        labels: minutes,
        datasets: [
          {
            label: 'Baseline SLA Violation Rate',
            data: baseSlaViol,
            borderColor: '#f43f5e',
            backgroundColor: 'rgba(244, 63, 94, 0.15)',
            borderWidth: 2,
            pointRadius: 0,
            fill: true
          },
          {
            label: 'Aware SLA Violation Rate',
            data: awareSlaViol,
            borderColor: '#10b981',
            borderWidth: 2,
            pointRadius: 0
          }
        ]
      },
      options: commonOptions
    });
  }

  // 6. Fetch Trace & System Status
  async function loadInitialData() {
    try {
      const statusResp = await fetch('/api/status');
      const statusData = await statusResp.json();
      if (statusData.aws_connected && statusData.account) {
        document.getElementById('aws-status-text').textContent = `AWS ${statusData.region} (${statusData.account.slice(-4)})`;
      }

      const traceResp = await fetch('/api/trace');
      const traceData = await traceResp.json();
      if (traceData.minutes && traceData.minutes.length > 0) {
        state.traceData = traceData;
        initCharts(traceData);
        updateTimeline(60);
      }
    } catch (e) {
      console.warn('Could not fetch initial server trace:', e);
    }
  }

  // 7. Modal Live Demo Run
  btnRunLiveDemo.addEventListener('click', async () => {
    demoModal.style.display = 'flex';
    demoTerminalOutput.innerHTML = '<span class="term-line prompt">> python -m cloud.demo</span>\n<span class="term-line loader">Dispatching live requests and querying DynamoDB...</span>';

    try {
      const resp = await fetch('/api/run-demo', { method: 'POST' });
      const data = await resp.json();
      if (data.stdout) {
        demoTerminalOutput.textContent = data.stdout;
      } else if (data.stderr) {
        demoTerminalOutput.textContent = `Error:\n${data.stderr}`;
      } else {
        demoTerminalOutput.textContent = JSON.stringify(data, null, 2);
      }
    } catch (err) {
      demoTerminalOutput.textContent = `Execution error: ${err.message}`;
    }
  });

  const closeModal = () => { demoModal.style.display = 'none'; };
  btnCloseModal.addEventListener('click', closeModal);
  btnModalDismiss.addEventListener('click', closeModal);
  demoModal.addEventListener('click', (e) => {
    if (e.target === demoModal) closeModal();
  });

  loadInitialData();
});
