PORTAL_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Report Portal — Dashen Bank</title>
<link rel="icon" type="image/png" href="/static/logo.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://unpkg.com/lucide@latest/dist/umd/lucide.js"></script>
<style>
:root {
  --dashen-navy: #012169;
  --dashen-blue: #273274;
  --dashen-gold: #FDD79A;
  --surface: #F0F2F6;
  --card: #FFFFFF;
  --border: #E4E9F1;
  --border-light: #EEF1F6;
  --text-primary: #0F172A;
  --text-secondary: #475569;
  --text-tertiary: #94A3B8;
  --success: #059669;
  --success-bg: rgba(5, 150, 105, 0.08);
  --danger: #DC2626;
  --warning: #D97706;
  --radius: 12px;
  --radius-sm: 8px;
  --radius-xs: 6px;
  --shadow-sm: 0 1px 3px rgba(1, 33, 105, 0.04), 0 1px 2px rgba(1, 33, 105, 0.02);
  --shadow-md: 0 4px 16px rgba(1, 33, 105, 0.06), 0 1px 4px rgba(1, 33, 105, 0.03);
  --shadow-lg: 0 12px 40px rgba(1, 33, 105, 0.08), 0 4px 12px rgba(1, 33, 105, 0.03);
  --shadow-xl: 0 20px 60px rgba(1, 33, 105, 0.1), 0 8px 20px rgba(1, 33, 105, 0.04);
  --transition: 0.25s cubic-bezier(0.4, 0, 0.2, 1);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  background: var(--surface);
  color: var(--text-primary);
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
  font-size: 13px;
  line-height: 1.5;
}

/* ── HEADER ── */
.header {
  background: rgba(255,255,255,0.92);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  padding: 0 36px;
  height: 68px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  position: sticky;
  top: 0;
  z-index: 100;
  border-bottom: 1px solid var(--border-light);
}
.header::after {
  content: '';
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  height: 3px;
  background: var(--dashen-gold);
  opacity: 0.6;
}
.header-left { display: flex; align-items: center; gap: 18px; }
.logo-container {
  display: flex;
  align-items: center;
  justify-content: center;
}
.logo-container img { height: 34px; object-fit: contain; }
.header-divider { width: 1px; height: 32px; background: var(--border); }
.header-title { display: flex; flex-direction: column; }
.header-brand { font-size: 16px; font-weight: 800; color: var(--dashen-navy); letter-spacing: -0.2px; line-height: 1.3; }
.header-sub { font-size: 10px; color: var(--text-tertiary); font-weight: 500; letter-spacing: 0.5px; margin-top: -1px; }

.header-right { display: flex; align-items: center; gap: 14px; }
.live-badge {
  background: var(--success-bg);
  border: 1px solid rgba(5, 150, 105, 0.15);
  color: var(--success);
  padding: 6px 16px;
  border-radius: 100px;
  font-size: 11px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
  letter-spacing: 0.2px;
}
.pulse-dot {
  width: 7px; height: 7px; border-radius: 50%; background: var(--success);
  animation: pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite;
  flex-shrink: 0;
}
@keyframes pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(5, 150, 105, 0.4); }
  50% { box-shadow: 0 0 0 6px rgba(5, 150, 105, 0); }
}

/* ── BREADCRUMB ── */
.breadcrumb {
  padding: 20px 36px 0;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-tertiary);
}
.breadcrumb a {
  color: var(--text-secondary);
  text-decoration: none;
  font-weight: 500;
  transition: color 0.15s;
}
.breadcrumb a:hover { color: var(--dashen-blue); }

/* ── MAIN CONTENT ── */
.main-container { max-width: 1240px; margin: 0 auto; padding: 20px 36px 48px; }

/* ── KPI SECTION ── */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 18px;
  margin-bottom: 28px;
}
.kpi-card {
  background: var(--card);
  padding: 20px 22px;
  border-radius: var(--radius);
  box-shadow: var(--shadow-sm);
  border: 1px solid var(--border-light);
  display: flex;
  flex-direction: column;
  gap: 10px;
  position: relative;
  overflow: hidden;
  transition: transform var(--transition), box-shadow var(--transition), border-color var(--transition);
}
.kpi-card:hover {
  transform: translateY(-3px);
  box-shadow: var(--shadow-md);
  border-color: var(--border);
}
.kpi-card::before {
  content: '';
  position: absolute;
  top: -24px;
  right: -24px;
  width: 72px;
  height: 72px;
  border-radius: 50%;
  opacity: 0.04;
  background: var(--dashen-navy);
  transition: all 0.4s ease;
  pointer-events: none;
}
.kpi-card:hover::before {
  opacity: 0.08;
  transform: scale(1.3);
}
.kpi-card::after {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 3px;
  background: var(--dashen-navy);
  border-radius: var(--radius) var(--radius) 0 0;
  opacity: 0.12;
  transition: opacity var(--transition);
}
.kpi-card:hover::after {
  opacity: 1;
  background: var(--dashen-gold);
}

.kpi-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.6px;
}
.kpi-icon-wrap {
  width: 30px;
  height: 30px;
  border-radius: var(--radius-xs);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  font-size: 14px;
}
.kpi-icon-wrap.navy { background: rgba(1, 33, 105, 0.06); color: var(--dashen-navy); }
.kpi-icon-wrap.green { background: var(--success-bg); color: var(--success); }
.kpi-icon-wrap.gold { background: rgba(253, 215, 154, 0.08); color: var(--warning); }
.kpi-icon-wrap.gray { background: rgba(148, 163, 184, 0.1); color: var(--text-secondary); }

.kpi-value {
  font-size: 28px;
  font-weight: 800;
  color: var(--dashen-navy);
  letter-spacing: -0.8px;
  line-height: 1.1;
  font-variant-numeric: tabular-nums;
}
.kpi-value.highlight-green { color: var(--success); }
.kpi-value.highlight-gold { color: var(--warning); }
.kpi-footer { font-size: 11px; color: var(--text-tertiary); font-weight: 400; }

