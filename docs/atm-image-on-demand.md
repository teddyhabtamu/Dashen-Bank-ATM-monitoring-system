# ATM image evidence — on-demand pilot runbook

> **Scope:** read-only transport and an admin-only portal viewer for one image
> at a time. This does **not** add a central image store or copy image bytes
> into Postgres, OpenSearch, the portal container, or object storage.
>
> **Decision record:** `docs/adr-atm-images.md`.

## What this builds

```text
Admin viewer in the portal
  → host-side bridge over a Unix socket (allow-list, HMAC locator, audit)
    → zabbix_get -s <ATM> -t 30 -k atm.image.dir[...]        page of names
    → zabbix_get -s <ATM> -t 30 -k atm.image.info[...]       size + SHA-256
    → zabbix_get -s <ATM> -t 30 -k atm.image.chunk[...,offset,length]
      → C:\monitor\atm-image-chunk.ps1   (read-only, allow-listed)
        → E:\pic\... or iCamImageZip\...zip
```

Image bytes remain on the ATM. The host bridge holds one verified image in
memory, streams it over the Unix socket, and discards it. The portal streams
those bytes to the admin's browser; the browser is the only place a copy can
land, and the portal never writes one. The portal container has no route to
ATM TCP/10050.

**Transient boundary:** the portal worker necessarily carries each response
through per-request memory while it proxies the Unix-socket stream to the
browser. It is never written to disk, Postgres, OpenSearch, logs, or object
storage, and it is not cached. If policy requires image bytes never to transit
the portal process at all, the alternative is a separate host HTTP endpoint
with short-lived signed URLs; that adds a firewall exposure and is not part of
this pilot.

## Confirmed test coordinates

| Item | Value |
|---|---|
| GRG ATM | `10.41.30.115` (real `172.23.248.6`) |
| GRG test image | `E:\pic\2026\10\22\CAM[1]_[DateTime-261022153444]_[CarNum-8888########8888]CardInserted.jpg` |
| GRG image size / MD5 | `114,843` bytes / `187b60def3f6582f77f86191431e34a4` |
| GRG EJ interval | `2026-10-22 15:34:44.992` → `15:35:20.043` (`NOCARD`) |
| NCR ATM | `10.41.30.114` (real `172.23.248.4`) |
| NCR zip | `C:\ISUITE\ICAM\Permit\iCamImageZip\172.23.248.4_102226_163377_16_1.zip` |
| NCR zip details | 22 Oct 2026, 163,377 bytes, 16 images |

## Prerequisites

On the ATM:

- Zabbix agent reachable on TCP/10050 from the monitoring host. The host
  already pulls EJ with `zabbix_get`; if that works, the agent's `Server=`
  ACL and the network path are already correct. If not, add the bridge host
  to `Server=` and restart the agent.
- PowerShell 2.0+ (the NCR test ATM runs 2.0; the chunk reader is
  PS2-compatible and does not require .NET 4.5 or `System.IO.Compression.ZipFile`).
