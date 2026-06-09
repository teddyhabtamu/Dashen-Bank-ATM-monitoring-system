#!/bin/sh
set -e

DARK_CSS=$(find /usr/share/grafana/public/build -name "grafana.dark.*.css" | head -1)
LIGHT_CSS=$(find /usr/share/grafana/public/build -name "grafana.light.*.css" | head -1)

DASHEN_CSS='

/* ============================================================
   DASHEN BANK OVERRIDES v7 — Nuclear Visibility Fix
   ============================================================ */

/* ---- GLOBAL BACKGROUND ---- */
html, body {
  background-color: #012169 !important;
  background-image: none !important;
}

/* ============================================================
   NUCLEAR OPTION — force ALL text/icons white everywhere in
   the top nav and breadcrumb bar, regardless of class name.
   Grafana 10 nav structure: two horizontal bars at the top.
   ============================================================ */

/* Every element inside the top nav bar */
header *,
[data-testid="navigation-bar"] *,
[data-testid="data-testid Nav bar"] * {
  color: #ffffff !important;
  fill: #ffffff !important;
}

/* Every SVG path inside header */
header svg path,
header svg rect,
header svg circle,
header svg polygon,
[data-testid="navigation-bar"] svg path,
[data-testid="navigation-bar"] svg rect,
[data-testid="data-testid Nav bar"] svg path,
[data-testid="data-testid Nav bar"] svg rect {
  fill: #ffffff !important;
  color: #ffffff !important;
}

/* Every button inside header */
header button,
[data-testid="navigation-bar"] button,
[data-testid="data-testid Nav bar"] button {
  background-color: rgba(255,255,255,0.08) !important;
  border: 1px solid rgba(253,215,154,0.2) !important;
  color: #ffffff !important;
}

header button:hover,
[data-testid="navigation-bar"] button:hover,
[data-testid="data-testid Nav bar"] button:hover {
  background-color: rgba(253,215,154,0.18) !important;
  border-color: rgba(253,215,154,0.6) !important;
}

header button:hover *,
[data-testid="navigation-bar"] button:hover *,
[data-testid="data-testid Nav bar"] button:hover * {
  color: #FDD79A !important;
  fill: #FDD79A !important;
}

/* ---- BREADCRUMB BAR (second bar with Home > Dashboards > ...) ---- */
/* This bar uses [class*="page-toolbar"] or similar */
[class*="page-toolbar"] *,
[class*="pageToolbar"] *,
[class*="toolbar"] > * {
  color: #ffffff !important;
  fill: #ffffff !important;
}

[class*="page-toolbar"] svg path,
[class*="pageToolbar"] svg path {
  fill: #ffffff !important;
}

[class*="page-toolbar"] button,
[class*="pageToolbar"] button {
  background-color: rgba(255,255,255,0.07) !important;
  color: #ffffff !important;
  border: 1px solid rgba(253,215,154,0.15) !important;
}

[class*="page-toolbar"] button:hover,
[class*="pageToolbar"] button:hover {
  background-color: rgba(253,215,154,0.15) !important;
  border-color: rgba(253,215,154,0.5) !important;
}

[class*="page-toolbar"] button:hover *,
[class*="pageToolbar"] button:hover * {
  color: #FDD79A !important;
  fill: #FDD79A !important;
}

/* Breadcrumb links and separators */
[class*="page-toolbar"] a,
[class*="pageToolbar"] a,
[class*="breadcrumb"] a,
[class*="Breadcrumb"] a {
  color: rgba(255,255,255,0.75) !important;
}

[class*="page-toolbar"] a:hover,
[class*="breadcrumb"] a:hover {
  color: #FDD79A !important;
}

/* ---- MENU HAMBURGER / TOGGLE BUTTON ---- */
/* The leftmost icon next to "Home" */
button[aria-label="Toggle menu"],
button[aria-label="Open menu"],
button[aria-label="Close menu"],
button[aria-label="Expand menu"],
nav > button:first-child,
header > div > button:first-child {
  background-color: rgba(255,255,255,0.08) !important;
  border: 1px solid rgba(253,215,154,0.2) !important;
}

button[aria-label="Toggle menu"] *,
button[aria-label="Open menu"] *,
button[aria-label="Close menu"] * {
  color: #ffffff !important;
  fill: #ffffff !important;
}

/* ---- HEADER BAR BACKGROUNDS ---- */
header,
[data-testid="navigation-bar"],
[data-testid="data-testid Nav bar"],
nav[aria-label="Navigation bar"],
[class*="navbar"],
[class*="Navbar"] {
  background-color: #012169 !important;
  border-bottom: 2px solid rgba(253,215,154,0.35) !important;
}

[class*="page-toolbar"],
.dashboard-toolbar,
[data-testid="data-testid Dashboard toolbar"] {
  background-color: #011a52 !important;
  border-bottom: 1px solid rgba(253,215,154,0.2) !important;
}