/* ── HERO BANNER REPORT ── */
.hero-report {
  background: var(--dashen-navy);
  border-radius: var(--radius);
  padding: 30px 34px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  box-shadow: var(--shadow-xl);
  margin-bottom: 28px;
  gap: 28px;
  flex-wrap: wrap;
  position: relative;
  z-index: 5;
  overflow: visible;
}
.hero-content { display: flex; align-items: center; gap: 20px; color: #FFFFFF; position: relative; z-index: 1; }
.hero-icon {
  width: 54px;
  height: 54px;
  border-radius: var(--radius-sm);
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid rgba(255, 255, 255, 0.12);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--dashen-gold);
  flex-shrink: 0;
}
.hero-icon i { width: 24px; height: 24px; }
.hero-title h2 { font-size: 20px; font-weight: 700; margin-bottom: 4px; letter-spacing: -0.3px; }
.hero-title p { font-size: 13px; color: rgba(255,255,255,0.55); font-weight: 400; line-height: 1.5; max-width: 420px; }

.hero-actions { display: flex; align-items: flex-end; gap: 16px; flex-wrap: wrap; position: relative; z-index: 1; }
.hero-actions .form-group label { color: rgba(255,255,255,0.5); font-size: 10px; }
.hero-actions .btn-gold { padding: 10px 20px; font-size: 13px; border: none; font-weight: 600; }
.hero-actions .btn-white {
  background: rgba(255,255,255,0.08);
  color: #FFFFFF;
  border: 1px solid rgba(255,255,255,0.15);
  backdrop-filter: blur(4px);
}
.hero-actions .btn-white:hover {
  background: rgba(255,255,255,0.15);
  border-color: rgba(255,255,255,0.25);
}

/* ── REPORT CARDS GRID ── */
.section-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.8px;
  margin-bottom: 18px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.section-title::after {
  content: '';
  flex: 1;
  height: 1px;
  background: var(--border);
}

.reports-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 22px;
}
.report-card {
  background: var(--card);
  border-radius: var(--radius);
  box-shadow: var(--shadow-sm);
  border: 1px solid var(--border-light);
  display: flex;
  flex-direction: column;
  transition: transform var(--transition), box-shadow var(--transition), border-color var(--transition);
  overflow: visible;
  position: relative;
  z-index: 1;
}
.report-card:hover {
  transform: translateY(-3px);
  box-shadow: var(--shadow-lg);
  border-color: rgba(39, 50, 116, 0.12);
  z-index: 10;
}
.card-accent {
  height: 3px;
  width: 100%;
  border-radius: var(--radius) var(--radius) 0 0;
  background: var(--dashen-navy);
  opacity: 0.8;
  transition: background 0.3s ease, opacity 0.3s ease;
}
.report-card:hover .card-accent {
  background: var(--dashen-gold);
  opacity: 1;
}

.card-content { padding: 24px 26px; flex: 1; display: flex; flex-direction: column; }
.card-header { display: flex; align-items: center; gap: 14px; margin-bottom: 26px; }
.card-icon {
  width: 42px;
  height: 42px;
  border-radius: var(--radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(1, 33, 105, 0.05);
  color: var(--dashen-navy);
  flex-shrink: 0;
  transition: background var(--transition), color var(--transition);
}
.report-card:hover .card-icon {
  background: rgba(39, 50, 116, 0.08);
  color: var(--dashen-blue);
}
.card-icon i { width: 18px; height: 18px; }
.card-header h3 { font-size: 15px; font-weight: 700; color: var(--text-primary); margin: 0; letter-spacing: -0.1px; }

/* ── CUSTOM SELECT ── */
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: auto; }
.form-group { display: flex; flex-direction: column; gap: 6px; }
.form-group.full { grid-column: 1 / -1; }
.form-group label {
  font-size: 10px;
  font-weight: 600;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.7px;
}

.custom-select {
  position: relative;
  font-family: inherit;
  width: 100%;
}
.custom-select select { display: none; }
.select-selected {
  background-color: var(--surface);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-xs);
  padding: 9px 14px;
  font-size: 12px;
  color: var(--text-primary);
  font-weight: 500;
  cursor: pointer;
  user-select: none;
  display: flex;
  justify-content: space-between;
  align-items: center;
  transition: border-color var(--transition), box-shadow var(--transition), background var(--transition);
}
.select-selected:after {
  content: "";
  width: 0; height: 0;
  border: 4px solid transparent;
  border-color: var(--text-tertiary) transparent transparent transparent;
  margin-top: 4px;
  transition: transform 0.2s ease;
}
.select-selected.select-arrow-active {
  border-color: var(--dashen-blue);
  background: #FFFFFF;
  box-shadow: 0 0 0 3px rgba(39, 50, 116, 0.06);
}
.select-selected.select-arrow-active:after {
  transform: rotate(180deg);
  margin-top: -4px;
}
.select-selected:hover { border-color: var(--border); background: #FFFFFF; }

/* White select for hero */
.select-white .select-selected {
  background: rgba(255,255,255,0.08);
  color: #FFFFFF;
  border: 1px solid rgba(255,255,255,0.15);
}
.select-white .select-selected:after {
  border-color: rgba(255,255,255,0.5) transparent transparent transparent;
}
.select-white .select-selected.select-arrow-active {
  background: rgba(255,255,255,0.15);
  box-shadow: 0 0 0 3px rgba(255,255,255,0.06);
  border-color: rgba(255,255,255,0.3);
}
.select-white .select-selected:hover {
  background: rgba(255,255,255,0.13);
  border-color: rgba(255,255,255,0.25);
}

.select-items {
  position: absolute;
  background-color: #FFFFFF;
  top: 100%;
  left: 0;
  right: 0;
  z-index: 999;
  margin-top: 6px;
  border-radius: var(--radius-xs);
  box-shadow: var(--shadow-lg);
  border: 1px solid var(--border);
  overflow: hidden;
  max-height: 200px;
  overflow-y: auto;
}
.select-hide { display: none; }
.select-items div {
  padding: 9px 14px;
  cursor: pointer;
  font-size: 12px;
  color: var(--text-primary);
  font-weight: 400;
  transition: background 0.1s ease;
}
.select-items div:hover, .same-as-selected {
  background-color: rgba(39, 50, 116, 0.05);
  color: var(--dashen-blue);
  font-weight: 500;
}

/* ── BUTTONS & FOOTER ── */
.card-actions {
  padding: 16px 26px 20px;
  background: #FAFBFC;
  border-top: 1px solid var(--border-light);
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  border-radius: 0 0 var(--radius) var(--radius);
}
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  padding: 8px 14px;
  border-radius: var(--radius-xs);
  border: 1px solid transparent;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  font-family: inherit;
  text-decoration: none;
  line-height: 1;
  transition: all var(--transition);
}
.btn i { width: 14px; height: 14px; }