- `C:\monitor\` exists or can be created by the deployment account.
- McAfee/Solidifier or the bank's application control allows
  `C:\monitor\atm-image-chunk.ps1` to run. If the agent log shows a denial,
  raise a change request; do not disable the control.
- The Zabbix agent service account can read the image roots. It normally runs
  as `LocalSystem`; the file ACLs still apply.

On the monitoring host:

- `python3` and `zabbix_get` (`command -v zabbix_get`). The UAT host has
  `/usr/bin/zabbix_get`.
- This repository at the commit that contains `scripts/fetch_atm_image.py`.

## Step 1 — deploy the two files

Copy:

```text
scripts/atm-image-chunk.ps1          → C:\monitor\atm-image-chunk.ps1
config/zabbix/atm-image-roots.txt    → C:\monitor\atm-image-roots.txt
```

The roots file is **required** and fails closed. Deploy only the roots that
exist on that ATM; the supplied file is a reference template, not a
one-size-fits-all list:

* GRG pilot: `E:\pic`
* NCR: `C:\ISUITE\ICAM\Permit\icamimages`,
  `C:\ISUITE\ICAM\Permit\iCamImageZip`, and `D:\Image\icamimages` only if it
  exists on the box.

A missing root is harmless until a request uses it, but a broad root is not.

The script runs on Windows PowerShell 2.0. It ships its own small ZIP reader
using `System.IO.Compression.DeflateStream` (present since .NET 2.0), so it
does **not** need `System.IO.Compression.ZipFile`, .NET 4.5, or a PowerShell
upgrade on the ATM.

GRG has no RDP or SMB exposure. Use the bank's endpoint/software distribution
tool or the vendor's deployment channel. Do **not** open RDP, SMB, or WinRM
just for this pilot.

Record the deployed script hash on the ATM:

```powershell
Get-FileHash C:\monitor\atm-image-chunk.ps1 -Algorithm SHA256
```

Put the hash in the change record so a later "is this the same script?"
question has an answer.

## Step 2 — add the agent keys

Open the agent configuration (normally
`C:\Program Files\Zabbix Agent\zabbix_agentd.conf`, or
`zabbix_agent2.conf` for Zabbix Agent 2) and append the
`atm.image.*` block from `config/zabbix/atm-agent-userparams.conf`.

Check there is exactly one copy of each key:

```powershell
Select-String -Path 'C:\Program Files\Zabbix Agent\zabbix_agentd.conf' `
  -Pattern 'atm\.image\.'
```

A duplicate UserParameter can stop the agent from starting with
`key already exists`.

Raise the agent timeout. Find the existing `Timeout=` line and set it to 30
(or at least 10); do not add a second line:

```ini
Timeout=30
```

Every call starts PowerShell, so the default 3 seconds is too short on a
loaded ATM. This is a global agent setting; note it in the change record.

Restart only the agent service:

```powershell
Get-Service *zabbix* | Format-Table Name,Status
Restart-Service 'Zabbix Agent'      # use the name shown above
Get-Service *zabbix* | Format-Table Name,Status
```

Watch the agent log for `key already exists`, `cannot open file`, or
`Timeout while executing`.

## Step 3 — test locally on the ATM (no network)

Run the script by hand first. It only reads.

```powershell
$img = 'E:\pic\2026\10\22\CAM[1]_[DateTime-261022153444]_[CarNum-8888########8888]CardInserted.jpg'

# size + SHA-256
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File C:\monitor\atm-image-chunk.ps1 -Path $img -Info

# first 16 bytes, base64
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File C:\monitor\atm-image-chunk.ps1 -Path $img -Offset 0 -Length 16
```

Expected `-Info` output is one JSON line with `size: 114843` and a 64-character
`sha256`. If it prints `ERR:E_CONFIG`, the roots file is missing or invalid.
If it prints `ERR:E_ROOT`, the path is outside the allow-list.

For a directory, list one page (non-recursive):

```powershell
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File C:\monitor\atm-image-chunk.ps1 `
  -DirPath 'E:\pic\2026\10\22' -DirList -DirOffset 0 -DirLimit 25
```

For a zip, list a page:

```powershell
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File C:\monitor\atm-image-chunk.ps1 `
  -ZipPath 'C:\ISUITE\ICAM\Permit\iCamImageZip\172.23.248.4_102226_163377_16_1.zip' `
  -List -ListOffset 0 -ListLimit 25
```

If an unexpected error returns only `ERR:E_IO`, re-run with `-Trace` to see the
PowerShell exception. Never put `-Trace` in a UserParameter.

## Step 4 — test from the monitoring host

The CLI prints JSON and never calls the portal. Raw ATM paths and entry names
are **masked by default**; add `--show-sensitive` only for local diagnosis and
do not paste that output into a ticket.

```bash
cd /data/atm-monitoring          # the repo on UAT-01
command -v zabbix_get

# discover the day folder first (names only, non-recursive)
python3 scripts/fetch_atm_image.py --host 10.41.30.115 \
  --dir 'E:\pic\2026\10\22'

python3 scripts/fetch_atm_image.py --host 10.41.30.115 \
  --path 'E:\pic\2026\10\22\CAM[1]_[DateTime-261022153444]_[CarNum-8888########8888]CardInserted.jpg' \
  --info
