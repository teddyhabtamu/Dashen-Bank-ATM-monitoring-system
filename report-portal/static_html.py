PORTAL_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Report Portal — Dashen Bank</title>
<link rel="icon" type="image/png" href="/static/logo.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://unpkg.com/lucide@latest/dist/umd/lucide.js"></script>
<style>
:root {
  --dashen-blue: #0F2E5A;
  --dashen-blue-light: #1A407A;
  --dashen-gold: #FFCC00;
  --dashen-gold-hover: #E6B800;
  --surface: #F1F5F9; /* Professional slate gray background */
  --card: #FFFFFF;
  --border: #E2E8F0;
  --text-primary: #1E293B;
  --text-secondary: #64748B;
  --success: #10B981;
  --danger: #EF4444;
  --warning: #F59E0B;
  --radius: 8px;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { 
  font-family: 'Inter', sans-serif; 
  background-color: var(--surface); 
  color: var(--text-primary); 
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
  font-size: 13px;
}

/* ── HEADER ── */
.header {
  background: #FFFFFF;
  padding: 0 32px;
  height: 64px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  position: sticky;
  top: 0;
  z-index: 100;
  box-shadow: 0 2px 10px rgba(0,0,0,0.05);
  border-bottom: 3px solid var(--dashen-gold);
}
.header-left { display: flex; align-items: center; gap: 16px; }
.logo-container {
  display: flex;
  align-items: center;
  justify-content: center;
}
.logo-container img { height: 32px; object-fit: contain; }
.header-title { display: flex; flex-direction: column; }
.header-title h1 { font-size: 16px; font-weight: 700; color: var(--dashen-blue); letter-spacing: 0.2px; }
.header-title span { font-size: 11px; color: var(--text-secondary); font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }

.header-right { display: flex; align-items: center; }
.live-badge {
  background: rgba(16, 185, 129, 0.1);
  border: 1px solid rgba(16, 185, 129, 0.2);
  color: var(--success);
  padding: 4px 12px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 6px;
}
.pulse-dot {
  width: 6px; height: 6px; border-radius: 50%; background: #10B981;
  animation: pulse 2s infinite;
}
@keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(16,185,129,0.4); } 70% { box-shadow: 0 0 0 6px rgba(16,185,129,0); } 100% { box-shadow: 0 0 0 0 rgba(16,185,129,0); } }

/* ── BREADCRUMB ── */
.breadcrumb {
  padding: 16px 32px 0;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-secondary);
}
.breadcrumb a { color: var(--dashen-blue); text-decoration: none; font-weight: 500; }

/* ── MAIN CONTENT ── */
.main-container { max-width: 1200px; margin: 0 auto; padding: 20px 32px 40px; }

/* ── KPI SECTION ── */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 16px;
  margin-bottom: 24px;
}
.kpi-card {
  background: var(--card);
  padding: 16px 20px;
  border-radius: var(--radius);
  box-shadow: 0 2px 8px rgba(0,0,0,0.03);
  border: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 6px;
  position: relative;
  overflow: hidden;
  transition: transform 0.2s ease, box-shadow 0.2s ease;
}
.kpi-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 6px 16px rgba(0,0,0,0.06);
}
.kpi-card::before {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px;
  background: var(--dashen-blue); opacity: 0.1;
}
.kpi-card:hover::before { opacity: 1; background: var(--dashen-gold); transition: all 0.3s ease; }