/* ---- LOGO ---- */
header a[href="/"] img,
header a[href="/"] svg,
[data-testid="navigation-bar"] a[href="/"] img,
[data-testid="navigation-bar"] a[href="/"] svg,
a[aria-label="Go to home"] img,
a[aria-label="Go to home"] svg,
a[aria-label="Home"] img,
a[aria-label="Home"] svg,
a[data-testid="nav-logo"] svg,
a[data-testid="nav-logo"] img,
a[class*="logo"] svg {
  display: none !important;
}

header a[href="/"]::after,
[data-testid="navigation-bar"] a[href="/"]::after,
a[aria-label="Go to home"]::after,
a[aria-label="Home"]::after,
a[data-testid="nav-logo"]::after,
a[class*="logo"]::after {
  content: "" !important;
  display: inline-block !important;
  width: 90px !important;
  height: 40px !important;
  background-image: url("/public/img/dashen-logo.png") !important;
  background-repeat: no-repeat !important;
  background-position: center left !important;
  background-size: contain !important;
  vertical-align: middle !important;
}

/* ---- SEARCH BAR ---- */
header [class*="input-wrapper"],
header [class*="inputWrapper"],
[data-testid="data-testid Nav bar"] [class*="input-wrapper"] {
  background-color: rgba(255,255,255,0.1) !important;
  border: 1px solid rgba(253,215,154,0.25) !important;
  border-radius: 6px !important;
}

header [class*="input-wrapper"] button,
header [class*="inputWrapper"] button {
  background-color: transparent !important;
  border: none !important;
}

/* ctrl+k badge */
header [class*="input-suffix"] span,
header [class*="inputSuffix"] span,
[data-testid="data-testid Nav bar"] [class*="input-suffix"] span {
  color: rgba(255,255,255,0.5) !important;
  background-color: rgba(255,255,255,0.1) !important;
  border: 1px solid rgba(255,255,255,0.2) !important;
  border-radius: 3px !important;
  padding: 1px 4px !important;
  font-size: 10px !important;
}

/* ============================================================
   LOGIN PAGE
   ============================================================ */
div:has(.login-content-box),
div:has(.login-content-box)::before {
  background-image: none !important;
  background-color: #012169 !important;
}

.login-content-box {
  background-color: #ffffff !important;
  border-radius: 16px !important;
  border-top: 6px solid #FDD79A !important;
  box-shadow: 0 24px 64px rgba(0,0,0,0.65) !important;
  padding: 16px 40px 40px !important;
  position: relative !important;
}

.login-content-box img[src*="grafana_icon"],
.login-content-box img[alt="Grafana"],
.login-content-box svg { display: none !important; }
.login-content-box h1 { display: none !important; }

.login-content-box::before {
  content: "" !important;
  display: block !important;
  width: 180px !important;
  height: 100px !important;
  margin: 0 auto 2px !important;
  background-image: url("/public/img/dashen-logo.png") !important;
  background-repeat: no-repeat !important;
  background-position: center !important;
  background-size: contain !important;
}

.login-content-box::after {
  content: "IT Monitoring Portal" !important;
  display: block !important;
  text-align: center !important;
  font-size: 10px !important;
  font-weight: 700 !important;
  color: #9ca3af !important;
  letter-spacing: 3px !important;
  text-transform: uppercase !important;
  margin: 0 0 20px !important;
}

.login-content-box label,
.login-content-box [class*="label"] {
  color: #374151 !important;
  font-weight: 500 !important;
  background-color: transparent !important;
}

.login-content-box > div,
.login-content-box > div > div,
.login-content-box [class*="input-wrapper"],
.login-content-box [class*="inputWrapper"] {
  background-color: transparent !important;
}

.login-content-box input {
  background-color: #f9fafb !important;
  color: #1e3a5f !important;
  border: 1.5px solid #d1d5db !important;
  border-radius: 8px !important;
  box-shadow: none !important;
}