```

The `--info` call returns size + SHA-256 and writes nothing. Compare it with
the local ATM output. For a quick reachability test, add
`--timeout 5 --retries 0`; for a real fetch keep the defaults. Then fetch:

```bash
python3 scripts/fetch_atm_image.py --host 10.41.30.115 \
  --path 'E:\pic\2026\10\22\CAM[1]_[DateTime-261022153444]_[CarNum-8888########8888]CardInserted.jpg' \
  --out /tmp/grg-22oct.jpg

file /tmp/grg-22oct.jpg
sha256sum /tmp/grg-22oct.jpg
md5sum /tmp/grg-22oct.jpg    # known test image: 187b60def3f6582f77f86191431e34a4
```

The CLI verifies size + SHA-256 before it publishes the file; `sha256sum` must
match the `sha256` in the JSON. A failed fetch leaves any existing `--out`
file untouched; it never publishes a partial file. If it does not match,
treat it as a transport incident, not a viewer bug.

Delete the export after the test using the bank's approved sanitisation:

```bash
shred -u /tmp/grg-22oct.jpg 2>/dev/null || rm -f /tmp/grg-22oct.jpg
```

For NCR, list the zip folder first, then the daily zip. The folder listing is
how the collector discovers zips whose names are not derivable from a date:

```bash
python3 scripts/fetch_atm_image.py --host 10.41.30.114 \
  --dir 'C:\ISUITE\ICAM\Permit\iCamImageZip'

python3 scripts/fetch_atm_image.py --host 10.41.30.114 \
  --zip 'C:\ISUITE\ICAM\Permit\iCamImageZip\172.23.248.4_102226_163377_16_1.zip' \
  --list
```

The JSON entry names/timestamps decide whether NCR can be linked by event or
only by the zip/time window. Entry names are masked in the default output;
`--show-sensitive` prints them for local diagnosis only.

## Step 4b — find candidates for one EJ session

`find_atm_image.py` lists the allow-listed day folder and ranks files by
capture time and event. It does **not** fetch bytes, write a file, or store
metadata. It needs the `atm.image.dir` key from Step 2; until that key is
deployed, the ATM returns `ZBX_NOTSUPPORTED: Unsupported item key`.

GRG:

```bash
python3 scripts/find_atm_image.py --host 10.41.30.115 --vendor GRG \
  --start '2026-10-22 15:34:44' --end '2026-10-22 15:35:21'
```

The real 22 October session produces this shape:

```json
{
  "confidence": "high",
  "ambiguous": false,
  "card_field_used_for_link": false,
  "start_anchor_count": 1,
  "end_anchor_count": 1,
  "candidates": [
    {"event": "CardInserted", "role": "start", "score": 80},
    {"event": "CardReturned", "role": "end", "score": 80},
    {"event": "", "kind": "video", "role": "in_window", "score": 30},
    {"event": "EnterInfo", "role": "in_window", "score": 30}
  ]
}
```

Candidates are ordered by score and then capture time, so the two anchors
come first; the video and `EnterInfo` share the supporting score.

Reading the result:

| Field | Meaning |
|---|---|
| `confidence: high` | Exactly one start anchor and one end anchor |
| `confidence: medium` | One anchor, or more than one anchor (`ambiguous: true`) |
| `confidence: low` | Only supporting files inside the interval |
| `confidence: none` | No matching file in the ATM folder |
| `card_field_used_for_link` | Always `false`; the card field is display-only |
| `caveat` | Must be shown next to the candidates, not hidden |

An **anchor** is `CardInserted` within 3 seconds of the session start or
`CardReturned` within 3 seconds of the session end. `CabinetOpen` is service
evidence and is excluded from the customer count. If `ambiguous` is true, do
not pick a candidate; show all of them.

A **video** is different: the filename time is the clip start, and the file
mtime is the clip end. The JSON carries `clip_end`, `clip_duration_s`, and
`capture_basis: "filename_start+file_mtime_end"`. A clip that overlaps the
session is included even if it starts before the 10-second pre-window. Show
the whole interval, not just the filename time.

NCR (day container only, until the zip entry format is confirmed):

```bash
python3 scripts/find_atm_image.py --host 10.41.30.114 --vendor NCR \
  --start '2026-10-22 00:00:00' --end '2026-10-22 23:59:59'
