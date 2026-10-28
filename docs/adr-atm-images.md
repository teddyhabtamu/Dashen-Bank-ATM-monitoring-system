# ADR: ATM camera images — on-demand evidence, no central image store

> **Status:** Accepted; admin-only pilot implemented (2026-10-27).
>
> The target paths, the agent keys, the chunk transport and the full image
> SHA-256/MD5 match are proven against the real 22 October GRG image. The
> admin-only portal viewer and host bridge are implemented. The **viewer is
> still gated** by compliance, by the NCR zip entry format, and by the
> retention answers below before operator or production exposure. This ADR
> records the on-demand model as the chosen direction and keeps the
> central-receiver design only as a fallback.

## Decision

**Image bytes stay on the ATM.** The portal holds metadata only. When an
authorised administrator opens an image, a host-side bridge reads the file
through the existing Zabbix agent in 24 KiB base64 chunks, verifies the size
and SHA-256 (or a zip entry's CRC32), streams it with
`Cache-Control: no-store`, and writes an audit record. If the ATM no longer
has the file, the viewer says **"Not available on ATM"**. There is no central
image archive, no object storage, and no image bytes in Postgres.

This matches the current NetXMS behaviour the operations team knows: NetXMS
also shows only files that are still on the ATM. It is also the strongest
data-minimisation story available to the bank: there is no new central store
of customer faces to retain, secure, or purge.

## Why this shape

| Driver | On-demand | Central receiver |
|---|---|---|
| Customer-face copies | None beyond the ATM | A new store to secure and purge |
| Retention | ATM's own window | Must be defined and enforced centrally |
| Dispute evidence after the ATM rotates | Not available | Available |
| Cost at 1,300 ATMs | No new storage; a bridge and one agent key | Upload queue, receiver, object storage, lifecycle |
| Failure mode | "Not available on ATM" | Silent gaps if uploads fail |
| Compliance review | Smaller scope | Larger scope |

The bank may later decide that disputes need images **after** the ATM rotates
them. That is a retention decision, not an engineering preference. If it is
required, the central-receiver fallback below becomes necessary; it is not
being built now.

## Confirmed evidence (2026-10-22, test ATMs)

### GRG (`10.41.30.115`, real `172.23.248.6`)

Live path:

```text
E:\pic\2026\10\22\
  CAM[1]_[DateTime-261022153444]_[CarNum-8888########8888]CardInserted.jpg
  CAM[1]_[DateTime-261022153519]_[CarNum-8888########8888]EnterInfo.jpg
  CAM[1]_[DateTime-261022153521]_[CarNum-8888########8888]CardReturned.jpg
  CAM[1]_[DateTime-261022153444]_[CarNum-8888########8888].mp4
```

Filename rule:

```text
CAM[n]_[DateTime-YYMMDDHHMMSS]_[CarNum-<masked-card>]<Event>.jpg|.mp4
```

Confirmed events: `CardInserted`, `PinEntered`, `RequestCode`,
`ReplyNextFunc`, `CardReturned`, `EnterInfo`, `CabinetOpen`.

The file used for the path/metadata proof was:

```text
E:\pic\2026\10\22\CAM[1]_[DateTime-261022153444]_[CarNum-8888########8888]CardInserted.jpg
size    114,843 bytes
MD5     187b60def3f6582f77f86191431e34a4
```

`vfs.file.size` and `vfs.file.md5sum` resolved the **bracketed filename**
without escaping, which is why the chunk key can carry the path directly.

GRG EJ for the same interval:

```text
2026-10-22 15:34:44.992 ----> TRANSACTION START NOCARD
2026-10-22 15:35:20.043 <---- TRANSACTION END
```

The NCR EJ shows the same `15:34:44` NOCARD interval, so the two-vendor test
was a coordinated session.

### NCR (`10.41.30.114`, real `172.23.248.4`)

Live paths:

```text
C:\ISUITE\ICAM\Permit\icamimages\<YY>\<MM>\<DD>\      (26 = 2026, 25 = 2025)
C:\ISUITE\ICAM\Permit\iCamImageZip\<ip>_<MMDDYY>_<size>_<count>_<camera>.zip
```

The durable unit is the **daily zip**, not the day folder. The 21 and 22
October day folders are empty because ICAM has already zipped them. Latest
observed:

```text
172.23.248.4_102226_163377_16_1.zip   22 Oct 2026, 16 images
172.23.248.4_102126_153953_15_1.zip   21 Oct 2026, 15 images
```

`D:\Image\icamimages` does **not** exist on TESTMACHINE. The NCR
`Snapshots` folder configured by `NDCSecurityCamera.xml` is empty; ICAM is
the active path. The zip entry names and timestamps are still unknown; the
PowerShell listing is the outstanding discovery item.

### The link rule is timestamp + event, not card

The GRG image filename says `CarNum-8888########8888`, but the GRG EJ says
`TRANSACTION START NOCARD`. The card field in the filename is therefore **not**
a reliable transaction link. The timestamps match to the second:

```text
15:34:44 image CardInserted   / EJ transaction start 15:34:44.992
15:35:19 image EnterInfo
15:35:21 image CardReturned   / EJ transaction end   15:35:20.043
```

The defensible link is **capture time + event sequence**, with the EJ
transaction interval as the anchor. It is not a foreign key. Two transactions
inside one second remain ambiguous, which is exactly the fraud pattern where a
wrong link would be worst. The viewer must show the timestamp evidence and a
short caveat; it must not present the card field as proof.

The matcher is implemented as a pure module, `scripts/atm_image_link.py`, with
a read-only CLI, `scripts/find_atm_image.py`. It **ranks, never joins**:

* `CardInserted` within 3 seconds of the session start is a **start anchor**;
* `CardReturned` within 3 seconds of the session end is an **end anchor**;
* other events inside the interval are supporting evidence;
* `CabinetOpen` is service evidence and is excluded from the customer count;
* the card field is displayed but the result always carries
  `card_field_used_for_link: false`.

`confidence: high` means exactly one start anchor and one end anchor.
`ambiguous: true` means more than one anchor matched, so the viewer must not
pick one. For NCR the matcher stops at the **day zip**, because the entry
naming/timestamps are not confirmed; it returns `confidence: low` with that
caveat rather than inventing an entry-level link.

For a GRG **video**, the filename time is the clip start and the file mtime is
the clip end. The matcher reports both (`captured_at`, `clip_end`,
`clip_duration_s`, `capture_basis`) and includes a clip that overlaps the
session even if it starts before the pre-window. The viewer must show the
whole clip interval, not just the filename time.

## Transport: read-only chunks through the existing agent

The reachability test settles the channel:

| Host | 10050 | 135/139/445 | 5985/5986 | 22 | 3389 |
|---|---|---|---|---|---|
| NCR `10.41.30.114` | open | closed | closed | closed | open |
| GRG `10.41.30.115` | open | closed | closed | closed | closed |

SMB, WinRM and SSH are not available. The only common automation channel is
the **Zabbix agent on TCP/10050**. The built-in `vfs.file.contents` cannot
carry a JPEG or zip (text-only, truncated), so the pilot adds a narrow
UserParameter that returns base64 chunks. The image is text-encoded only for
the duration of one call; it is never stored as a Zabbix item or in the
Zabbix database. See `docs/adr-ej-transport.md` for the text/binary limit.

```text
Portal (admin viewer)
  → host-side bridge over a Unix socket, read-only mount
      (allow-list, HMAC locator, audit)
    → zabbix_get -s <ATM> -k atm.image.dir[...]         page of names
    → zabbix_get -s <ATM> -k atm.image.info[...]        size + SHA-256
    → zabbix_get -s <ATM> -k atm.image.chunk[...,o,l]   base64 chunks
      → ATM: C:\monitor\atm-image-chunk.ps1
        → read-only directory / file / zip entry
```

The portal container has **no** route to ATM TCP/10050 and no `zabbix_get`.
The Unix socket is mounted read-only; the bridge holds the HMAC key and the
ATM allow-list. A request carries an ATM id, a day and an HMAC locator, never
a path. The portal proxies the verified stream through per-request worker
memory; it is never written or cached. A literal no-transit requirement would
need a separate host HTTP endpoint with signed URLs, which adds firewall
exposure and is not part of this pilot.

Keys:

```text
atm.image.dir[<base64url dir>,<offset>,<limit>]
atm.image.info[<base64url path>]
atm.image.chunk[<base64url path>,<offset>,<length>]
atm.image.ziplist[<base64url zip>,<offset>,<limit>]
atm.image.zipentry[<base64url zip>,<index>,<offset>,<length>]
```

Zip entries are addressed by **archive index**, so an entry name, which may
contain a card number, never appears in a Zabbix key. A directory listing
carries only the directory path in the key; filenames appear in the JSON
value and are masked in CLI/UI output by default. For GRG files the path is
in the key; confirm that production filenames are masked (the test filename
is). If they are not, the next hardening step is an index-based directory
read, not a wider key.

The agent's `Timeout` must be raised (30 recommended, 10 minimum) because
every call starts PowerShell. The default 3 seconds is too short on a loaded
ATM. Restrict TCP/10050 to the bridge host; the key is a file-read primitive.

The script is PowerShell 2.0-compatible. It reads zip central directories and
inflates method-8 entries with `System.IO.Compression.DeflateStream`, so it does
not depend on `System.IO.Compression.ZipFile` or .NET 4.5 on the ATM.

### What the ATM script does and does not do

It is read-only: no writes, extraction, deletion, rename, or execution. It:

* requires `C:\monitor\atm-image-roots.txt` and fails closed if it is missing,
  empty, a drive root, a UNC path, or otherwise malformed;
* allows only `.jpg`, `.jpeg`, `.png`, `.mp4`, `.zip` under the configured
  roots; zip entries are further filtered to image/video extensions;
* rejects `..`, UNC paths, alternate data streams, reserved device names,
  reparse points, and paths outside the roots;
* caps still images at 32 MiB, video and zip entries at 8 MiB, image zips at
  256 MiB, chunks at 32 KiB, directory/zip entries at 5,000, and names at
  256 characters;
* lists directories **non-recursively** and filters to evidence extensions;
  this is the discovery primitive that replaces recursive `vfs.dir.get`;
* returns generic `ERR:E_*` codes and never a path;
* emits one line with `[System.Console]::WriteLine`, not `Write-Output` (the
  latter wraps long JSON/base64 at console width and inserts CRLF — the WMI
  collector hit that bug on 2026-10-09).

### What the host bridge does (implemented)

* accepts only ATM ids in the host-side allow-list; vendor, host, roots and
  day-folder rules come from that config, never from the request;
* listens on a Unix socket, not a TCP port, and requires a shared bearer token;
* resolves an opaque HMAC locator to an allow-listed source, never a
  user-supplied path;
* re-lists the source at view time and recomputes `HMAC-SHA256(key, name)` for
  every entry, so a stale locator cannot become a path;
* discovers files with the paged, non-recursive directory listing, so a
  browser cannot ask the ATM to walk the disk;
* ranks candidates with the shared `atm_image_link` rule, showing the
  confidence and caveat rather than a transaction key;
* checks the ATM's size + SHA-256 before streaming; zip entries are checked by
  size + CRC32;
* checks magic bytes so an HTML error page saved as `.jpg` is refused;
* holds the verified bytes in memory for one response and discards them; no
  central file is written;
* the portal writes `IMAGE_SEARCH` for discovery and `IMAGE_VIEW` before the
  first byte, with the admin identity, ATM, day, HMAC prefix, size and hash;
* sends `Cache-Control: no-store`, `X-Content-Type-Options: nosniff` and a
  sandbox CSP, and never logs image bytes or raw names;
* returns **404 "Not available on ATM"** when the file is gone, and a
  separate transport error when the ATM or bridge did not answer.

The pilot has no `atm_image_evidence` table. The locator is recomputed live,
so there is no central copy of card-bearing filenames to retain. A persistent
metadata index is deferred and, if added, must store only `name_masked` and
`name_hmac`, never the raw name or bytes.

## Compliance gates

These are bank decisions and still gate operator/production exposure of the
admin-only pilot, not the transport itself:

| Gate | Why |
|---|---|
| Lawful basis + consent signage at the ATM | Face images are biometric-adjacent personal data |
| Retention and automatic purge | Even on-demand metadata is personal data; the ATM's own window is the effective limit |
| Access control + audit of every view | Who may see a customer's face is an audit question |
| PCI DSS | An image can capture a PAN or the PIN pad; filename masking is not a substitute for review |
| Least-privilege account and read-only endpoints | A monitoring-host compromise must not become a customer-face archive |

The on-demand model **reduces** the compliance scope; it does not remove it.
The metadata still needs retention, access control, and audit.

## Remaining unknowns

1. **NCR zip entry names and timestamps** — the PowerShell listing is pending.
   Until the zip is listed, NCR images cannot be linked by event; the
   transport can list them once the script is deployed.
2. **NCR and GRG retention** — how long the ATM keeps day folders and zips,
   and when ICAM archives/deletes them.
3. **Production path** — does production use `C:\ISUITE\ICAM\Permit\icamimages`
   or `D:\Image\icamimages`? TESTMACHINE only has the former.
4. **Production filename masking** — confirm that GRG `CarNum` and NCR entry
   names never contain a full PAN. If they do, the key must be index-based
   for files too.
5. **Does NetXMS cache centrally?** Open an image older than the ATM's
   retention. If NetXMS still shows it, NetXMS is a central store and the
   on-demand model is weaker for old disputes.
6. **Compliance sign-off** on lawful basis, retention, audit, and masking.
7. **Metadata collector** — the pilot computes the HMAC locator live from the
   paged, non-recursive `atm.image.dir`; it stores nothing. A persistent
   metadata index is deferred; do not import raw filenames into Postgres.

## Phasing

| Phase | Scope | State |
|---|---|---|
| 1 | Read-only transport proof on GRG | Done: 114,843 bytes, SHA-256 and MD5 match |
| 2 | Host bridge over Unix socket + admin viewer, HMAC locator, audit, no-store | Implemented; UAT install pending |
| 3 | NCR zip entry listing and timestamp semantics | Pending `--list` after NCR deployment |
| 4 | UAT pilot on both test ATMs; retention measured | Pending |
| 5 | Compliance sign-off, then fleet rollout; long-term file agent/tunnel if 1,300 ATMs outgrow chunking | Gate |

A persistent metadata index (no bytes) is optional and deferred; it is not a
prerequisite for the viewer. It would store only masked names and HMACs.

## Fallback if retention beyond the ATM is required

If the bank decides that disputes need images **after** the ATM rotates them,
the fallback is the 2026-10-16 design: an ATM-side scheduled collector posts
images to a receiver, which writes object storage plus metadata. That is a
larger compliance scope (central face images, purge, access audit) and a
larger deployment (1,300 agents and queues). It is not being built now. The
metadata index and the audit model can be reused; the image store and its
lifecycle cannot.

## Rejected alternatives

* **Central upload receiver now** — larger compliance scope, new storage to
  purge, and the bank has not said it needs images after the ATM rotates
  them. Kept as a fallback, not the default.
* **SMB / WinRM / SSH** — ports are closed on both test ATMs; opening them for
  a monitoring feature is a network change with a larger blast radius.
* **`vfs.file.contents`** — text-only and truncated; a JPEG or zip cannot use
  it.
* **A general "browse any ATM file" panel** — a file-read primitive with no
  evidence scope. Only allow-listed image evidence, audited.
* **Filename card field as the transaction key** — contradicted by the
  22 October GRG NOCARD session.

## Historical appendix — 2026-10-16 discovery (superseded)

The first discovery concluded that TESTMACHINE had no camera and that the
cheap "journal carries an image reference" design was dead. Both conclusions
were superseded on 2026-10-22:

* NCR images were found in the ICAM day folders and daily zips.
* GRG images were found under `E:\pic\<YYYY>\<MM>\<DD>\`.
* For GRG, there is still **no transaction ID in the image filename**. The
  link remains timestamp + event, as recorded above. The NCR entry names are
  still being listed.

The 2026-10-16 probes are retained here because they explain why the earlier
plan expected a central receiver and why the current design does not.