.login-content-box input::placeholder { color: #9ca3af !important; }
.login-content-box input:focus {
  border-color: #012169 !important;
  box-shadow: 0 0 0 3px rgba(1,33,105,0.15) !important;
  outline: none !important;
}

[data-testid="login-submit-button"],
.login-content-box button[type="submit"] {
  background-color: #012169 !important;
  color: #FDD79A !important;
  font-weight: 700 !important;
  border: none !important;
  border-radius: 8px !important;
  font-size: 15px !important;
}

[data-testid="login-submit-button"]:hover,
.login-content-box button[type="submit"]:hover {
  background-color: #001647 !important;
  color: #ffffff !important;
}

.login-content-box a { color: #012169 !important; font-weight: 500 !important; }

/* ============================================================
   LEFT SIDE NAVIGATION
   ============================================================ */
nav[aria-label="Main navigation"],
[data-testid="sidemenu"],
[class*="sidemenu"],
[class*="sideMenu"],
aside {
  background-color: #011a52 !important;
  border-right: 1px solid rgba(253,215,154,0.15) !important;
}

[data-testid="sidemenu"] *,
[class*="sidemenu"] * {
  color: rgba(255,255,255,0.85) !important;
  fill: rgba(255,255,255,0.75) !important;
}

[data-testid="sidemenu"] svg path,
[class*="sidemenu"] svg path {
  fill: rgba(255,255,255,0.75) !important;
}

[data-testid="sidemenu"] a:hover *,
[class*="sidemenu"] a:hover * {
  color: #FDD79A !important;
  fill: #FDD79A !important;
}

[data-testid="sidemenu"] a:hover svg path,
[class*="sidemenu"] a:hover svg path {
  fill: #FDD79A !important;
}

[data-testid="sidemenu"] a[aria-current="page"],
[class*="sidemenu"] a[aria-current="page"] {
  background-color: rgba(253,215,154,0.12) !important;
  border-left: 3px solid #FDD79A !important;
}

[data-testid="sidemenu"] a[aria-current="page"] *,
[class*="sidemenu"] a[aria-current="page"] * {
  color: #FDD79A !important;
  fill: #FDD79A !important;
}

/* Sidemenu logo */
[data-testid="sidemenu"] a[aria-label="Go to home"] svg,
[data-testid="sidemenu"] a[href="/"] svg,
[class*="sidemenu"] a[href="/"] svg { display: none !important; }

[data-testid="sidemenu"] a[aria-label="Go to home"]::after,
[data-testid="sidemenu"] a[href="/"]::after,
[class*="sidemenu"] a[href="/"]::after {
  content: "" !important;
  display: block !important;
  width: 36px !important;
  height: 36px !important;
  background-image: url("/public/img/dashen-logo.png") !important;
  background-repeat: no-repeat !important;
  background-position: center !important;
  background-size: contain !important;
}

[data-testid="sidemenu"] [class*="footer"],
[class*="sidemenu-footer"],
[class*="sideMenuFooter"] {
  border-top: 1px solid rgba(253,215,154,0.15) !important;
  background-color: #011a52 !important;
}

/* ============================================================
   SCROLLBAR
   ============================================================ */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: #011a52; }
::-webkit-scrollbar-thumb { background: rgba(253,215,154,0.4); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: rgba(253,215,154,0.7); }
'

if [ -n "$DARK_CSS" ]; then
  sed -i '/DASHEN BANK OVERRIDES/,$ d' "$DARK_CSS" 2>/dev/null || true
  echo "$DASHEN_CSS" >> "$DARK_CSS"
  echo "Dashen: patched $DARK_CSS"
fi

if [ -n "$LIGHT_CSS" ]; then
  sed -i '/DASHEN BANK OVERRIDES/,$ d' "$LIGHT_CSS" 2>/dev/null || true
  echo "$DASHEN_CSS" >> "$LIGHT_CSS"
  echo "Dashen: patched $LIGHT_CSS"
fi

# Replace favicon
if [ -f "/usr/share/grafana/public/img/dashen-logo.png" ]; then
  cp /usr/share/grafana/public/img/dashen-logo.png \
     /usr/share/grafana/public/img/apple-touch-icon.png 2>/dev/null || true
fi

# Blank Grafana SVG icons
printf '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"></svg>' \
  > /usr/share/grafana/public/img/grafana_icon.svg
printf '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"></svg>' \
  > /usr/share/grafana/public/img/grafana_mask_icon.svg 2>/dev/null || true

# Blank login background
G8_LOGIN=$(find /usr/share/grafana/public/build -name "g8_login_dark*" 2>/dev/null | head -1)
if [ -n "$G8_LOGIN" ]; then
  printf '\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x8f\x01\x00\x00\x02\x00\x01\xe2!\xbc\x33\x00\x00\x00\x00IEND\xaeB`\x82' \
    > "$G8_LOGIN" 2>/dev/null || true
fi

# Patch index.html with critical CSS that loads before React
INDEX="/usr/share/grafana/public/views/index.html"
if [ -w "$INDEX" ] && ! grep -q "dashen-critical" "$INDEX"; then
  CRITICAL_CSS='<style id="dashen-critical">
html,body{background:#012169!important;background-image:none!important;}
header,
[data-testid="navigation-bar"],
[data-testid="data-testid Nav bar"]{background-color:#012169!important;border-bottom:2px solid rgba(253,215,154,0.35)!important;}
[data-testid="sidemenu"],nav[aria-label="Main navigation"]{background-color:#011a52!important;}
[data-testid="login-background"]{display:none!important;}
header *,[data-testid="navigation-bar"] *,[data-testid="data-testid Nav bar"] *{color:#ffffff!important;fill:#ffffff!important;}
header svg path,[data-testid="navigation-bar"] svg path,[data-testid="data-testid Nav bar"] svg path{fill:#ffffff!important;}
header button,[data-testid="navigation-bar"] button,[data-testid="data-testid Nav bar"] button{background-color:rgba(255,255,255,0.08)!important;border:1px solid rgba(253,215,154,0.2)!important;}
[class*="page-toolbar"] *{color:#ffffff!important;fill:#ffffff!important;}
[class*="page-toolbar"] svg path{fill:#ffffff!important;}
</style>'
  sed -i "s|</head>|${CRITICAL_CSS}</head>|" "$INDEX" 2>/dev/null || true
  echo "Dashen: patched index.html with critical CSS"
fi

echo "Dashen: branding v7 complete — starting Grafana..."
exec /run.sh