```

This returns the daily zip with `confidence: low` and the NCR caveat. It does
not pretend to link an entry.

If the day folder has rotated off the ATM, the CLI returns exit code `0` and:

```json
{
  "available": false,
  "reason": "Not available on ATM",
  "detail": "ERR:E_MISSING",
  "candidates": []
}
```

The viewer must render that as **Not available on ATM**, not as a broken
image or an empty success. Raw names/directories are masked by default; add
`--show-sensitive` only for local diagnosis.

The CLI is a **host-side diagnostic tool**, not the audited viewer. It does
not write the portal audit log; shell access to the host is the control. The
portal viewer (Step 6) writes `IMAGE_SEARCH` for discovery and `IMAGE_VIEW`
before it streams a byte from the bridge.

## Step 5 — fetch one NCR entry

By index (the form the portal will use, and the only safe form if the entry
name contains card data):

```bash
python3 scripts/fetch_atm_image.py --host 10.41.30.114 \
  --zip 'C:\ISUITE\ICAM\Permit\iCamImageZip\172.23.248.4_102226_163377_16_1.zip' \
  --index 0 --out /tmp/ncr-22oct.jpg
```

By name, only when the name is non-sensitive and unique:

```bash
python3 scripts/fetch_atm_image.py --host 10.41.30.114 \
  --zip 'C:\ISUITE\ICAM\Permit\iCamImageZip\172.23.248.4_102226_163377_16_1.zip' \
  --entry '<entry name from --list --show-sensitive>' \
  --out /tmp/ncr-22oct.jpg
```

The zabbix key carries the zip path and the index, never the entry name.
The host verifies the entry's declared size and CRC32 before publishing.
Only image/video entries are listed and fetchable; a text/config entry in the
same zip is not reachable through this transport.

## Step 6 — install the host bridge and enable the portal viewer

The bridge is a host process, not a container: the portal image has no
`zabbix_get` and must not have a route to ATM TCP/10050. It listens on a Unix
socket that is mounted read-only into the portal container.

### 6.1 Host files and secrets

On the monitoring host:

```bash
cd /data/atm-monitoring
git pull --ff-only origin uat-prep

sudo install -d -m 0750 -o tewodros -g tewodros /etc/atm-monitoring
sudo install -m 0640 -o root -g tewodros \
  config/atm-image-bridge.json /etc/atm-monitoring/atm-image-bridge.json
sudo install -m 0640 -o root -g tewodros \
  config/systemd/atm-image-bridge.env.example \
  /etc/atm-monitoring/image-bridge.env
```

Generate **two different** secrets. The token is shared with the portal; the
HMAC key never leaves the host:

```bash
TOKEN=$(openssl rand -hex 32)
HMAC_KEY=$(openssl rand -hex 32)

sudo sed -i "s|^ATM_IMAGE_BRIDGE_TOKEN=.*|ATM_IMAGE_BRIDGE_TOKEN=$TOKEN|" \
  /etc/atm-monitoring/image-bridge.env
printf '%s\n' "$HMAC_KEY" | sudo tee /etc/atm-monitoring/image-hmac.key >/dev/null
sudo chown tewodros:tewodros /etc/atm-monitoring/image-hmac.key
sudo chmod 0600 /etc/atm-monitoring/image-hmac.key
```

`config/atm-image-bridge.json` is the **allow-list**. It contains only the two
UAT test ATMs and their confirmed roots. For production, generate one entry per
ATM from inventory; never accept an ATM id, IP, path, or vendor from the
browser. A broad root such as `E:\` or `C:\` must be rejected at startup.

The same file sets `preflight_timeout` (default 3 seconds). Before it calls
`zabbix_get`, the bridge opens TCP/10050 to the requested ATM. If the connect
fails, the request stops there with `agent_unreachable` instead of spending
`timeout × (retries + 1)` — 60 seconds with the current settings — on an ATM
that is off or off the network. Set it to `0` only for a deliberate transport
diagnosis.

### 6.2 Start the service

```bash
sudo install -m 0644 config/systemd/atm-image-bridge.service \
  /etc/systemd/system/atm-image-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable --now atm-image-bridge