.btn-outline {
  background: #FFFFFF;
  color: var(--text-secondary);
  border-color: var(--border);
}
.btn-outline:hover {
  color: var(--dashen-blue);
  border-color: var(--dashen-blue);
  background: rgba(39, 50, 116, 0.03);
  box-shadow: 0 1px 4px rgba(39, 50, 116, 0.06);
  transform: translateY(-1px);
}

.btn-primary {
  background: var(--dashen-navy);
  color: #FFFFFF;
  font-weight: 600;
}
.btn-primary:hover {
  background: var(--dashen-blue);
  box-shadow: 0 4px 16px rgba(1, 33, 105, 0.25);
  transform: translateY(-1px);
}

.btn-gold {
  background: var(--dashen-gold);
  color: var(--dashen-navy);
  font-weight: 700;
  border: none;
  letter-spacing: -0.1px;
}
.btn-gold:hover {
  background: #C49A00;
  box-shadow: 0 4px 16px rgba(253, 215, 154, 0.3);
  transform: translateY(-1px);
}

.btn-white {
  font-weight: 500;
}

/* ── LOADER ── */
.loading-wrapper { display: none; margin-top: 12px; width: 100%; }
.loading-wrapper.active { display: block; }
.loading-bar {
  height: 3px;
  width: 0%;
  border-radius: 3px;
  background: var(--dashen-navy);
  animation: loading 1.8s ease-in-out infinite;
}
.loading-text { font-size: 11px; font-weight: 500; color: var(--text-secondary); margin-top: 8px; text-align: center; }
@keyframes loading { 0% { width: 0%; opacity: 1; } 50% { width: 100%; opacity: 1; } 100% { width: 100%; opacity: 0; } }

/* ── FOOTER ── */
.footer {
  text-align: center;
  padding: 24px 36px;
  font-size: 11px;
  color: var(--text-tertiary);
  border-top: 1px solid var(--border-light);
  margin-top: 12px;
}
.footer a { color: var(--text-secondary); text-decoration: none; font-weight: 500; }
.footer a:hover { color: var(--dashen-blue); }

