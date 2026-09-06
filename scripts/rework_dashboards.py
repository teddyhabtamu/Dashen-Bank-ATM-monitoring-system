#!/usr/bin/env python3
"""One-time brave rework of the Grafana boards (UAT review findings).

Run once from the repo root, review `git diff`, commit:
    python3 scripts/rework_dashboards.py

1. Transaction Monitoring banner: external brandfetch logo -> bundled
   public/img/dashen-logo.png (bank networks often block the CDN, which left
   a blank badge); purple palette -> Dashen navy/gold (mechanical color
   remap, layout/typography/JS untouched).
2. Management KPI: absolute June window -> relative now-7d/now; NetXMS
   leftover title fixed; "7-day availability trend" rewritten from
   transaction-success proxy to TRUE state-history availability.
3. All keeper boards: refresh 10s -> 30s (data polls at 30s), timezone pinned
   to Africa/Addis_Ababa for wall displays.
Does NOT delete the ATM-001 PoC board — `git rm` it separately by decision.
"""
import glob
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DASH_DIR = os.path.join(HERE, "..", "config", "grafana", "dashboards")

COLOR_MAP = {
    "#0f0a28": "#012169",
    "#1a0d44": "#012169",
    "#160a3d": "#001642",
    "#090613": "#001642",
    "#a855f7": "#FDD79A",
    "#c084fc": "#FDD79A",
    "#e0b0ff": "#FDD79A",
    "#e9d5ff": "#FFFFFF",
    "rgba(168,85,247,": "rgba(253,215,154,",
    "rgba(168, 85, 247,": "rgba(253, 215, 154,",
    "rgba(130, 80, 255,": "rgba(253, 215, 154,",
    "rgba(120, 60, 255,": "rgba(253, 215, 154,",
    "rgba(192, 132, 252,": "rgba(233, 169, 63,",
}
LOGO_URL = ("https://cdn.brandfetch.io/idNZ7JoLsG/w/150/h/149/theme/dark/"
            "logo.png?c=1dxbfHSJFAPEGdCLU4o5B")

AVAIL_SQL = """SELECT d.day AS "time",
  ROUND(100.0 * COALESCE(SUM(CASE WHEN s.state='IN_SERVICE'
    THEN EXTRACT(EPOCH FROM (LEAST(COALESCE(s.ended_at, NOW()), d.day + INTERVAL '1 day')
      - GREATEST(s.started_at, d.day))) ELSE 0 END), 0)
  / NULLIF((SELECT COUNT(*) FROM atm_locations WHERE status='active') * 86400, 0), 2)
  AS "Availability %"
FROM (SELECT generate_series(DATE_TRUNC('day', NOW() - INTERVAL '7 days'),
  DATE_TRUNC('day', NOW()), INTERVAL '1 day') AS day) d
LEFT JOIN atm_state_history s
  ON s.started_at < d.day + INTERVAL '1 day'
  AND (s.ended_at IS NULL OR s.ended_at > d.day)
GROUP BY d.day ORDER BY d.day"""


def rework(path):
    with open(path) as f:
        text = f.read()
    fname = os.path.basename(path)
    changed = []

    # 1+2. banner rebrand (Transaction Monitoring board only has the banner)
    if "cdn.brandfetch.io" in text:
        n = text.count("cdn.brandfetch.io")
        text = text.replace(
            "https://cdn.brandfetch.io/idNZ7JoLsG/w/150/h/149/theme/dark/logo.png?c=1dxbfHSJFAPEGdCLU4o5B",
            "public/img/dashen-logo.png")
        for old, new in COLOR_MAP.items():
            c = text.count(old)
            if c:
                text = text.replace(old, new)
                changed.append(f"banner {old}x{c}->{new}")
        changed.append(f"logo url x{n}->bundled")

    d = json.loads(text)  # validate after string surgery
    dirty = bool(changed)

    # 3. KPI fixes
    if "Management KPI" in fname:
        if d.get("time", {}).get("from") != "now-7d":
            d["time"] = {"from": "now-7d", "to": "now"}
            changed.append("time window -> now-7d/now")

        def walk(panels):
            for p in panels:
                if p.get("title") == "Hardware Faults (NetXMS Dashboard 4)":
                    p["title"] = "Hardware Faults"
                    changed.append("row title fixed")
                for t in p.get("targets", []):
                    q = t.get("rawSql", "")
                    if "DATE_TRUNC('day', recorded_at)" in q and "Success Rate %" in q:
                        t["rawSql"] = AVAIL_SQL
                        changed.append("availability trend -> state-history query")
                if "panels" in p:
                    walk(p["panels"])
        walk(d.get("panels", []))
        dirty = True

    # 4. global polish
    if d.get("refresh") == "10s":
        d["refresh"] = "30s"
        changed.append("refresh 10s->30s")
    if d.get("timezone") in ("", None):
        d["timezone"] = "Africa/Addis_Ababa"
        changed.append("timezone -> EAT")

    if dirty or changed:
        with open(path, "w") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
            f.write("\n")
    return changed


def main():
    for path in sorted(glob.glob(os.path.join(DASH_DIR, "*.json"))):
        if "ATM Monitoring — Dashen Bank" in path:
            continue  # PoC board: deleted separately, not reworked
        changed = rework(path)
        print(os.path.basename(path)[:45], "->", "; ".join(changed) if changed else "no change")


if __name__ == "__main__":
    main()