sudo systemctl status atm-image-bridge --no-pager
sudo journalctl -u atm-image-bridge -n 30 --no-pager
```

Validate without starting a second copy:

```bash
sudo -u tewodros bash -c '
  set -a; . /etc/atm-monitoring/image-bridge.env; set +a
  cd /data/atm-monitoring
  python3 scripts/atm_image_bridge.py --check
'
```

Expected: `"ok": true`, two ATM ids, and no raw key/token in the output.

Test the socket directly. The token stays out of shell history by reading it
from the root-readable env file:

```bash
sudo bash -c '
  set -a; . /etc/atm-monitoring/image-bridge.env; set +a
  curl --silent --show-error --unix-socket /run/atm-image-bridge/bridge.sock \
    -H "Authorization: Bearer $ATM_IMAGE_BRIDGE_TOKEN" \
    http://localhost/healthz
'
```

Expected:

```json
{"ok":true,"atms":2}
```

An unauthenticated request must return `401`; a request for an ATM not in the
allow-list must return `404`.

### 6.3 Give the portal the socket and token

The token is the **same** value generated above. Update `.env` on the host:

```bash
cd /data/atm-monitoring
TOKEN=$(sudo awk -F= '/^ATM_IMAGE_BRIDGE_TOKEN=/{print $2}' \
  /etc/atm-monitoring/image-bridge.env)

if grep -q '^ATM_IMAGE_BRIDGE_TOKEN=' .env; then
  sed -i "s|^ATM_IMAGE_BRIDGE_TOKEN=.*|ATM_IMAGE_BRIDGE_TOKEN=$TOKEN|" .env
else
  printf '\nATM_IMAGE_BRIDGE_SOCKET=/run/atm-image-bridge/bridge.sock\nATM_IMAGE_BRIDGE_TOKEN=%s\n' \
    "$TOKEN" >> .env
fi
```

`docker-compose.yml` mounts `/run/atm-image-bridge` read-only into
`report-portal` and passes `ATM_IMAGE_BRIDGE_TOKEN`. If the directory does not
exist, create it with the service user before recreating the portal; otherwise
Docker may create a root-owned directory that the service cannot use:

```bash
sudo install -d -o tewodros -g tewodros -m 0750 /run/atm-image-bridge
docker compose build report-portal
docker compose up -d --force-recreate report-portal
docker compose exec report-portal python -c \
  "import os; print(os.path.exists('/run/atm-image-bridge/bridge.sock'))"
```

Expected: `True`. Also prove the container can connect, not just see the file:

```bash
docker compose exec report-portal python -c \
  "import socket; s=socket.socket(socket.AF_UNIX); s.connect('/run/atm-image-bridge/bridge.sock'); print('connected')"
```

If it prints `False` or `Permission denied`, the bridge is not running, the
mount was created before the socket directory existed, or SELinux is denying
the bind mount. Fix the service/order first; do not disable SELinux. On an
enforcing host, inspect `ausearch -m avc -ts recent` and label the shared
directory under change control.

The unit sets `RuntimeDirectoryPreserve=yes`. This is not optional: without it,
every bridge restart removes and recreates `/run/atm-image-bridge`, and the
running portal container stays mounted to the old, now-empty directory. The
symptom is **Image bridge not configured** even though the bridge is active.
After changing the unit, restart the bridge and recreate the portal once.

### 6.4 Use the viewer

Open:

```text
http://<portal>/ej-journal/ATM-TEST-GRG/images
```

Then choose the ATM day and the session window. The EJ Journal detail page has
an **ATM images** button; each staged transaction and each journal file has an
**Images** link. A transaction link fills a ±2 minute window. The evidence
table shows the confidence, both anchors, the masked filename, the video clip
interval, and a **View** link. **View** opens the portal's evidence page, not a
bare image tab: it keeps the sidebar and the ATM context, shows the fetch state,
and distinguishes **Not available on ATM** from a bridge/ATM transport failure.
The page fetches the verified stream once and holds it in this browser tab only
(an object URL); there is no thumbnail cache, disk cache, or second copy. The
**Open raw stream** action is for a direct URL or the browser's own viewer.

For the confirmed GRG session:

```text
Day:    2026-10-22
Start:  15:34:44
End:    15:35:21
```

Expected candidates: `CardInserted.jpg` (start, 114,843 bytes),
`CardReturned.jpg` (end, 108,133 bytes), the 37-second `.mp4` (clip
`15:34:44` → `15:35:21`), and `EnterInfo.jpg` (supporting).

For NCR, choose the day and open the daily zip; entries are addressed by
**archive index**, never by name. The page says the zip is a day container,
not a transaction link, until the entry timestamps are confirmed.

### 6.5 What the audit records

Two actions are written to `audit_log`:

| Action | When | Contains |
|---|---|---|
| `IMAGE_SEARCH` | A day/window or zip listing is requested | ATM id, day, window, confidence, counts, unavailable/error flags |
| `IMAGE_VIEW` | Before an image/video byte is streamed | ATM id, day, HMAC prefix, size, SHA-256 or CRC32 |

Neither action contains a raw filename, ATM path, full PAN, HMAC key, or image
bytes. The page and every response use `Cache-Control: no-store`.

```sql
SELECT performed_at, username, action, detail
  FROM audit_log
 WHERE action IN ('IMAGE_SEARCH', 'IMAGE_VIEW')
 ORDER BY performed_at DESC
 LIMIT 20;