/* ── SCROLLBAR ── */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #D1D5DB; border-radius: 100px; }
::-webkit-scrollbar-thumb:hover { background: #9CA3AF; }

/* ── RESPONSIVE ── */
@media(max-width: 1024px){
  .kpi-grid { grid-template-columns: repeat(3, 1fr); }
}
@media(max-width: 768px){
  .header { padding: 12px 20px; height: auto; min-height: 60px; flex-wrap: wrap; gap: 12px; }
  .header::after { display: none; }
  .logo-container img { height: 26px; }
  .header-brand { font-size: 14px; }
  .header-sub { font-size: 9px; }

  .breadcrumb { padding: 14px 20px 0; font-size: 11px; flex-wrap: wrap; line-height: 1.6; }

  .main-container { padding: 16px 20px 40px; }

  .kpi-grid { grid-template-columns: repeat(2, 1fr); gap: 14px; }
  .kpi-card { padding: 16px 18px; }
  .kpi-value { font-size: 24px; }

  .hero-report { flex-direction: column; align-items: flex-start; padding: 24px 20px; gap: 18px; }
  .hero-actions { width: 100%; flex-direction: column; align-items: stretch; gap: 14px; }
  .hero-actions .form-group { min-width: 100%; }
  .hero-actions > div:last-child { flex-direction: column; width: 100%; }
  .hero-actions .btn { width: 100%; justify-content: center; padding: 11px 16px; }

  .reports-grid { grid-template-columns: 1fr; gap: 18px; }
  .card-content { padding: 20px; }
  .form-grid { grid-template-columns: 1fr; }

  .card-actions { padding: 14px 20px; flex-direction: column; gap: 8px; }
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
    <div class="header-divider"></div>
    <div class="header-title">
      <span class="header-brand">Dashen Bank</span>
      <span class="header-sub">ATM Report Portal</span>
    </div>
  </div>
  <div class="header-right">
    <a href="/admin/atm" class="btn btn-outline" style="padding:6px 14px;font-size:11px;font-weight:600;text-decoration:none;">
      <i data-lucide="settings" style="width:13px;height:13px"></i> ATM Admin
    </a>
    <a href="/ej-search" class="btn btn-outline" style="padding:6px 14px;font-size:11px;font-weight:600;text-decoration:none;">
      <i data-lucide="file-search" style="width:13px;height:13px"></i> EJ Search
    </a>
    <div class="live-badge">
      <span class="pulse-dot"></span> System Online
    </div>
  </div>
</header>

<div class="breadcrumb">
  <i data-lucide="home" style="width: 12px; height: 12px;"></i>
  <a href="#">Dashen Bank</a>
  <i data-lucide="chevron-right" style="width: 12px; height: 12px; opacity: 0.35;"></i>
  <a id="grafana-link" href="#" target="_blank">ATM Monitoring</a>
  <script>document.getElementById('grafana-link').href='http://'+window.location.hostname+':3002';</script>
  <i data-lucide="chevron-right" style="width: 12px; height: 12px; opacity: 0.35;"></i>
  <span style="color: var(--text-primary); font-weight: 600;">Report Centre</span>
</div>

<main class="main-container">

  <!-- KPI Cards -->
  <div class="kpi-grid">
    <div class="kpi-card">
      <div class="kpi-header">
        <div class="kpi-icon-wrap navy"><i data-lucide="activity" style="width: 14px; height: 14px;"></i></div>
        Transactions (7d)
      </div>
      <div class="kpi-value" id="kpi-txns">&mdash;</div>
      <div class="kpi-footer">Total network volume</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-header">
        <div class="kpi-icon-wrap green"><i data-lucide="check-circle" style="width: 14px; height: 14px;"></i></div>
        Success Rate
      </div>
      <div class="kpi-value highlight-green" id="kpi-rate">&mdash;</div>
      <div class="kpi-footer">Approved vs Total</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-header">
        <div class="kpi-icon-wrap gold"><i data-lucide="banknote" style="width: 14px; height: 14px;"></i></div>
        Cash Dispensed
      </div>
      <div class="kpi-value highlight-gold" id="kpi-cash">&mdash;</div>
      <div class="kpi-footer">ETB Withdrawn Today</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-header">
        <div class="kpi-icon-wrap gray"><i data-lucide="server" style="width: 14px; height: 14px;"></i></div>
        Monitored ATMs
      </div>
      <div class="kpi-value" style="color: var(--text-primary);" id="kpi-atm-count">&mdash;</div>
      <div class="kpi-footer">Active endpoints</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-header">
        <div class="kpi-icon-wrap gray"><i data-lucide="clock" style="width: 14px; height: 14px;"></i></div>
        Last Updated
      </div>
      <div class="kpi-value" style="font-size: 20px; color: var(--text-primary); line-height: 28px;" id="kpi-time">&mdash;</div>
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
      <div style="display: flex; gap: 10px; margin-top: 14px;">
        <button class="btn btn-gold" onclick="gen('full','excel','full-days')">
          <i data-lucide="file-spreadsheet"></i> Excel Workbook
        </button>
        <button class="btn btn-white" onclick="gen('full','pdf','full-days')">
          <i data-lucide="file-text"></i> PDF Report
        </button>
      </div>
    </div>
    <div class="loading-wrapper" id="lw-full" style="width: 100%;">
      <div class="loading-bar" style="background: var(--dashen-gold);"></div>
      <div class="loading-text" style="color: rgba(255,255,255,0.7);">Compiling Complete Report...</div>
    </div>
  </div>

  <!-- Report Section -->
  <div class="section-title">Individual Reports</div>

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
          <h3>Error &amp; Incident Report</h3>
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

    <!-- ATM Availability Report -->
    <div class="report-card">
      <div class="card-accent"></div>
      <div class="card-content">
        <div class="card-header">
          <div class="card-icon"><i data-lucide="gauge"></i></div>
          <h3>ATM Availability Report</h3>
        </div>
        <div class="form-grid">
          <div class="form-group">
            <label>Period</label>
            <div class="custom-select">
              <select id="avail-days">
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
              <select id="avail-atm">
                <option value="all" selected>All Branches</option>
              </select>
            </div>
          </div>
        </div>
      </div>
      <div class="card-actions">
        <button class="btn btn-outline" onclick="gen('availability','excel','avail-days','avail-atm')"><i data-lucide="file-spreadsheet"></i> Excel</button>
        <button class="btn btn-outline" onclick="gen('availability','pdf','avail-days','avail-atm')"><i data-lucide="file-text"></i> PDF</button>
        <button class="btn btn-outline" onclick="gen('availability','csv','avail-days','avail-atm')"><i data-lucide="file"></i> CSV</button>
        <div class="loading-wrapper" id="lw-availability">
          <div class="loading-bar"></div><div class="loading-text">Generating...</div>
        </div>
      </div>
    </div>

  </div>
</main>

<footer class="footer">
  Dashen Bank &middot; ATM Report Portal &middot; <a href="https://dashenbanksc.com/frequently-asked-questions/#:~:text=Our%20Customer%20Service%20Team%20is,via%206333%20for%20any%20support." target="_blank">Support</a>
</footer>

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
    document.getElementById('kpi-atm-count').textContent = d.atm_count;
    document.getElementById('kpi-time').textContent =
      new Date().toLocaleTimeString('en-ET',{hour12:false});
  } catch(e){}
}

function populateATMSelect(selectId, atms) {
  const sel = document.getElementById(selectId);
  while (sel.options.length > 1) sel.remove(1);
  atms.forEach(a => {
    sel.add(new Option(a.id + ' \u2014 ' + a.branch, a.id));
  });
  const container = sel.closest('.custom-select');
  container.querySelector('.select-selected')?.remove();
  container.querySelector('.select-items')?.remove();
  const a = document.createElement('DIV');
  a.setAttribute('class', 'select-selected');
  a.innerHTML = sel.options[0].innerHTML;
  container.appendChild(a);
  const b = document.createElement('DIV');
  b.setAttribute('class', 'select-items select-hide');
  for (let j = 0; j < sel.length; j++) {
    const c = document.createElement('DIV');
    c.innerHTML = sel.options[j].innerHTML;
    c.setAttribute('data-value', sel.options[j].value);
    if (j === 0) c.setAttribute('class', 'same-as-selected');
    c.addEventListener('click', function(e) {
      var y, i, k, s, h, sl, yl;
      s = this.parentNode.parentNode.getElementsByTagName('select')[0];
      sl = s.length;
      h = this.parentNode.previousSibling;
      for (i = 0; i < sl; i++) {
        if (s.options[i].innerHTML == this.innerHTML) {
          s.selectedIndex = i;
          h.innerHTML = this.innerHTML;
          y = this.parentNode.getElementsByClassName('same-as-selected');
          yl = y.length;
          for (k = 0; k < yl; k++) y[k].removeAttribute('class');
          this.setAttribute('class', 'same-as-selected');
          s.dispatchEvent(new Event('change'));
          break;
        }
      }
      h.click();
    });
    b.appendChild(c);
  }
  container.appendChild(b);
  a.addEventListener('click', function(e) {
    e.stopPropagation();
    closeAllSelect(this);
    this.nextSibling.classList.toggle('select-hide');
    this.classList.toggle('select-arrow-active');
  });
}