.kpi-header { display: flex; align-items: center; gap: 6px; color: var(--text-secondary); font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
.kpi-value { font-size: 24px; font-weight: 700; color: var(--dashen-blue); letter-spacing: -0.5px; }
.kpi-value.highlight-green { color: var(--success); }
.kpi-value.highlight-gold { color: #D97706; }
.kpi-footer { font-size: 11px; color: var(--text-secondary); }

/* ── HERO BANNER REPORT ── */
.hero-report {
  background: var(--dashen-blue);
  border-radius: var(--radius);
  padding: 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  box-shadow: 0 4px 15px rgba(15, 46, 90, 0.2);
  margin-bottom: 24px;
  gap: 24px;
  flex-wrap: wrap;
}
.hero-content { display: flex; align-items: center; gap: 16px; color: #FFFFFF; }
.hero-icon {
  width: 44px; height: 44px; border-radius: 8px;
  background: rgba(255, 255, 255, 0.1);
  display: flex; align-items: center; justify-content: center; color: var(--dashen-gold);
}
.hero-title h2 { font-size: 18px; font-weight: 700; margin-bottom: 4px; }
.hero-title p { font-size: 12px; color: rgba(255, 255, 255, 0.7); }

.hero-actions { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.hero-actions .form-group label { color: rgba(255, 255, 255, 0.7); }
.hero-actions .btn-gold { padding: 9px 16px; font-size: 13px; border: none; }
.hero-actions .btn-white { background: rgba(255, 255, 255, 0.1); color: #FFFFFF; border: 1px solid rgba(255, 255, 255, 0.2); }
.hero-actions .btn-white:hover { background: rgba(255, 255, 255, 0.2); }

/* ── REPORT CARDS GRID ── */
.reports-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 24px;
}
.report-card {
  background: var(--card);
  border-radius: var(--radius);
  box-shadow: 0 2px 10px rgba(0,0,0,0.03);
  border: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  transition: all 0.2s ease;
  overflow: visible; /* Needed for custom dropdowns */
}
.report-card:hover {
  box-shadow: 0 8px 24px rgba(15, 46, 90, 0.08);
  transform: translateY(-2px);
  border-color: rgba(15, 46, 90, 0.15);
}
.card-accent { height: 3px; width: 100%; border-radius: var(--radius) var(--radius) 0 0; background: var(--dashen-blue); opacity: 0.8; }

.card-content { padding: 20px 24px; flex: 1; display: flex; flex-direction: column; }
.card-header { display: flex; align-items: center; gap: 12px; margin-bottom: 24px; }
.card-icon {
  width: 36px; height: 36px; border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  background: rgba(15, 46, 90, 0.06); 
  color: var(--dashen-blue);
}
.card-icon i { width: 18px; height: 18px; }

.card-header h3 { font-size: 15px; font-weight: 700; color: var(--text-primary); margin: 0; }

/* ── CUSTOM SELECT (DROPDOWN) ── */
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: auto; }
.form-group { display: flex; flex-direction: column; gap: 6px; }
.form-group.full { grid-column: 1 / -1; }
.form-group label { font-size: 10.5px; font-weight: 600; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.5px; }

.custom-select {
  position: relative;
  font-family: inherit;
  width: 100%;
}
.custom-select select { display: none; }
.select-selected {
  background-color: #FFFFFF;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 12px;
  color: var(--text-primary);
  font-weight: 500;
  cursor: pointer;
  user-select: none;
  display: flex;
  justify-content: space-between;
  align-items: center;
  transition: all 0.2s ease;
}
.select-selected:after {
  content: "";
  width: 0; height: 0;
  border: 4px solid transparent;
  border-color: #64748B transparent transparent transparent;
  margin-top: 4px;
}
.select-selected.select-arrow-active {
  border-color: var(--dashen-blue);
  box-shadow: 0 0 0 2px rgba(15, 46, 90, 0.05);
}
.select-selected.select-arrow-active:after {
  border-color: transparent transparent var(--dashen-blue) transparent;
  margin-top: -4px;
}
.select-selected:hover { border-color: #CBD5E1; }

/* White select for the hero section */
.select-white .select-selected {
  background: rgba(255, 255, 255, 0.1);
  color: #FFFFFF;
  border: 1px solid rgba(255, 255, 255, 0.2);
}
.select-white .select-selected:after {
  border-color: #FFFFFF transparent transparent transparent;
}
.select-white .select-selected.select-arrow-active:after {
  border-color: transparent transparent #FFFFFF transparent;
}
.select-white .select-selected:hover {
  background: rgba(255, 255, 255, 0.2);
  border-color: rgba(255, 255, 255, 0.3);
}

.select-items {
  position: absolute;
  background-color: #FFFFFF;
  top: 100%;
  left: 0;
  right: 0;
  z-index: 99;
  margin-top: 4px;
  border-radius: 6px;
  box-shadow: 0 4px 20px rgba(0,0,0,0.1);
  border: 1px solid var(--border);
  overflow: hidden;
  max-height: 180px;
  overflow-y: auto;
}
.select-hide { display: none; }
.select-items div {
  padding: 8px 12px;
  cursor: pointer;
  font-size: 12px;
  color: var(--text-primary);
  transition: background 0.1s;
}
.select-items div:hover, .same-as-selected {
  background-color: rgba(15, 46, 90, 0.05);
  color: var(--dashen-blue);
  font-weight: 500;
}

/* ── BUTTONS & FOOTER ── */
.card-actions {
  padding: 16px 24px;
  background: #F8FAFC;
  border-top: 1px solid var(--border);
  display: flex; flex-wrap: wrap; gap: 8px;
}
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  padding: 7px 12px; border-radius: 6px; border: 1px solid transparent;
  font-size: 12px; font-weight: 500; cursor: pointer;
  transition: all 0.2s ease; font-family: inherit; text-decoration: none;
}
.btn i { width: 13px; height: 13px; }

/* Minimalist Outline Buttons */
.btn-outline { 
  background: #FFFFFF; 
  color: var(--dashen-blue); 
  border-color: var(--border);
}
.btn-outline:hover { 
  background: rgba(15, 46, 90, 0.03); 
  border-color: rgba(15, 46, 90, 0.3);
}

.btn-primary { background: var(--dashen-blue); color: #FFFFFF; font-weight: 600; }
.btn-primary:hover { background: var(--dashen-blue-light); box-shadow: 0 4px 12px rgba(15, 46, 90, 0.2); }
.btn-gold { background: var(--dashen-gold); color: var(--dashen-blue); font-weight: 600; }
.btn-gold:hover { background: var(--dashen-gold-hover); box-shadow: 0 4px 12px rgba(255, 204, 0, 0.2); }

/* ── LOADER ── */
.loading-wrapper { display: none; margin-top: 12px; width: 100%; }
.loading-wrapper.active { display: block; }
.loading-bar {
  height: 3px; width: 100%; border-radius: 2px;
  background: linear-gradient(90deg, var(--dashen-blue), var(--dashen-gold), var(--dashen-blue));
  background-size: 200% 100%; animation: loading 1.5s linear infinite;
}
.loading-text { font-size: 11px; font-weight: 500; color: var(--text-secondary); margin-top: 6px; text-align: center; }
@keyframes loading { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }

/* RESPONSIVE */
@media(max-width: 768px){
  .header { padding: 10px 16px; height: auto; min-height: 60px; flex-wrap: wrap; gap: 12px; justify-content: space-between; }
  .logo-container img { height: 24px; }
  .header-title h1 { font-size: 14px; }
  .header-title span { font-size: 10px; }
  
  .breadcrumb { padding: 12px 16px 0; font-size: 11px; flex-wrap: wrap; line-height: 1.5; }
  
  .main-container { padding: 16px; }
  
  .kpi-grid { grid-template-columns: repeat(2, 1fr); gap: 12px; }
  .kpi-card { padding: 12px 16px; }
  .kpi-value { font-size: 20px; }
  
  .hero-report { flex-direction: column; align-items: flex-start; padding: 20px 16px; gap: 16px; }
  .hero-actions { width: 100%; flex-direction: column; align-items: stretch; gap: 16px; }
  .hero-actions .form-group { min-width: 100%; }
  .hero-actions > div:last-child { flex-direction: column; width: 100%; }
  .hero-actions .btn { width: 100%; justify-content: center; }
  
  .reports-grid { grid-template-columns: 1fr; gap: 16px; }
  .card-content { padding: 16px; }
  .form-grid { grid-template-columns: 1fr; }
  
  .card-actions { padding: 16px; flex-direction: column; gap: 8px; }
  .card-actions .btn { width: 100%; justify-content: center; }
}

@media(max-width: 480px){
  .kpi-grid { grid-template-columns: 1fr; }
  .header { flex-direction: column; align-items: flex-start; }
}
</style>
</head>
<body>

<header class="header">
  <div class="header-left">
    <div class="logo-container">
      <img src="/static/logo.png" alt="Dashen Bank" />
    </div>
    <div class="header-title">
      <h1>ATM Report Portal</h1>
      <span>Operational Intelligence</span>
    </div>
  </div>
  <div class="header-right">
    <div class="live-badge">
      <span class="pulse-dot"></span> System Online
    </div>
  </div>
</header>

<div class="breadcrumb">
  <i data-lucide="home" style="width: 12px; height: 12px;"></i>
  <a href="#">Dashen Bank</a>
  <i data-lucide="chevron-right" style="width: 12px; height: 12px; opacity: 0.5;"></i>
  <a id="grafana-link" href="#" target="_blank">ATM Monitoring</a>
<script>document.getElementById('grafana-link').href='http://'+window.location.hostname+':3002';</script>
  <i data-lucide="chevron-right" style="width: 12px; height: 12px; opacity: 0.5;"></i>
  <span style="color: var(--text-primary); font-weight: 500;">Report Centre</span>
</div>

<main class="main-container">

  <div class="kpi-grid">
    <div class="kpi-card">
      <div class="kpi-header"><i data-lucide="activity" style="width: 14px; color: var(--dashen-blue);"></i> Transactions (7d)</div>
      <div class="kpi-value" id="kpi-txns">—</div>
      <div class="kpi-footer">Total network volume</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-header"><i data-lucide="check-circle" style="width: 14px; color: var(--success);"></i> Success Rate</div>
      <div class="kpi-value highlight-green" id="kpi-rate">—</div>
      <div class="kpi-footer">Approved vs Total</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-header"><i data-lucide="banknote" style="width: 14px; color: #D97706;"></i> Cash Dispensed</div>
      <div class="kpi-value highlight-gold" id="kpi-cash">—</div>
      <div class="kpi-footer">ETB Withdrawn Today</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-header"><i data-lucide="server" style="width: 14px; color: var(--text-secondary);"></i> Monitored ATMs</div>
      <div class="kpi-value" style="color: var(--text-primary);">5</div>
      <div class="kpi-footer">Active endpoints</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-header"><i data-lucide="clock" style="width: 14px; color: var(--text-secondary);"></i> Last Updated</div>
      <div class="kpi-value" style="font-size: 18px; color: var(--text-primary); line-height: 28px;" id="kpi-time">—</div>
      <div class="kpi-footer">Auto-sync active</div>
    </div>
  </div>

  <!-- Hero Report Banner -->
  <div class="hero-report">
    <div class="hero-content">
      <div class="hero-icon"><i data-lucide="folder-check"></i></div>
      <div class="hero-title">
        <h2>Complete Management Report</h2>
        <p>Comprehensive document containing transactions, cash, errors, and performance data.</p>
      </div>
    </div>
    <div class="hero-actions">
      <div class="form-group" style="min-width: 150px;">
        <label>Reporting Period</label>
        <div class="custom-select select-white">
          <select id="full-days">
            <option value="7" selected>Last 7 Days</option>
            <option value="30">Last 30 Days</option>
            <option value="90">Last 90 Days</option>
          </select>
        </div>
      </div>
      <div style="display: flex; gap: 8px; margin-top: 14px;">
        <button class="btn btn-gold" onclick="gen('full','excel','full-days')">
          <i data-lucide="file-spreadsheet"></i> Excel Workbook
        </button>
        <button class="btn btn-white" onclick="gen('full','pdf','full-days')">
          <i data-lucide="file-text"></i> PDF Report
        </button>
      </div>
    </div>
    <div class="loading-wrapper" id="lw-full" style="width: 100%;">
      <div class="loading-bar" style="background: linear-gradient(90deg, #FFFFFF, var(--dashen-gold), #FFFFFF);"></div>
      <div class="loading-text" style="color: #FFFFFF;">Compiling Complete Report...</div>
    </div>
  </div>

  <div class="reports-grid">

    <!-- Transaction Summary -->
    <div class="report-card">
      <div class="card-accent"></div>
      <div class="card-content">
        <div class="card-header">
          <div class="card-icon"><i data-lucide="bar-chart-2"></i></div>
          <h3>Transaction Summary</h3>
        </div>
        <div class="form-grid">
          <div class="form-group">
            <label>Period</label>
            <div class="custom-select">
              <select id="txn-days">
                <option value="1">Today</option>
                <option value="7" selected>Last 7 Days</option>
                <option value="30">Last 30 Days</option>
                <option value="90">Last 90 Days</option>
              </select>
            </div>
          </div>
          <div class="form-group">
            <label>Branch</label>
            <div class="custom-select">
              <select id="txn-atm">
                <option value="all" selected>All Branches</option>
                <option value="ATM-001">ATM-001 — Addis Main</option>
                <option value="ATM-002">ATM-002 — Bole</option>
                <option value="ATM-003">ATM-003 — Merkato</option>
                <option value="ATM-004">ATM-004 — Hawassa</option>
                <option value="ATM-005">ATM-005 — Dire Dawa</option>
              </select>
            </div>
          </div>
        </div>
      </div>
      <div class="card-actions">
        <button class="btn btn-outline" onclick="gen('transaction','excel','txn-days','txn-atm')"><i data-lucide="file-spreadsheet"></i> Excel</button>
        <button class="btn btn-outline" onclick="gen('transaction','pdf','txn-days','txn-atm')"><i data-lucide="file-text"></i> PDF</button>
        <button class="btn btn-outline" onclick="gen('transaction','csv','txn-days','txn-atm')"><i data-lucide="file"></i> CSV</button>
        <div class="loading-wrapper" id="lw-transaction">
          <div class="loading-bar"></div><div class="loading-text">Generating...</div>
        </div>
      </div>
    </div>

    <!-- Cash Level Report -->
    <div class="report-card">
      <div class="card-accent"></div>
      <div class="card-content">
        <div class="card-header">
          <div class="card-icon"><i data-lucide="coins"></i></div>
          <h3>Cash Level Report</h3>
        </div>
        <div class="form-grid">
          <div class="form-group full">
            <label>Reporting Period</label>
            <div class="custom-select">
              <select id="cash-days">
                <option value="1">Today</option>
                <option value="7" selected>Last 7 Days</option>
                <option value="30">Last 30 Days</option>
                <option value="90">Last 90 Days</option>
              </select>
            </div>
          </div>
        </div>
      </div>
      <div class="card-actions">
        <button class="btn btn-outline" onclick="gen('cash','excel','cash-days')"><i data-lucide="file-spreadsheet"></i> Excel</button>
        <button class="btn btn-outline" onclick="gen('cash','pdf','cash-days')"><i data-lucide="file-text"></i> PDF</button>
        <button class="btn btn-outline" onclick="gen('cash','csv','cash-days')"><i data-lucide="file"></i> CSV</button>
        <div class="loading-wrapper" id="lw-cash">
          <div class="loading-bar"></div><div class="loading-text">Generating...</div>
        </div>
      </div>
    </div>

    <!-- Error & Incident Report -->
    <div class="report-card">
      <div class="card-accent"></div>
      <div class="card-content">
        <div class="card-header">
          <div class="card-icon"><i data-lucide="alert-triangle"></i></div>
          <h3>Error & Incident Report</h3>
        </div>
        <div class="form-grid">
          <div class="form-group full">
            <label>Reporting Period</label>
            <div class="custom-select">
              <select id="error-days">
                <option value="1">Today</option>
                <option value="7" selected>Last 7 Days</option>
                <option value="30">Last 30 Days</option>
              </select>
            </div>
          </div>
        </div>
      </div>
      <div class="card-actions">
        <button class="btn btn-outline" onclick="gen('error','excel','error-days')"><i data-lucide="file-spreadsheet"></i> Excel</button>
        <button class="btn btn-outline" onclick="gen('error','pdf','error-days')"><i data-lucide="file-text"></i> PDF</button>
        <button class="btn btn-outline" onclick="gen('error','csv','error-days')"><i data-lucide="file"></i> CSV</button>
        <div class="loading-wrapper" id="lw-error">
          <div class="loading-bar"></div><div class="loading-text">Generating...</div>
        </div>
      </div>
    </div>

    <!-- ATM Performance Report -->
    <div class="report-card">
      <div class="card-accent"></div>
      <div class="card-content">
        <div class="card-header">
          <div class="card-icon"><i data-lucide="zap"></i></div>
          <h3>ATM Performance Report</h3>
        </div>
        <div class="form-grid">
          <div class="form-group full">
            <label>Reporting Period</label>
            <div class="custom-select">
              <select id="perf-days">
                <option value="7" selected>Last 7 Days</option>
                <option value="30">Last 30 Days</option>
                <option value="90">Last 90 Days</option>
              </select>
            </div>
          </div>
        </div>
      </div>
      <div class="card-actions">
        <button class="btn btn-outline" onclick="gen('performance','excel','perf-days')"><i data-lucide="file-spreadsheet"></i> Excel</button>
        <button class="btn btn-outline" onclick="gen('performance','pdf','perf-days')"><i data-lucide="file-text"></i> PDF</button>
        <button class="btn btn-outline" onclick="gen('performance','csv','perf-days')"><i data-lucide="file"></i> CSV</button>
        <div class="loading-wrapper" id="lw-performance">
          <div class="loading-bar"></div><div class="loading-text">Generating...</div>
        </div>
      </div>
    </div>

  </div>
</main>

<script>
lucide.createIcons();

// Custom Dropdown Logic
function initCustomSelects() {
  var x, i, j, l, ll, selElmnt, a, b, c;
  x = document.getElementsByClassName("custom-select");
  l = x.length;
  for (i = 0; i < l; i++) {
    selElmnt = x[i].getElementsByTagName("select")[0];
    ll = selElmnt.length;
    a = document.createElement("DIV");
    a.setAttribute("class", "select-selected");
    a.innerHTML = selElmnt.options[selElmnt.selectedIndex].innerHTML;
    x[i].appendChild(a);
    b = document.createElement("DIV");
    b.setAttribute("class", "select-items select-hide");
    for (j = 0; j < ll; j++) {
      c = document.createElement("DIV");
      c.innerHTML = selElmnt.options[j].innerHTML;
      c.setAttribute("data-value", selElmnt.options[j].value);
      if (j === selElmnt.selectedIndex) {
        c.setAttribute("class", "same-as-selected");
      }
      c.addEventListener("click", function(e) {
          var y, i, k, s, h, sl, yl;
          s = this.parentNode.parentNode.getElementsByTagName("select")[0];
          sl = s.length;
          h = this.parentNode.previousSibling;
          for (i = 0; i < sl; i++) {
            if (s.options[i].innerHTML == this.innerHTML) {
              s.selectedIndex = i;
              h.innerHTML = this.innerHTML;
              y = this.parentNode.getElementsByClassName("same-as-selected");
              yl = y.length;
              for (k = 0; k < yl; k++) {
                y[k].removeAttribute("class");
              }
              this.setAttribute("class", "same-as-selected");
              s.dispatchEvent(new Event('change'));
              break;
            }
          }
          h.click();
      });
      b.appendChild(c);
    }
    x[i].appendChild(b);
    a.addEventListener("click", function(e) {
        e.stopPropagation();
        closeAllSelect(this);
        this.nextSibling.classList.toggle("select-hide");
        this.classList.toggle("select-arrow-active");
    });
  }
}
function closeAllSelect(elmnt) {
  var x, y, i, xl, yl, arrNo = [];
  x = document.getElementsByClassName("select-items");
  y = document.getElementsByClassName("select-selected");
  xl = x.length;
  yl = y.length;
  for (i = 0; i < yl; i++) {
    if (elmnt == y[i]) {
      arrNo.push(i)
    } else {
      y[i].classList.remove("select-arrow-active");
    }
  }
  for (i = 0; i < xl; i++) {
    if (arrNo.indexOf(i)) {
      x[i].classList.add("select-hide");
    }
  }
}
document.addEventListener("click", closeAllSelect);
initCustomSelects();


// Data Logic
async function loadKPIs() {
  try {
    const d = await fetch('/api/stats').then(r=>r.json());
    document.getElementById('kpi-txns').textContent = d.total_txns.toLocaleString();
    document.getElementById('kpi-rate').textContent = d.success_rate + '%';
    document.getElementById('kpi-cash').textContent =
      (d.cash_today >= 1000000
        ? (d.cash_today/1000000).toFixed(1)+'M'
        : (d.cash_today/1000).toFixed(0)+'K');
    document.getElementById('kpi-time').textContent =
      new Date().toLocaleTimeString('en-ET',{hour12:false});
  } catch(e){}
}

function gen(type, fmt, daysId, atmId) {
  const days = document.getElementById(daysId).value;
  const atm  = atmId ? document.getElementById(atmId).value : 'all';
  const lw = document.getElementById('lw-'+type);
  lw.classList.add('active');

  fetch(`/report/${type}/${fmt}?days=${days}&atm=${atm}`)
    .then(r => { if(!r.ok) throw new Error('Failed'); return r.blob(); })
    .then(blob => {
      const ext = fmt==='excel'?'xlsx': fmt==='pdf'?'pdf':'csv';
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `Dashen_ATM_${type}_${new Date().toISOString().slice(0,10)}.${ext}`;
      a.click();
    })
    .catch(e => alert('Report generation failed: ' + e.message))
    .finally(() => {
      lw.classList.remove('active');
    });
}

loadKPIs();
setInterval(loadKPIs, 30000);
</script>
</body>
</html>
"""
