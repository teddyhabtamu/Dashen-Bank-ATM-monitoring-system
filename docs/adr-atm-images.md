# ADR: ATM camera images for fraud monitoring — deferred, discovery blocked

> **Status:** Deferred (2026-10-16). Mechanism is understood and buildable; the
> **data itself is unconfirmed**. Do not design against assumptions below —
> re-run the discovery in Phase 1 before committing.
>
> Recorded because this is expected to be built eventually. Revisit when the
> vendor answers the four questions in "Blocking unknowns".

## Motivation

BRD 2.6 (fraud rules) and the dispute-investigation use cases would benefit from
the image each ATM captures of the user on a successful transaction. Today
**NetXMS** provides that view. The goal is a first-class path in this portal,
rather than a second system operators must switch to during an investigation.

## What we measured (TESTMACHINE, 10.41.30.114, 2026-10-16)

| Probe | Result | Meaning |
|---|---|---|
| `vfs.dir.size[C:\ATM]` | `ZBX_NOTSUPPORTED` — no such directory | The commonly cited NCR image path does not exist here |
| `vfs.dir.size[C:\monitor]` | `6454` (KB) | Our own deployment directory; expected |
| Journal grep `cam\|image\|capture` | only `CAM 03.00.00` (×2) | **Firmware component-version banner, not a transaction image reference** |
| WMI device sweep (14 devices) | `ALRM, ALT_, CASH-01, FLT_, KEYB×2, MCRW, MDSW, MEEI, MISC, RPNT, RRLY, RSI_, SND_` | **No camera in the list** |

The device sweep enumerates attached USB devices, so an attached camera would
appear. Combined with `C:\ATM` being absent, the likely reading is that
**TESTMACHINE has no camera attached, or per-transaction capture is not enabled
on it.**

## What this rules out

**The cheap design is dead.** The plan was: if the journal carries an image
reference, show *"image available, ref XYZ"* and move no image at all. **It does
not carry one.**

Without a reference, associating an image with a transaction requires either
filename-encoded sequence numbers, or time-based nearest-match. **Time-matching
is unreliable precisely when it matters** — several transactions inside one
second is a common fraud pattern, and that is where "nearest" breaks down.

A wrong link in a fraud investigation is worse than no image: it puts the wrong
person's face next to an accusation.

## Why Zabbix cannot carry the images

| Constraint | Detail |
|---|---|
| `vfs.file.contents` returns **text** | A JPEG is binary |
| Zabbix text values truncate at **~64 KB** | ATM JPEGs are typically 20–80 KB and up |
| No arbitrary binary file item | Over the agent or otherwise |
| Trapper items do not help | Same value limits — and images would persist in the value table |

This is why EJ (text, 2 KB) works via `zabbix_get` and images never will. See
`docs/adr-ej-transport.md`.

## Recommended mechanism (once unblocked)

**Dedicated collector + receiver**, not Zabbix:

```
ATM (PowerShell scheduled task)
  → HTTPS POST
    → receiver on the monitoring host
      → object storage (images) / Postgres (metadata)
        → portal
```

- The receiver scales comfortably: 1,300 uploads/min ≈ **22/sec**
- **The ATM side is the real cost**: 1,300 deployments, each needing verification
- Needs a hard cap on the ATM-side queue — a branch returning after an outage
  will otherwise dump a backlog at once
- The same channel can later carry EJ and filesystem inventory, so it is not
  throwaway work

**Do not build it "generally".** Build it for one real payload and let the shape
emerge. Building a multi-purpose agent protocol before there are two payloads is
how you build a framework nobody uses.

## Blocking unknowns — ask the ATM vendor

1. Does `TESTMACHINE` have a camera attached?
2. Is per-transaction capture **enabled** in the terminal configuration?
3. Where are images written?
4. **Does the filename encode the transaction or sequence number?** ← *this one
   decides the design*
5. How long does the ATM retain images before rotating them?

Without answer 4 there is no defensible link between an image and a transaction.

## Compliance gates — these precede engineering

| Gate | Why |
|---|---|
| Lawful basis + **consent signage at the ATM** | Face images are biometric-adjacent personal data (GDPR / FDACC) |
| Retention period + **automatic purge** | Central copies must not become an unbounded store of customer faces |
| Access control + **audit of every view** | Who may see a customer's face is an audit question |
| PCI DSS | An image can capture a PAN or the PIN pad — masking logic in the parser is bypassed by nature |
| Least-privilege account, read-only endpoints | Blast radius: today a monitoring-host compromise yields metrics; with images it yields customer faces |

Compliance is a **bank decision that gates this**, not an engineering detail to
resolve later.

## Phasing

| Phase | Scope | Gate |
|---|---|---|
| 1 | **Discovery** — the five questions above | Answers 1 and 4 must be yes |
| 2 | Metadata-only filesystem inventory over the same channel | Validates the receiver before any binary payload |
| 3 | Image transfer to object storage | Compliance sign-off |
| 4 | Portal view, with access audit | — |

## Cheap interim alternative

Until then, the fraud workflow can be improved **without moving a single image**:
EJ already gives transaction type, timestamp, card, amount and response code.
What it cannot give is the person. Being explicit about that gap is better than a
half-working image panel.