async function loadATMs() {
  try {
    const atms = await fetch('/api/atms').then(r=>r.json());
    populateATMSelect('txn-atm', atms);
    populateATMSelect('avail-atm', atms);
    document.getElementById('kpi-atm-count').textContent = atms.length;
  } catch(e){}
}

function gen(type, fmt, daysId, atmId) {
  const days = document.getElementById(daysId).value;
  const atm  = atmId ? document.getElementById(atmId).value : 'all';
  const lw = document.getElementById('lw-'+type);
  const ts  = Date.now();
  const url = `/report/${type}/${fmt}?days=${days}&atm=${atm}&_t=${ts}`;
  lw.classList.add('active');

  // Use a hidden iframe for reliable download with correct query params
  const iframe = document.createElement('iframe');
  iframe.style.display = 'none';
  iframe.src = url;
  document.body.appendChild(iframe);

  setTimeout(() => {
    lw.classList.remove('active');
    if (iframe.parentNode) iframe.parentNode.removeChild(iframe);
  }, 4000);
}

loadKPIs();
loadATMs();
setInterval(loadKPIs, 30000);
</script>
</body>
</html>
"""

ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ATM Admin — Dashen Bank</title>
<link rel="icon" type="image/png" href="/static/logo.png">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/flatpickr/dist/flatpickr.min.css">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',sans-serif;background:#F0F2F6;color:#0F172A;font-size:13px;line-height:1.5;padding:32px}
h1{color:#012169;font-size:22px;font-weight:800;letter-spacing:-0.3px;margin-bottom:4px}
p.sub{color:#64748B;font-size:13px;margin-bottom:24px}
.card{background:#fff;border-radius:12px;box-shadow:0 1px 3px rgba(1,33,105,.04);border:1px solid #EEF1F6;margin-bottom:24px}
.card-header{display:flex;align-items:center;justify-content:space-between;padding:18px 24px;background:#FAFBFC;border-bottom:1px solid #EEF1F6;overflow:hidden;border-radius:12px 12px 0 0}
.card-header h2{font-size:15px;font-weight:700}
.btn{display:inline-flex;align-items:center;gap:6px;padding:9px 18px;border-radius:6px;border:none;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit;text-decoration:none;transition:.2s}
.btn-primary{background:#012169;color:#fff}
.btn-primary:hover{background:#273274;box-shadow:0 4px 16px rgba(1,33,105,.25)}
.btn-outline{background:#fff;color:#475569;border:1px solid #E4E9F1}
.btn-outline:hover{border-color:#012169;color:#012169}
table{width:100%;border-collapse:collapse;font-size:12px}
th{background:#012169;color:#fff;font-weight:600;padding:10px 12px;text-align:left;white-space:nowrap}
td{padding:8px 12px;border-bottom:1px solid #EEF1F6}
tr:nth-child(even){background:#F8FAFC}
tr:hover{background:#EEF2FF}
.actions{display:flex;gap:6px}
.actions a{padding:5px 10px;border-radius:4px;font-size:11px;text-decoration:none;font-weight:500}
.actions .edit{background:rgba(1,33,105,.06);color:#012169}
.actions .edit:hover{background:rgba(1,33,105,.12)}
.badge{padding:2px 8px;border-radius:100px;font-size:10px;font-weight:600}
.badge.active{background:rgba(5,150,105,.1);color:#059669}
.badge.inactive{background:rgba(220,38,38,.08);color:#DC2626}
.empty{text-align:center;padding:48px;color:#94A3B8;font-size:14px}
.form-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px;padding:24px}
.form-group{display:flex;flex-direction:column;gap:4px}
.form-group label{font-size:10px;font-weight:600;color:#64748B;text-transform:uppercase;letter-spacing:.6px}
.form-group input,.form-group select{padding:8px 10px;border:1px solid #E4E9F1;border-radius:6px;font-size:12px;font-family:inherit;outline:none;transition:border-color .2s}
.form-group input:focus,.form-group select:focus{border-color:#012169;box-shadow:0 0 0 3px rgba(1,33,105,.06)}
.form-actions{padding:0 24px 24px;display:flex;gap:10px}
.form-actions .btn{padding:10px 20px}
.cust-select{position:relative;font-family:inherit}
.cust-select select{display:none}
.cust-trigger{background:#fff;border:1px solid #E4E9F1;border-radius:6px;padding:8px 28px 8px 10px;font-size:12px;color:#0F172A;cursor:pointer;user-select:none;display:flex;align-items:center;justify-content:space-between;transition:border-color .2s,box-shadow .2s;position:relative}
.cust-trigger:hover{border-color:#CBD5E1}
.cust-trigger:after{content:'';position:absolute;right:10px;top:50%;margin-top:-2px;width:0;height:0;border-left:4px solid transparent;border-right:4px solid transparent;border-top:5px solid #94A3B8;transition:transform .2s}
.cust-trigger.open{box-shadow:0 0 0 3px rgba(1,33,105,.06);border-color:#012169}
.cust-trigger.open:after{transform:rotate(180deg);margin-top:-7px}
.cust-drop{position:absolute;top:100%;left:0;right:0;z-index:999;background:#fff;border:1px solid #E4E9F1;border-radius:6px;box-shadow:0 8px 32px rgba(1,33,105,.1);margin-top:4px;max-height:200px;overflow-y:auto;display:none}
.cust-drop.show{display:block}
.cust-drop.up{top:auto;bottom:100%;margin-top:0;margin-bottom:4px}
.cust-trigger.up:after{transform:rotateX(180deg);margin-top:-7px}
.cust-trigger.open.up:after{transform:rotateX(0);margin-top:-2px}
.cust-opt{padding:8px 10px;font-size:12px;color:#0F172A;cursor:pointer;transition:background .1s}
.cust-opt:hover{background:#EEF2FF;color:#012169}
.cust-opt.sel{background:#F0F2F6;color:#012169;font-weight:600}
.flatpickr-calendar{background:#fff;border:1px solid #E4E9F1;box-shadow:0 8px 32px rgba(1,33,105,.1);border-radius:8px;font-family:'Inter',sans-serif;margin-top:4px}
.flatpickr-calendar.arrowTop:before,.flatpickr-calendar.arrowTop:after{display:none}
.flatpickr-months .flatpickr-month{background:#fff;color:#0F172A;height:48px}
.flatpickr-current-month .flatpickr-monthDropdown-months{font-weight:600;font-size:13px}
.flatpickr-current-month input.cur-year{font-weight:600;font-size:13px}
.flatpickr-months .flatpickr-prev-month,.flatpickr-months .flatpickr-next-month{color:#94A3B8;fill:#94A3B8}
.flatpickr-months .flatpickr-prev-month:hover,.flatpickr-months .flatpickr-next-month:hover{color:#012169}
.flatpickr-weekday{color:#64748B;font-weight:600;font-size:11px}
.flatpickr-day{color:#0F172A;font-size:12px;border-radius:4px;line-height:34px}
.flatpickr-day:hover{background:#EEF2FF;border-color:#EEF2FF}
.flatpickr-day.selected,.flatpickr-day.selected:hover{background:#012169;border-color:#012169;color:#fff}
.flatpickr-day.today{border-color:#012169}
.flatpickr-day.today:hover{border-color:#012169;background:#EEF2FF}
.flatpickr-day.inRange,.flatpickr-day.prevMonthDay.inRange,.flatpickr-day.nextMonthDay.inRange,.flatpickr-day.today.inRange,.flatpickr-day.prevMonthDay.today.inRange,.flatpickr-day.nextMonthDay.today.inRange{background:#EEF2FF;border-color:#EEF2FF;color:#012169}
.flatpickr-day.startRange,.flatpickr-day.endRange,.flatpickr-day.startRange:hover,.flatpickr-day.endRange:hover{background:#012169;border-color:#012169;color:#fff}
.flatpickr-day.flatpickr-disabled,.flatpickr-day.flatpickr-disabled:hover{color:#CBD5E1}
.flatpickr-time input{font-size:12px}
</style>
</head>
<body>
<div style="margin-bottom:16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap">
  <a href="/" style="background:#F0F2F6;padding:8px 16px;border-radius:6px;color:#012169;text-decoration:none;font-weight:500;font-size:13px">&larr; Report Portal</a>
  <span style="color:#94A3B8;font-size:13px">|</span>
  <span style="color:#475569;font-size:13px;font-weight:600">ATM Registration Admin</span>
</div>
<h1>ATM Registration</h1>
<p class="sub">Manage ATM location data used by monitoring dashboards and reports.</p>
<div class="card">
  <div class="card-header"><h2>{{ADMIN_FORM_TITLE}}</h2></div>
  {{ADMIN_FORM_EDIT_NOTE}}
  <form method="post" action="/admin/atm/save">
    <div class="form-grid">{{ADMIN_FORM_FIELDS}}</div>
    <div class="form-actions">
      <button type="submit" class="btn btn-primary"><i data-lucide="save" style="width:14px;height:14px"></i> Save ATM</button>
      <a href="/admin/atm" class="btn btn-outline">Cancel</a>
    </div>
  </form>
</div>
<div class="card">
  <div class="card-header">
    <h2>Registered ATMs ({{ADMIN_ATM_COUNT}})</h2>
    <a href="/admin/atm" class="btn btn-primary">+ Add New ATM</a>
  </div>
  <div style="overflow-x:auto">
    <table>
      <thead><tr>
        <th>ATM ID</th><th>Branch</th><th>District</th><th>City</th><th>Region</th>
        <th>Latitude</th><th>Longitude</th><th>Terminal ID</th><th>Vendor</th><th>Model</th>
        <th>Install Date</th><th>Status</th><th></th>
      </tr></thead>
      <tbody>{{ADMIN_TABLE_ROWS}}</tbody>
    </table>
  </div>
</div>
<script src="https://unpkg.com/lucide@latest/dist/umd/lucide.js"></script>
<script src="https://cdn.jsdelivr.net/npm/flatpickr"></script>
<script>
lucide.createIcons()
function initCustomSelects(){document.querySelectorAll('select').forEach(function(sel){if(sel.closest('.cust-select'))return;var wrap=document.createElement('div');wrap.className='cust-select';sel.parentNode.insertBefore(wrap,sel);wrap.appendChild(sel);var trig=document.createElement('div');trig.className='cust-trigger';trig.textContent=sel.options[sel.selectedIndex].text;wrap.appendChild(trig);var drop=document.createElement('div');drop.className='cust-drop';wrap.appendChild(drop);for(var i=0;i<sel.length;i++){(function(idx){var o=document.createElement('div');o.className='cust-opt'+(idx===sel.selectedIndex?' sel':'');o.textContent=sel.options[idx].text;o.addEventListener('click',function(){sel.selectedIndex=idx;trig.textContent=sel.options[idx].text;drop.querySelectorAll('.cust-opt').forEach(function(c){c.classList.remove('sel')});o.classList.add('sel');trig.classList.remove('open');drop.classList.remove('show');sel.dispatchEvent(new Event('change'))});drop.appendChild(o)})(i)}trig.addEventListener('click',function(e){e.stopPropagation();var already=drop.classList.contains('show');document.querySelectorAll('.cust-drop.show').forEach(function(d){d.classList.remove('show')});document.querySelectorAll('.cust-trigger.open').forEach(function(t){t.classList.remove('open')});if(!already){var rect=trig.getBoundingClientRect();if(window.innerHeight-rect.bottom<180){drop.classList.add('up');trig.classList.add('up')}else{drop.classList.remove('up');trig.classList.remove('up')}drop.classList.add('show');trig.classList.add('open')}})});document.addEventListener('click',function(){document.querySelectorAll('.cust-drop.show').forEach(function(d){d.classList.remove('show')});document.querySelectorAll('.cust-trigger.open').forEach(function(t){t.classList.remove('open')})})}
document.querySelectorAll('.flatpickr-date').forEach(el => flatpickr(el, {
  dateFormat: 'Y-m-d',
  allowInput: true,
  disableMobile: true,
  monthSelectorType: 'static',
}))
initCustomSelects()
</script>
</body>
</html>"""

