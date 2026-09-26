#!/bin/sh
# ============================================================
# Dashen Bank Grafana branding — STABLE SURFACE ONLY (v8)
#
# Policy: brand only what survives Grafana upgrades —
#   logo/favicon files, page <title> (index.html mount),
#   page background, login box. No deep nav/toolbar/button
#   selectors: those are internal markup and rot on upgrade.
#
# Grafana image is pinned (docker-compose.yml, 10.2.6).
# UPGRADE RULE: after ANY Grafana version bump, eyeball these
# three screenshots before merge: login page, left nav,
# one dashboard. Five minutes, catches all rot.
# ============================================================
set -e

DARK_CSS=$(find /usr/share/grafana/public/build -name "grafana.dark.*.css" | head -1)
LIGHT_CSS=$(find /usr/share/grafana/public/build -name "grafana.light.*.css" | head -1)

DASHEN_CSS='

/* ============================================================
   DASHEN BANK OVERRIDES v8 — stable surfaces only
   ============================================================ */

/* ---- Page background (generic element, version-proof) ---- */
html, body {
  background-color: #012169 !important;
  background-image: none !important;
}

/* ---- Login box (markup stable for years) ---- */
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

.login-content-box button[type="submit"] {
  background-color: #012169 !important;
  color: #FDD79A !important;
  font-weight: 700 !important;
  border: none !important;
  border-radius: 8px !important;
  font-size: 15px !important;
}

.login-content-box button[type="submit"]:hover {
  background-color: #001647 !important;
  color: #ffffff !important;
}

/* ---- Scrollbar (generic pseudo-elements, version-proof) ---- */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: #011a52; }
::-webkit-scrollbar-thumb { background: rgba(253,215,154,0.4); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: rgba(253,215,154,0.7); }

/* ---- TOP BAR (experimental, 2026-09-26: navy + logo only) ----
   Narrowest selectors that achieve it. If a Grafana upgrade
   breaks this block, delete from here to the marker and keep
   the rest — everything above stays valid. */
header,
[data-testid="navigation-bar"] {
  background-color: #012169 !important;
  border-bottom: 2px solid rgba(253,215,154,0.35) !important;
}

header *,
[data-testid="navigation-bar"] * {
  color: #ffffff !important;
}

header a[href="/"] img,
header a[href="/"] svg,
[data-testid="navigation-bar"] a[href="/"] img,
[data-testid="navigation-bar"] a[href="/"] svg {
  display: none !important;
}

header a[href="/"]::after,
[data-testid="navigation-bar"] a[href="/"]::after {
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
/* ---- END TOP BAR EXPERIMENT ---- */
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
# (background + login backdrop only — the version-proof minimum)
INDEX="/usr/share/grafana/public/views/index.html"
if [ -w "$INDEX" ] && ! grep -q "dashen-critical" "$INDEX"; then
  CRITICAL_CSS='<style id="dashen-critical">
html,body{background:#012169!important;background-image:none!important;}
</style>'
  sed -i "s|</head>|${CRITICAL_CSS}</head>|" "$INDEX" 2>/dev/null || true
  echo "Dashen: patched index.html with critical CSS"
fi

echo "Dashen: branding v8 complete — starting Grafana..."
exec /run.sh
