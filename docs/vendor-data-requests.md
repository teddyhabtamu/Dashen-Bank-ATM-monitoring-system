# Vendor Data Requests — ATM Monitoring (UAT → Production)
Date issued: 2026-10-14 · Owner: Bank management → vendors · Tech contact: monitoring team
Response asked: within 2 weeks per artifact (stagger acceptable; say which slips).

Framing (use in the cover note): requested as integration support under the
existing maintenance contracts — management interfaces the contracts'
MIB/XFS references describe, for consumption by the bank's monitoring
platform. Read-only, no changes to vendor systems.

## Letter 1 — NCR (SelfServ 34 test core + all fleet models)

1. **APTRA XFS Service Provider version matrix** — observed 3.02
   (`HKLM\SOFTWARE\XFS\SERVICE_PROVIDERS\CDM`, test core). Confirm per
   deployed model; state whether 3.20 (CWA 16374) or 3.30 (CWA 16926)
   object definitions apply per model.
2. **XFS MIB implementation statement** — which of CWA Parts 29–47 the
   devices serve over SNMP, per model; attach the MIB files. In particular:
   CDM SubDevice counters (`CULCount`/`CUPCount`, thresholds, logical
   status) — see item 4.
3. **Cassette note-count interface (highest value)** — which OID, file, or
   API reports notes-per-cassette on our models? If the MIB path is
   unserved, state the supported alternative (no open questions accepted:
   name the interface or state none exists).
4. **Official WMI value maps** — `NCR_DevicePart` and `NCR_Device`
   (`PartStatus`, `PMStatus`, `ReplenishmentStatus`, `DeviceStatus`)
   complete enumerations incl. values past 3; clarify the Replenishment
   text ("4 - Unknown") vs ValueMap ({0,1,2,3}) contradiction we observed.
5. **Supervisor / application-mode state surface** — which interface
   reports supervisor mode, out-of-service application state, reboot
   events per model.
6. **Journal export specification** — `EJDATA.LOG` record format
   reference for NDC journals (we parse it live; official grammar
   removes guesswork for dispute-grade parsing).
7. **SNMP access process** — community/credential provisioning per
   device and SNMP trap destination support (for event-driven alerting,
   Phase 2).

## Letter 2 — GRG (AD2600 test unit + all fleet models)

Same seven items, SNMP-first (no WMI on this platform):
1. XFS SP version matrix per model. 2. Standard-xfsMIB vs vendor-MIB
   statement **+ MIB files** (this single answer decides our whole GRG
   template strategy). 3. Cassette counts OID. 4. Not applicable (no
   WMI) — instead: status-code enumerations for the chosen MIB. 5–7 as
   for NCR (supervisor, EJ format for GRG journals, SNMP credentials).

## Tracking

| # | Artifact | Vendor | Asked | Due | Received | Notes |
|---|----------|--------|-------|-----|----------|-------|
| 1 | XFS version matrix | NCR/GRG | 2026-10-14 | +2w | ☐ | |
| 2 | MIB implementation + files | NCR/GRG | 2026-10-14 | +2w | ☐ | decides GRG template |
| 3 | Cassette-count interface | NCR/GRG | 2026-10-14 | +2w | ☐ | highest value |
| 4 | Status enumerations | NCR/GRG | 2026-10-14 | +2w | ☐ | |
| 5 | Supervisor/app-mode surface | NCR/GRG | 2026-10-14 | +2w | ☐ | |
| 6 | Journal format reference | NCR/GRG | 2026-10-14 | +2w | ☐ | |
| 7 | SNMP credential process | NCR/GRG | 2026-10-14 | +2w | ☐ | |

Rules: vendor paper describes design; our on-box reads describe reality.
Where they disagree, the box wins and the vendor owes an explanation.
No response by due date escalates through the maintenance contract owner.