```

### 6.6 Verify the two failure states

1. **Missing on ATM:** request a day with no folder, or delete a test export
   from the ATM. The page must say **Not available on ATM**. It must not show a
   broken image or an empty success.
2. **Bridge down:** `sudo systemctl stop atm-image-bridge`, reload the viewer.
   The page must say **Image transport failed** / bridge not running. It must
   **not** say "Not available on ATM", because the puller is broken, not the
   evidence.
3. **ATM agent unreachable:** stop or unplug the test ATM, or block TCP/10050
   from the monitoring host. The page must say **ATM agent unreachable** and
   return in about three seconds, not after the 60-second retry budget. This is
   still not missing evidence.
4. **Wrong token:** change only the portal `.env` token and recreate the
   portal. The bridge returns `401`; the viewer reports a transport/token
   failure, not missing evidence.

Start the service again after the test.

## Rollback

No image or database state is created, so rollback is configuration only:

1. `sudo systemctl disable --now atm-image-bridge`
2. Remove the `/run/atm-image-bridge` bind mount and
   `ATM_IMAGE_BRIDGE_*` variables from `docker-compose.yml`/`.env`, then
   `docker compose up -d report-portal`.
3. Remove `/etc/systemd/system/atm-image-bridge.service` and
   `/etc/atm-monitoring/atm-image-bridge.json` if the pilot is ending.
4. Remove the four `atm.image.*` UserParameter lines from
   `zabbix_agentd.conf`.
5. Restart the agent service.
6. Delete `C:\monitor\atm-image-chunk.ps1` and
   `C:\monitor\atm-image-roots.txt`.
7. Restore the previous agent `Timeout` if it was changed only for this pilot.
8. Delete any test export from the host.

The agent keys are additive; removing them does not affect `atm.parts`,
`atm.xfs.cdm`, or the EJ pull. `audit_log` rows are retained under the bank's
audit-retention policy.

## Troubleshooting

| Symptom | Meaning | Action |
|---|---|---|
| `ZBX_NOTSUPPORTED: Unsupported item key` | Agent has not loaded the UserParameter | Check the exact line, duplicates, config path, and restart |
| `ZBX_NOTSUPPORTED` only for the new keys | Agent uses `AllowKey`/`DenyKey` | Add `AllowKey=atm.image.*` before the deny rule and restart |
| `ZBX_NOTSUPPORTED: Timeout while executing` | Agent `Timeout` too low, or PowerShell blocked | Set `Timeout=30`; check McAfee/Solidifier; run the script locally |
| `ERR:E_CONFIG` | Roots file missing, empty, or invalid | Copy `atm-image-roots.txt`; keep roots absolute and narrow |
| `ERR:E_ROOT` | Path is outside the allow-list | Fix the roots file or the path; do not widen to `C:\` |
| `ERR:E_EXT` | Unsupported extension | Only `.jpg`, `.jpeg`, `.png`, `.mp4`, `.zip` |
| `ERR:E_MISSING` | File rotated or deleted | Expected; the viewer should say "Not available on ATM" |
| `ERR:E_REPARSE` | A junction/symlink is in the path | Investigate the directory; do not bypass the check |
| `ERR:E_ZIP_BUSY` | ICAM has the zip open | Retry later; do not copy or unlock the zip |
| `ERR:E_SIZE` | File/entry above the cap | Do not raise the cap without a review |
| `ERR:E_KEY` | The key is longer than 240 characters | Shorten the root/path; use the zip index; never put a PAN in a key |
| `E_CONTENT` (host) | The bytes are not a real JPEG/PNG/MP4 | Treat as an incident; the viewer must not show it |
| `E_SHA256` / `E_CRC32` (host) | The file changed during the fetch | Retry once; if persistent, investigate the ATM |
| `agent timeout` | Network, ACL, or agent load | Check TCP/10050 ACL and the agent log |
| `agent_unreachable` | TCP/10050 refused or timed out during the 3-second preflight | Treat as ATM offline/off-network; do not treat it as missing evidence |
| `zabbix_get not found` | Host tooling missing | Install the Zabbix agent package or set `ZBX_GET` |
| Viewer says **Image bridge not configured** | Socket/token missing in the portal | Check `ATM_IMAGE_BRIDGE_*` in `.env`, the read-only `/run/atm-image-bridge` mount, and `systemctl status atm-image-bridge` |
| Viewer says bridge not running right after a bridge restart | The running container is mounted to the old `/run/atm-image-bridge` inode; systemd removed and recreated it | Keep `RuntimeDirectoryPreserve=yes` in the unit. After adding it, restart the bridge and recreate the portal once. |
| Viewer says **Image transport failed** | Bridge is down or the ATM did not answer | Check the bridge journal, `zabbix_get`, and TCP/10050; do not treat this as missing evidence |
| Viewer says **ATM agent unreachable** | The 3-second TCP/10050 preflight failed | Check ATM power/link and the IP mapping; this is an outage, not missing evidence |
| Bridge returns `401` | Portal token differs from the host env file | Regenerate/paste the same token, recreate the portal |
| Bridge returns `404 unknown_atm` | ATM id is not in `config/atm-image-bridge.json` | Add it under change control; never accept an IP from the browser |
| `E_CHANGED` / changed evidence | The file or zip was replaced during the fetch | Refresh candidates; investigate if it repeats |
| `429 busy` | Two requests are already pulling from that ATM | Wait for the current view to finish; do not raise the limit blindly |

## Security checklist

- [ ] TCP/10050 is ACL'd to the monitoring host/bridge, not the whole network.
- [ ] The 10050 path is on a trusted/ACL'd network. This pilot does not add
      TLS; the long-term fleet design needs an HTTPS agent or tunnel.
- [ ] `C:\monitor\atm-image-roots.txt` is present and contains no drive root,
      UNC path, wildcard, or colon after the drive letter.
- [ ] The deployed script hash is in the change record.
- [ ] The portal container has no route to ATM TCP/10050; it reaches only the
      host bridge socket.
- [ ] The bridge allow-list contains only ATMs approved for image evidence;
      unknown ATM ids and paths are refused before `zabbix_get`.
- [ ] Viewer access is admin-only; every search and view is audited
      (`IMAGE_SEARCH`, `IMAGE_VIEW`).
- [ ] Responses use `Cache-Control: no-store` and `X-Content-Type-Options:
      nosniff`; evidence responses also use a sandbox CSP.
- [ ] No image bytes in Postgres, OpenSearch, logs, or object storage.
- [ ] Paths in tickets/logs are masked with `mask_sensitive`; full PANs never
      appear in a Zabbix key.
- [ ] Directory listings are masked in the UI and audit; raw names stay in the
      host bridge process and are never rendered.
- [ ] The HMAC key is outside the repository, database and portal container.
- [ ] Test exports are deleted under the bank's media-sanitisation policy.
- [ ] Compliance has signed off on lawful basis, retention, audit, and PCI
      masking before the viewer is exposed to operators.

## Performance and limits

| Item | Limit / expectation |
|---|---|
| Chunk | 24 KiB default, 32 KiB max (base64 ~32–44 KiB, under the Zabbix text limit) |
| Still image | 32 MiB cap; a 115 KiB JPEG is ~5 chunks |
| Video | 8 MiB cap; chunking is best-effort, not a video streaming design |
| Zip entry | 8 MiB cap; 5,000 entries; listing page 25 (max 100) |
| Zip file | 256 MiB cap; the daily NCR zip is ~160 KB |
| Directory | Non-recursive; 5,000 evidence entries; page 25 (max 100) |
| Latency | ~1–2 s per chunk/page because each call starts PowerShell |
| Zip skip cost | Each chunk reopens the zip and skips from the start; the 8 MiB entry cap bounds this. Still images are the target; video is best-effort |
| Concurrency | Limit at the bridge (e.g. 2 per ATM); no caching |
| Offline ATM | 3-second TCP/10050 preflight, then `agent_unreachable`; no 60-second retry loop |
| Bridge memory | One verified image per request; bounded by the 8 MiB video/zip-entry cap |
| Bridge socket | Unix stream socket, mode 0660, mounted read-only into the portal |
| Portal worker | Gunicorn timeout is 120 s; a still image is the target. A maximum-size video/zip entry may exceed it, so do not treat a timeout as missing evidence |

The long-term answer for 1,300 ATMs and video is an ATM-side HTTPS file agent
or tunnel, not a 24 KiB Zabbix key. The chunk transport is the smallest thing
that works on both test ATMs without a network change.

## Portal bridge contract (implemented)

The browser never supplies a path. The only inputs are an ATM id, a local day,
and a keyed HMAC locator returned by the bridge:

```text
GET /ej-journal/<atm_id>/images
  → portal asks the bridge for masked candidates for one day/window
  → bridge re-lists the allow-listed ATM folder and ranks by
    capture time + event sequence
  → each candidate carries name_masked + name_hmac (no raw name/path)

