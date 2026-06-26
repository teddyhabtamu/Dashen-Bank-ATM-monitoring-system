# Real ATM Onboarding Checklist

> Short, repeatable checklist for connecting one new real ATM to the
> monitoring system. This assumes the production server is already
> deployed and at least one real ATM has been onboarded before (i.e.
> the SNMP template, ISO 8583 gateway, and EJ pipeline already exist).
>
> For the full explanation behind each step — including how to
> discover SNMP OIDs, handle different EJ formats, and configure
> auto-discovery for the first time — see the **Production Migration
> Guide** (Sections 3–7). This document is the short version for
> repeat use.

---

## Before You Start — Information Needed

Get this from the ATM Hardware Team / Branch Network before beginning:

- [ ] ATM ID (e.g. `ATM-006`)
- [ ] Branch name, district, city, region
- [ ] GPS coordinates (latitude, longitude)
- [ ] IP address on the ATM network
- [ ] Terminal ID (matches ATM switch records)
- [ ] Vendor and model (e.g. NCR SelfServ 84)
- [ ] SNMP community string (or v3 credentials)
- [ ] Confirmation the ATM is powered on, networked, and processing transactions

---

## Path A — If Zabbix Auto-Discovery Is Already Configured

If the discovery rule and auto-registration action exist (see
Production Migration Guide Section 7), most of this happens
automatically:

- [ ] Confirm the ATM responds to the discovery rule's SNMP check —
      wait up to one scan cycle (default: 1 hour)
- [ ] Check **Configuration → Hosts** — the new ATM should appear
      automatically with the SNMP template linked
- [ ] If it hasn't appeared after one scan cycle, manually trigger a
      check: **Configuration → Discovery → [rule name] → Execute now**
      (or troubleshoot per `docs/troubleshooting.md`)
- [ ] Once it appears, rename the host to match your naming
      convention if auto-discovery only assigned an IP-based name
- [ ] Continue to **"Common Steps for Both Paths"** below

---

## Path B — Manual Host Creation (No Auto-Discovery Yet)

- [ ] **Configuration → Hosts → Create host**
- [ ] Host name: `ATM-0XX | <Vendor> | <Branch Name>`
- [ ] Host group: `Dashen Bank ATMs`
- [ ] Interfaces → Add → Type: `SNMP`, Port: `161`
- [ ] Set SNMP version and community string (or v3 credentials)
- [ ] Templates → Link template: `Dashen Bank ATM Hardware — SNMP (Real)`
- [ ] Click **Add**
- [ ] **Monitoring → Latest data** → filter by the new host → confirm
      values are populating within one polling interval
- [ ] If items show "Not supported" — check firewall (UDP 161) and
      community string; see `docs/troubleshooting.md`

---

## Common Steps for Both Paths

### 1. ATM Location Data

- [ ] If the Report Portal admin page exists (Priority 4 in
      `docs/local-progress-plan.md`): fill in the form at
      `/admin/atm` with the ATM's details
- [ ] If not yet built, insert manually:
  ```bash
  docker exec zabbix-db psql -U zabbix -d zabbix << 'SQL'
  INSERT INTO atm_locations VALUES
  ('ATM-0XX','<Branch Name>',
   '<District>','<City>','<Region>',
   <latitude>,<longitude>,'<Terminal ID>','<Vendor>','<Model>',
   '<install_date>','active')
  ON CONFLICT (atm_id) DO NOTHING;
  SQL
  ```
- [ ] Confirm the ATM appears correctly positioned on the Grafana
      geo-map
- [ ] Confirm it appears in the ATM Fleet Overview table with correct
      branch/district info

### 2. Electronic Journal (EJ) Logs

- [ ] Confirm the EJ collection method for this ATM (agent on ATM,
      shared folder, or SFTP push — see Production Migration Guide
      Section 4)
- [ ] If using an existing shipping method already configured,
      confirm new log files for this ATM land in the expected
      directory automatically
- [ ] If a new shipping path is needed, add it to `filebeat.yml` and
      restart Filebeat:
  ```bash
  sudo chown root:root filebeat.yml && sudo chmod 644 filebeat.yml
  docker compose up -d filebeat
  ```
- [ ] Search for the ATM's Terminal ID or branch name in OpenSearch Dashboards
      Discover — confirm entries appear with masked card numbers

### 3. Transactions (ISO 8583)

- [ ] No per-ATM gateway configuration is needed — the
      `iso8583-gateway` is switch-wide
- [ ] Confirm transactions for this Terminal ID begin appearing:
  ```sql
  SELECT * FROM atm_transactions
  WHERE terminal_id = '<Terminal ID>'
  ORDER BY recorded_at DESC LIMIT 10;
  ```
- [ ] Spot-check a few transactions (amount, status, timestamp)
      against the switch's own records if available

### 4. Alerts and Tickets

- [ ] Confirm triggers are active for the new host (inherited from
      the linked template — no per-host action needed)
- [ ] Test one alert path end-to-end: simulate or wait for a real
      threshold breach (e.g. low cash) and confirm:
  - [ ] Problem appears in **Monitoring → Problems**
  - [ ] Email notification received
  - [ ] SMS notification received (if severity is Disaster/High)
  - [ ] GLPI ticket auto-created with correct ATM/branch details

### 5. Dashboards

- [ ] Open the ATM Operations Centre dashboard in Grafana — confirm
      the new ATM appears in the Fleet Overview table and geo-map
- [ ] Open the drill-down dashboard, select the new ATM from the
      variable dropdown, confirm panels populate

### 6. Reports

- [ ] Generate a Transaction Summary report (any format) from the
      Report Portal, filtered to include the new ATM — confirm it
      appears with correct data

---

## Sign-Off

- [ ] All sections above completed
- [ ] ATM ID and onboarding date recorded in the ATM inventory
      tracking sheet
- [ ] Any issues encountered during onboarding documented (helps
      speed up the next one)

**Date onboarded:** ____________________
**Onboarded by:** ____________________
**Notes:**

---

## See Also

- **Production Migration Guide** — full explanation of SNMP OID
  discovery, EJ format handling, ISO 8583 parser adjustments, and
  auto-discovery setup (read once, before the first real ATM)
- `docs/architecture.md` — what each part of the system does
- `docs/troubleshooting.md` — fixes if any step above doesn't work
  as expected