EJ_SEARCH_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EJ Search — Dashen Bank</title>
<link rel="icon" type="image/png" href="/static/logo.png">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/flatpickr/dist/flatpickr.min.css">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',sans-serif;background:#F0F2F6;color:#0F172A;font-size:13px;line-height:1.5;padding:32px}
h1{color:#012169;font-size:22px;font-weight:800;letter-spacing:-0.3px;margin-bottom:4px}
p.sub{color:#64748B;font-size:13px;margin-bottom:24px}
.card{background:#fff;border-radius:12px;box-shadow:0 1px 3px rgba(1,33,105,.04);border:1px solid #EEF1F6;margin-bottom:24px}
.card-header{display:flex;align-items:center;justify-content:space-between;padding:18px 24px;background:#FAFBFC;border-bottom:1px solid #EEF1F6;overflow:hidden;border-radius:12px 12px 0 0}
.card-header h2{font-size:15px;font-weight:700}
.btn{display:inline-flex;align-items:center;gap:6px;padding:9px 18px;border-radius:6px;border:none;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit;text-decoration:none;transition:.2s}
.btn-primary{background:#012169;color:#fff}
.btn-primary:hover{background:#273274;box-shadow:0 4px 16px rgba(1,33,105,.25)}
.btn-outline{background:#fff;color:#475569;border:1px solid #E4E9F1}
.btn-outline:hover{border-color:#012169;color:#012169}
.btn-gold{background:#FDD79A;color:#012169}
.btn-gold:hover{background:#C49A00}
.search-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:14px;padding:24px}
.form-group{display:flex;flex-direction:column;gap:4px}
.form-group label{font-size:10px;font-weight:600;color:#64748B;text-transform:uppercase;letter-spacing:.6px}
.form-group input,.form-group select{padding:8px 10px;border:1px solid #E4E9F1;border-radius:6px;font-size:12px;font-family:inherit;outline:none;transition:border-color .2s}
.form-group input:focus,.form-group select:focus{border-color:#012169;box-shadow:0 0 0 3px rgba(1,33,105,.06)}
.form-group select{display:none}
.cust-select{position:relative;font-family:inherit;grid-column:unset}
.cust-trigger{background:#fff;border:1px solid #E4E9F1;border-radius:6px;padding:8px 28px 8px 10px;font-size:12px;color:#0F172A;cursor:pointer;user-select:none;display:flex;align-items:center;justify-content:space-between;transition:border-color .2s,box-shadow .2s;position:relative}
.cust-trigger:hover{border-color:#CBD5E1}
.cust-trigger:after{content:'';position:absolute;right:10px;top:50%;margin-top:-2px;width:0;height:0;border-left:4px solid transparent;border-right:4px solid transparent;border-top:5px solid #94A3B8;transition:transform .2s}
.cust-trigger.open{box-shadow:0 0 0 3px rgba(1,33,105,.06);border-color:#012169}
.cust-trigger.open:after{transform:rotate(180deg);margin-top:-7px}
.cust-drop{position:absolute;top:100%;left:0;right:0;z-index:999;background:#fff;border:1px solid #E4E9F1;border-radius:6px;box-shadow:0 8px 32px rgba(1,33,105,.1);margin-top:4px;max-height:200px;overflow-y:auto;display:none}
.cust-drop.show{display:block}
.cust-drop.up{top:auto;bottom:100%;margin-top:0;margin-bottom:4px}
.cust-trigger.up:after{transform:rotateX(180deg);margin-top:-7px}
.cust-trigger.open.up:after{transform:rotateX(0);margin-top:-2px}
.cust-opt{padding:8px 10px;font-size:12px;color:#0F172A;cursor:pointer;transition:background .1s}
.cust-opt:hover{background:#EEF2FF;color:#012169}
.cust-opt.sel{background:#F0F2F6;color:#012169;font-weight:600}
.search-actions{padding:0 24px 24px;display:flex;gap:10px}
.search-actions .btn{padding:10px 20px}
table{width:100%;border-collapse:collapse;font-size:12px}
th{background:#012169;color:#fff;font-weight:600;padding:10px 12px;text-align:left;white-space:nowrap}
td{padding:8px 12px;border-bottom:1px solid #EEF1F6;font-variant-numeric:tabular-nums}
tr:nth-child(even){background:#F8FAFC}
tr:hover{background:#EEF2FF}
.empty{text-align:center;padding:48px;color:#94A3B8;font-size:14px}
.badge{padding:2px 8px;border-radius:100px;font-size:10px;font-weight:600}
.badge.APPROVED{background:rgba(5,150,105,.1);color:#059669}
.badge.DECLINED{background:rgba(220,38,38,.08);color:#DC2626}
.badge.TIMEOUT,.badge.ERROR{background:rgba(217,119,6,.1);color:#D97706}
.result-count{font-size:12px;color:#64748B;padding:4px 24px 16px}
.flatpickr-calendar{background:#fff;border:1px solid #E4E9F1;box-shadow:0 8px 32px rgba(1,33,105,.1);border-radius:8px;font-family:'Inter',sans-serif;margin-top:4px}
.flatpickr-calendar.arrowTop:before,.flatpickr-calendar.arrowTop:after{display:none}
.flatpickr-months .flatpickr-month{background:#fff;color:#0F172A;height:48px}
.flatpickr-current-month .flatpickr-monthDropdown-months{font-weight:600;font-size:13px}
.flatpickr-current-month input.cur-year{font-weight:600;font-size:13px}
.flatpickr-months .flatpickr-prev-month,.flatpickr-months .flatpickr-next-month{color:#94A3B8;fill:#94A3B8}
.flatpickr-months .flatpickr-prev-month:hover,.flatpickr-months .flatpickr-next-month:hover{color:#012169}
.flatpickr-weekday{color:#64748B;font-weight:600;font-size:11px}
.flatpickr-day{color:#0F172A;font-size:12px;border-radius:4px;line-height:34px}
.flatpickr-day:hover{background:#EEF2FF;border-color:#EEF2FF}
.flatpickr-day.selected,.flatpickr-day.selected:hover{background:#012169;border-color:#012169;color:#fff}
.flatpickr-day.today{border-color:#012169}
.flatpickr-day.today:hover{border-color:#012169;background:#EEF2FF}
.flatpickr-day.inRange,.flatpickr-day.prevMonthDay.inRange,.flatpickr-day.nextMonthDay.inRange,.flatpickr-day.today.inRange,.flatpickr-day.prevMonthDay.today.inRange,.flatpickr-day.nextMonthDay.today.inRange{background:#EEF2FF;border-color:#EEF2FF;color:#012169}
.flatpickr-day.startRange,.flatpickr-day.endRange,.flatpickr-day.startRange:hover,.flatpickr-day.endRange:hover{background:#012169;border-color:#012169;color:#fff}
.flatpickr-day.flatpickr-disabled,.flatpickr-day.flatpickr-disabled:hover{color:#CBD5E1}
.flatpickr-time input{font-size:12px}
</style>
</head>
<body>
<div style="margin-bottom:16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap">
  <a href="/" style="background:#F0F2F6;padding:8px 16px;border-radius:6px;color:#012169;text-decoration:none;font-weight:500;font-size:13px">&larr; Report Portal</a>
  <span style="color:#94A3B8;font-size:13px">|</span>
  <span style="color:#475569;font-size:13px;font-weight:600">Electronic Journal Search</span>
</div>
<h1>EJ Search</h1>
<p class="sub">Search the electronic journal for dispute investigation and transaction lookup.</p>
<form method="post" action="/ej-search">
<div class="card">
  <div class="card-header"><h2>Search Criteria</h2></div>
    <div class="search-grid">
      <div class="form-group">
        <label>Card (Last 4 Digits)</label>
        <input type="text" name="card" placeholder="e.g. 1234" maxlength="4" pattern="[0-9]{4}" value="{{EJ_CARD}}">
      </div>
      <div class="form-group">
        <label>Date From</label>
        <input type="text" name="date_from" class="flatpickr-date" placeholder="YYYY-MM-DD" value="{{EJ_DATE_FROM}}">
      </div>
      <div class="form-group">
        <label>Date To</label>
        <input type="text" name="date_to" class="flatpickr-date" placeholder="YYYY-MM-DD" value="{{EJ_DATE_TO}}">
      </div>
      <div class="form-group">
        <label>ATM</label>
        <select name="atm_id">{{EJ_ATM_OPTIONS}}</select>
      </div>
    </div>
    <div class="search-actions">
      <button type="submit" class="btn btn-primary"><i data-lucide="search" style="width:14px;height:14px"></i> Search</button>
      <a href="/ej-search" class="btn btn-outline">Clear</a>
    </div>
</div>
{{EJ_RESULTS_SECTION}}
</form>
<script src="https://cdn.jsdelivr.net/npm/flatpickr"></script>
<script>
function initCustomSelects(){document.querySelectorAll('select').forEach(function(sel){if(sel.closest('.cust-select'))return;var wrap=document.createElement('div');wrap.className='cust-select';sel.parentNode.insertBefore(wrap,sel);wrap.appendChild(sel);var trig=document.createElement('div');trig.className='cust-trigger';trig.textContent=sel.options[sel.selectedIndex].text;wrap.appendChild(trig);var drop=document.createElement('div');drop.className='cust-drop';wrap.appendChild(drop);for(var i=0;i<sel.length;i++){(function(idx){var o=document.createElement('div');o.className='cust-opt'+(idx===sel.selectedIndex?' sel':'');o.textContent=sel.options[idx].text;o.addEventListener('click',function(){sel.selectedIndex=idx;trig.textContent=sel.options[idx].text;drop.querySelectorAll('.cust-opt').forEach(function(c){c.classList.remove('sel')});o.classList.add('sel');trig.classList.remove('open');drop.classList.remove('show');sel.dispatchEvent(new Event('change'))});drop.appendChild(o)})(i)}trig.addEventListener('click',function(e){e.stopPropagation();var already=drop.classList.contains('show');document.querySelectorAll('.cust-drop.show').forEach(function(d){d.classList.remove('show')});document.querySelectorAll('.cust-trigger.open').forEach(function(t){t.classList.remove('open')});if(!already){var rect=trig.getBoundingClientRect();if(window.innerHeight-rect.bottom<180){drop.classList.add('up');trig.classList.add('up')}else{drop.classList.remove('up');trig.classList.remove('up')}drop.classList.add('show');trig.classList.add('open')}})});document.addEventListener('click',function(){document.querySelectorAll('.cust-drop.show').forEach(function(d){d.classList.remove('show')});document.querySelectorAll('.cust-trigger.open').forEach(function(t){t.classList.remove('open')})})}
document.querySelectorAll('.flatpickr-date').forEach(el => flatpickr(el, {
  dateFormat: 'Y-m-d',
  allowInput: true,
  disableMobile: true,
  monthSelectorType: 'static',
}))
initCustomSelects()
</script>
</body>
</html>"""