GET /ej-journal/<atm_id>/images/view?day=...&name_hmac=...&kind=...
  → portal page around one piece of evidence (sidebar, ATM context, fetch state)
  → the page fetches the stream once into this tab and shows missing vs failure
GET /ej-journal/<atm_id>/images/file?day=...&name_hmac=...
  → bridge re-lists the same folder, recomputes HMAC for every entry,
    matches exactly one, then fetches size + SHA-256 + bytes
  → portal streams bytes with Cache-Control: no-store
  → Content-Type from the validated extension; nosniff; sandbox CSP
  → Content-Disposition: inline; filename="atm-evidence.jpg"
  → IMAGE_VIEW audit before the first byte
  → 404 "Not available on ATM" if the source is gone

GET /ej-journal/<atm_id>/images/zip?day=...&zip_hmac=...
  → NCR daily zip listing; entries addressed by archive index
  → /images/view?day=...&source=zip&zip_hmac=...&index=...
  → /images/zip/file?day=...&zip_hmac=...&index=... (raw stream)
```

The HMAC locator is `HMAC-SHA256(server_key, raw_filename)`. The key lives only
in the host service environment/file, never in Postgres, the portal container,
the repository, or a log. At view time the bridge re-lists the directory and
recomputes the HMAC for each name; it does not trust a path supplied by the
caller. A stale locator returns **Not available on ATM**; a changed file returns
a changed/retry state. For a zip entry, the archive index + size + CRC32 are
enough to re-select it; the entry name is not needed.

The pilot does **not** create an `atm_image_evidence` table. It computes the
locator live, so there is no second copy of card-bearing filenames to protect
or retain. A persistent metadata index is optional and would need its own
retention/compliance decision; if added, it must store only `name_masked` and
`name_hmac`, never the raw name or bytes.

## Open questions

1. NCR zip entry names and timestamps (run `--list`).
2. GRG/NCR retention for day folders and zips.
3. Production path: `C:\ISUITE\ICAM\Permit\icamimages` or `D:\Image\icamimages`?
4. Are production GRG `CarNum` and NCR entry names always masked? If not,
   move file reads to a directory index as well.
5. Does NetXMS cache images centrally, or only show what is still on the ATM?
6. Compliance sign-off: lawful basis, retention, access audit, PCI masking.
7. Metadata collector: the pilot computes HMACs live and stores nothing. A
   persistent index is deferred; do not scan-hash every file or import raw
   filenames into Postgres.
