#!/usr/bin/env bash
# Diagnose Incident Management avatars on the UAT host.
#
# Run from the repo root:
#   bash scripts/check-glpi-avatars.sh
#
# It uses the portal container's own GLPI credentials, lists every GLPI
# user's `picture` field, and fetches the picture through the same API
# endpoint the portal uses. A user with picture='' has nothing to show;
# a user with a filename but a non-200 fetch is a GLPI-side problem.
set -euo pipefail
docker compose exec -T report-portal python - <<'PY'
import os
import sys
import requests

base = (os.environ.get('GLPI_UI_URL') or '').rstrip('/')
app = os.environ.get('GLPI_APP_TOKEN') or ''
user = os.environ.get('GLPI_USER') or ''
password = os.environ.get('GLPI_API_PASSWORD') or ''
if not (base and app and user and password):
    sys.exit('Missing GLPI_UI_URL / GLPI_APP_TOKEN / GLPI_USER / GLPI_API_PASSWORD')

headers = {'App-Token': app}
r = requests.get(f'{base}/apirest.php/initSession', headers=headers,
                 auth=(user, password), timeout=10)
print(f'GLPI: {base}  initSession={r.status_code}')
if r.status_code != 200:
    print(r.text[:500])
    sys.exit(1)
token = r.json().get('session_token')
headers['Session-Token'] = token
try:
    r = requests.get(f'{base}/apirest.php/User/',
                     params={'range': '0-999'}, headers=headers, timeout=20)
    print(f'User list={r.status_code}')
    if r.status_code != 200:
        print(r.text[:500])
        sys.exit(1)
    rows = r.json()
    if isinstance(rows, dict):
        print(rows)
        sys.exit(1)
    print(f'{"id":>4}  {"login":<28} {"picture":<38} picture fetch')
    for u in rows:
        uid = u.get('id')
        login = (u.get('name') or '')[:28]
        picture = (u.get('picture') or '')
        if not picture:
            print(f'{uid:>4}  {login:<28} {"(none)":<38} -')
            continue
        p = requests.get(f'{base}/apirest.php/user/{uid}/picture',
                         headers=headers, timeout=10)
        print(f'{uid:>4}  {login:<28} {picture[:38]:<38} '
              f'{p.status_code} {p.headers.get("Content-Type", "?")} {len(p.content)}B')
finally:
    requests.get(f'{base}/apirest.php/killSession', headers=headers, timeout=10)
PY
