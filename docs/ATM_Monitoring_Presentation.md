# Real-Time ATM Monitoring & Reporting System

**Dashen Bank**

IT Modernization & Project Management Department

October 2026

---

## Outline

**01** What is this system?

**02** Problems we face today

**03** How the system solves them

**04** Who uses it

**05** What is already done

**06** What is still left

**07** Decisions we need

**08** What "finished" looks like

**09** Demo

---

## 01 — What is this system?

### One screen for every ATM in the bank.

It tells us, at any moment:

- Is the ATM working right now — or is it down?
- How much cash is inside it?
- What went wrong, and who is fixing it?

Instead of asking each vendor's tool, we look in one place.

---

## 01 — What is this system? *(cont.)*

It watches the ATM from four sides:

| What we watch | Where the information comes from |
|---|---|
| **Hardware health** — doors, printer, card reader, dispenser | The ATM itself, read every 30 seconds (SNMP) |
| **Transactions** — withdrawals, balance checks, failures | The messages the ATM sends to the bank switch (ISO 8583) |
| **The ATM's own log** — every action it took | Electronic Journal (EJ) files, like a receipt book |
| **The network** — is the line up or down? | Network checks and link monitoring |

Then it **alerts people** and **makes reports** without anyone doing it by hand.

---

## 01 — What is this system? *(cont.)*

### The fleet it must cover

- **1,202 ATMs** across branches and districts
- **Two vendors** — NCR and GRG
- **Different network types** — fiber, MPLS, VSAT, GSM
- **Spread across the country**, so faults must be found remotely

> Today we are testing with a small pilot set.
> The same system will serve the full fleet — only the data source changes.

---

## 02 — Problems we face today

1. **No single view.** Each vendor has its own tool, so nobody sees the whole picture.

2. **We hear about faults from customers**, not from the system.

3. **Cash runs out without warning** — the first to notice is the customer at the machine.

4. **Reports are made by hand** in Excel, every day, by busy people.

5. **Finding old ATM records is slow.** A customer dispute can take hours of searching.

6. **No record of who changed what.** Changes are hard to trace and hard to audit.

---

## 02 — Problems we face today *(cont.)*

### What these problems cost us

- **Lost transactions** — a machine that is down earns nothing
- **Poor customer experience** — long queues and failed withdrawals
- **Wasted staff time** — manual reporting instead of fixing faults
- **Risk in audits** — no clear trail of changes and actions
- **Slow response** — we learn too late, so we lose more money

---

## 03 — How the system solves them

| Problem | What the system does |
|---|---|
| No single view | **One live dashboard** for the whole fleet |
| We hear from customers | **Automatic alerts** when a fault happens |
| Cash runs out | **Cash level tracking** and low-cash warnings |
| Manual reports | **Reports made and emailed automatically** |
| Slow record search | **Fast log search** across all ATM records |
| No trace of changes | **Audit trail** — every change is recorded |

---

## 03 — How the system solves them *(cont.)*

### What the system does, in six simple parts

1. **Live status** — see every ATM as online, offline, or in trouble

2. **Cash monitoring** — how much cash is left in each cassette

3. **Transaction monitoring** — count of successful and failed transactions

4. **Fault alerts** — when something breaks, a ticket is created automatically

5. **Log search** — find any past ATM action in seconds

6. **Dashboards and reports** — for daily work and for management

Plus: **adding a new ATM is a guided process**, not a manual setup job.

---

## 03 — How the system solves them *(cont.)*

### How it works — four simple steps

**1. COLLECT** — Tools read the ATMs every 30 seconds, read transactions as they happen, and collect the ATM log files.

**2. STORE** — All information is kept in a central database, with logs kept separately for fast searching.

**3. ANALYSE** — Rules and checks decide what is normal and what is a problem. Suspicious patterns and network faults are flagged too.

**4. SHOW** — People see it on dashboards, in reports, and as tickets in the helpdesk system.

> Nothing is done by hand at any step.

---

## 04 — Who uses it

### Management
KPIs, uptime, performance and vendor reports — without asking anyone to prepare them.

### Operations
A live wall of every ATM, with alarms when something goes wrong.

### Support and Engineers
The ticket tells them which ATM, which fault, and what was already tried.

### Administrators
User accounts, ATM records, settings, and the audit trail.

> Everyone sees the same truth, at the same time.

---

## 05 — What is already done *(1 of 3)*

### The foundation

