# ATM image evidence — on-demand pilot runbook

> **Scope:** read-only transport for one image at a time. This does **not**
> add a central image store, an image database column, or a portal page yet.
> The viewer remains gated by compliance and by the NCR zip entry format.
>
> **Decision record:** `docs/adr-atm-images.md`.

## What this builds

```text
Admin viewer (later)
  → host-side bridge (metadata ID, allow-list, audit)
    → zabbix_get -s <ATM> -t 30 -k atm.image.info[...]
    → zabbix_get -s <ATM> -t 30 -k atm.image.chunk[...,offset,length]
      → C:\monitor\atm-image-chunk.ps1   (read-only, allow-listed)
        → E:\pic\... or iCamImageZip\...zip
```

Image bytes remain on the ATM. The host writes a file only when an operator
runs the CLI with `--out`; the future bridge will stream from memory. The
portal container never talks to an ATM.

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
- PowerShell 3.0+ and .NET 4.5+ (Windows 10 is fine).
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

For the NCR zip, this is also the discovery command we still need:

```bash
python3 scripts/fetch_atm_image.py --host 10.41.30.114 \
  --zip 'C:\ISUITE\ICAM\Permit\iCamImageZip\172.23.248.4_102226_163377_16_1.zip' \
  --list
```

The JSON entry names/timestamps decide whether NCR can be linked by event or
only by the zip/time window. Entry names are masked in the default output;
`--show-sensitive` prints them for local diagnosis only.

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

## Rollback

No image or database state is created, so rollback is configuration only:

1. Remove the four `atm.image.*` UserParameter lines from
   `zabbix_agentd.conf`.
2. Restart the agent service.
3. Delete `C:\monitor\atm-image-chunk.ps1` and
   `C:\monitor\atm-image-roots.txt`.
4. Restore the previous agent `Timeout` if it was changed only for this pilot.
5. Delete any test export from the host.

The agent keys are additive; removing them does not affect `atm.parts`,
`atm.xfs.cdm`, or the EJ pull.

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
| `zabbix_get not found` | Host tooling missing | Install the Zabbix agent package or set `ZBX_GET` |

## Security checklist

- [ ] TCP/10050 is ACL'd to the monitoring host/bridge, not the whole network.
- [ ] The 10050 path is on a trusted/ACL'd network. This pilot does not add
      TLS; the long-term fleet design needs an HTTPS agent or tunnel.
- [ ] `C:\monitor\atm-image-roots.txt` is present and contains no drive root,
      UNC path, wildcard, or colon after the drive letter.
- [ ] The deployed script hash is in the change record.
- [ ] The portal container has no route to ATM TCP/10050.
- [ ] Viewer access is admin-only; every view is audited (`IMAGE_VIEW`).
- [ ] Responses use `Cache-Control: no-store` and `X-Content-Type-Options:
      nosniff`.
- [ ] No image bytes in Postgres, OpenSearch, logs, or object storage.
- [ ] Paths in tickets/logs are masked with `mask_sensitive`; full PANs never
      appear in a Zabbix key.
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
| Latency | ~1–2 s per chunk because each call starts PowerShell |
| Zip skip cost | Each chunk reopens the zip and skips from the start; the 8 MiB entry cap bounds this. Still images are the target; video is best-effort |
| Concurrency | Limit at the bridge (e.g. 2 per ATM); no caching |

The long-term answer for 1,300 ATMs and video is an ATM-side HTTPS file agent
or tunnel, not a 24 KiB Zabbix key. The chunk transport is the smallest thing
that works on both test ATMs without a network change.

## Portal bridge contract (next phase)

The viewer must not accept a path from the browser. It uses a metadata ID:

```text
GET /admin/evidence/image/<id>
  → bridge looks up id in atm_image_evidence (no bytes in the row)
  → re-lists the zip / checks the file
  → streams bytes with Cache-Control: no-store
  → Content-Type from the validated extension; nosniff
  → Content-Disposition: inline; filename="atm-image.jpg" (never the raw name)
  → writes IMAGE_VIEW audit before the first byte
  → 404 "Not available on ATM" if the source is gone
```

Metadata index (no bytes):

```text
atm_image_evidence
  id, atm_id, captured_at, event,
  source_kind (file|zip), source_path (restricted),
  source_path_masked, zip_entry_name, zip_entry_index,
  size_bytes, sha256, crc32,          -- sha256 for files, crc32 for zip entries
  first_seen, last_seen, last_viewed_at
```

`source_path` can contain sensitive filename fragments; store it in a
restricted column/table with column-level privileges, and never render it in
the UI. The UI shows `source_path_masked`.

## Open questions

1. NCR zip entry names and timestamps (run `--list`).
2. GRG/NCR retention for day folders and zips.
3. Production path: `C:\ISUITE\ICAM\Permit\icamimages` or `D:\Image\icamimages`?
4. Are production GRG `CarNum` and NCR entry names always masked? If not,
   move file reads to a directory index as well.
5. Does NetXMS cache images centrally, or only show what is still on the ATM?
6. Compliance sign-off: lawful basis, retention, access audit, PCI masking.
7. Paged directory listing (`atm.image.dir`) for the metadata collector; do
   not use recursive `vfs.dir.get` on a 500-entry folder without checking the
   Zabbix text-value limit.