- Two UAT servers are running, with a clean separation between monitoring data and reporting data
- Database backups are taken automatically, and the backup process has been proven in a drill
- **Roles and permissions** are enforced — people see only what they should
- **Every important change is recorded** — who, when, and what changed
- **Passwords are hidden** in the audit trail

---

## 05 — What is already done *(2 of 3)*

### Monitoring and alerts

- **Hardware monitoring** for NCR and GRG ATMs
- **68 alert rules** written and loaded — 38 for NCR, 30 for GRG
- **Live dashboards** — 7 dashboards, including a map view of ATM locations
- **Fault alerts create tickets automatically** in the helpdesk system
- Ticket lifecycle works end to end: raised, assigned, solved, closed
- **Anomaly detection** flags unusual transaction patterns
- **Network correlation** links network faults to transaction failures

---

## 05 — What is already done *(3 of 3)*

### Reports, logs and the portal

- **8 reports**, each available as Excel, PDF or CSV
  *(Management, Daily Outage, Transactions, Cash, Errors, Performance, Vendor, Availability)*

- **Daily outage report is emailed automatically** — proven working

- **Transaction monitoring** reads real ISO 8583 messages, with card numbers masked

- **Electronic Journal** logs are collected, understood for both vendors, searchable, and kept for 90 days

- **Report Portal** — ATM registry, guided ATM onboarding, fleet health check, cash load records, and user management

---

## 05 — What is already done *(cont.)*

### How we know it works

We did not only build it — we tested it on the UAT servers:

- A fault was raised and followed all the way to a solved ticket
- A daily report was generated and delivered by email, automatically
- Reports were checked against the real data, line by line
- The backup process was proven with a drill
- Alerts now reach the helpdesk system and close the loop when the fault clears

---

## 06 — What is still left

Three kinds of remaining work — each needs something different.

### A. Waiting on the bank's network and credentials

Real hardware data cannot flow until:

- The **firewall allows SNMP (port 161)** from the monitoring server to ATMs
- **SNMP community strings** are provided for each ATM vendor
- Two pilot ATMs are still not reporting for exactly these reasons

> This is not a software gap. The software is ready and waiting.

---

## 06 — What is still left *(cont.)*

### B. Waiting on decisions

These are choices for the bank, not build work:

- Mobile app for engineers — build it, or use the mobile-friendly web page?
- Are **cameras** and **power/UPS monitoring** in this phase?
- The BRD asks for updates every 5 seconds; the design polls every 30 seconds. Can we agree a **tiered target**?
- **High availability and encryption** for production — which design and budget?
- Do we need **Core Banking** and **Card System** connections at go-live?
- Which **SMS provider** will we use?

---

## 06 — What is still left *(cont.)*

### C. Build and preparation work

- **Bulk import of 1,202 ATMs** — the register must be loaded and cleaned
- **Cash depletion forecast** — predict when each ATM will run out
- **SMS alerts** — send faults to phones
- **Load testing** with the full fleet, to prove speed at scale
- **Training and handover** for the teams who will run it
- **Security cleanup** — rotate old credentials and add encrypted connections

---

## 07 — Decisions we need

| # | Decision | Why it matters |
|---|---|---|
| 1 | Mobile app: build, delay, or skip? | The BRD lists it as mandatory |
| 2 | Cameras and power monitoring: now or later? | Big scope and cost question |
| 3 | Update speed: accept 30 seconds for hardware? | The design polls every 30s |
| 4 | Production design: high availability and encryption | UAT is a single server today |
| 5 | Core Banking and Card System connections | Needed at go-live? |
| 6 | SMS provider, cash forecast, replenishment | Small builds, clear owners needed |
| 7 | Directory login and secure connections | Needs the IAM team |

> Without these answers, the remaining work cannot be scheduled.

---

## 08 — What "finished" looks like

When the project is complete:

- **All 1,202 ATMs are visible** and reporting on their own
- **We know about a fault before the customer does**
- **Cash is refilled before it runs out**
- **Reports arrive by themselves** — nobody builds them in Excel
- **Every fault has a ticket, an owner, and a recorded outcome**
- **Every change is traceable**, so audits are simple

---

## 09 — Demo

What we will show:

1. The **live dashboard** — every ATM on one screen
2. **Fleet health** — which ATMs are reporting and which are not
3. A **report** — generated in seconds, not hours
4. A **fault to ticket** — from alert to solved

---

## Thank you

**Questions and discussion**

*Real-Time ATM Monitoring & Reporting System*
Dashen Bank — IT Modernization & Project Management Department
