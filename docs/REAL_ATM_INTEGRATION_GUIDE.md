# Real ATM Integration Guide

## Dashen Bank ATM Monitoring System

**Version:** 1.0
**Last Updated:** July 2026
**Audience:** Beginner — step-by-step, no assumptions

---

## Table of Contents

1. [What This Guide Is About](#1-what-this-guide-is-about)
2. [What You Need Before Starting](#2-what-you-need-before-starting)
3. [Understanding the System Architecture](#3-understanding-the-system-architecture)
4. **[Step 0: Physical ATM Setup — How to Get Data from a Real ATM](#4-step-0-physical-atm-setup--how-to-get-data-from-a-real-atm)** (NEW)
5. [Step 1: Understand the ATM](#5-step-1-understand-the-atm)
6. [Step 2: Register the ATM in the Database](#6-step-2-register-the-atm-in-the-database)
7. [Step 3: Configure SNMP Monitoring](#7-step-3-configure-snmp-monitoring)
8. [Step 4: Configure Transaction Feed](#8-step-4-configure-transaction-feed)
9. [Step 5: Configure Electronic Journal (EJ) Log Collection](#9-step-5-configure-electronic-journal-ej-log-collection)
10. [Step 6: Configure Anomaly Detection](#10-step-6-configure-anomaly-detection)
11. [Step 7: Configure Network Correlation](#11-step-7-configure-network-correlation)
12. [Step 8: Test Everything](#12-step-8-test-everything)
13. [Step 9: Switch from Simulation to Production](#13-step-9-switch-from-simulation-to-production)
14. [Common Issues & How to Fix Them](#14-common-issues--how-to-fix-them)
15. [Quick Reference: All Commands](#15-quick-reference-all-commands)

---

## 1. What This Guide Is About

You have a monitoring system that currently runs with **simulated ATMs**. The system generates fake transactions, fake hardware metrics, and fake Electronic Journal (EJ) logs. Everything works, but it's all fake data.

Now you have **one real ATM** that you want to connect to the system. This guide explains **exactly** what you need to do, step by step, to get that real ATM working with your system.

**The guide is organized in this order:**
1. **Step 0** — Physical ATM setup (how to physically connect and get data from the ATM)
2. **Step 1** — Understand the ATM (gather all the information you need)
3. **Steps 2-7** — Configure each component of the monitoring system
4. **Step 8** — Test everything
5. **Step 9** — Switch from simulation to production

**What we're NOT doing:**
- We are NOT removing the simulation
- We are NOT breaking anything that works
- We are NOT changing the database schema

**What we ARE doing:**
- Adding a real ATM to the existing system
- Connecting the real ATM's data to the same monitoring pipeline
- The real ATM will coexist with the simulated ATMs

---

## 2. What You Need Before Starting

### 2.1 — Physical Requirements

| Item | Why You Need It |
|------|-----------------|
| **One real ATM** | Obviously |
| **ATM IP address** | The ATM must have a network IP address that your monitoring server can reach |
| **SNMP community string** | A password for SNMP access (like `dashen_atm` or `public`) |
| **Terminal ID** | The ATM's physical terminal ID (e.g., `TID001`, `TID002`) |
| **Vendor name** | `NCR` or `GRG` (the two vendors Dashen Bank uses) |
| **Branch name** | Where the ATM is located (e.g., "Addis Ababa Main Branch") |
| **District** | The district code (e.g., `NAD`, `SAD`, `EAD`, `WAD`, `ADAMA`, etc.) |
| **City** | The city (e.g., "Addis Ababa") |
| **Region** | The region (e.g., "Addis Ababa") |

### 2.2 — Software Requirements

| Item | How to Check |
|------|--------------|
| **Docker running** | `docker ps` should show containers |
| **PostgreSQL accessible** | `docker exec zabbix-db psql -U zabbix -c "SELECT 1"` should return `1` |
| **Zabbix Server accessible** | `curl http://localhost:8080/api_jsonrpc.php` should return JSON |
| **Grafana accessible** | `curl http://localhost:3002` should return HTML |
| **Report Portal accessible** | `curl http://localhost:8888/health` should return `{"status":"ok"}` |

### 2.3 — Network Requirements

Your monitoring server must be able to reach the ATM's IP address on port 161 (SNMP). Test this:

```bash
# Replace 192.168.1.100 with your ATM's IP
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.37513.1.1.0
```

If this returns a value, the ATM is reachable. If it times out, check the network.

---

## 3. Understanding the System Architecture

Before we start, let's understand how the system works. Here's the big picture:

```
┌─────────────────────────────────────────────────────────────┐
│                    MONITORING SYSTEM                         │
│                                                             │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐ │
│  │  Zabbix   │   │  Grafana │   │ OpenSearch│   │  GLPI    │ │
│  │  Server   │   │Dashboards│   │ Dashboards│   │ Tickets  │ │
│  └────┬─────┘   └────┬─────┘   └────┬─────┘   └────┬─────┘ │
│       │              │              │              │         │
│       └──────────────┴──────────────┴──────────────┘         │
│                          │                                   │
│                   ┌──────┴──────┐                            │
│                   │ PostgreSQL  │                            │
│                   │   Database  │                            │
│                   └──────┬──────┘                            │
│                          │                                   │
│       ┌──────────────────┼──────────────────┐                │
│       │                  │                  │                │
│  ┌────┴─────┐   ┌───────┴──────┐   ┌───────┴──────┐         │
│  │ Transaction│   │   Anomaly   │   │   Network    │         │
│  │  Engine   │   │  Detector   │   │  Correlator  │         │
│  │(simulated)│   │ (reads DB)  │   │  (reads DB)  │         │
│  └────┬─────┘   └──────────────┘   └──────────────┘         │
│       │                                                      │
│  ┌────┴─────┐   ┌──────────────┐   ┌──────────────┐         │
│  │SIM Engine│   │   EJ Engine  │   │  State Mgr   │         │
│  │(SNMP sim)│   │ (EJ logs)   │   │ (reads HTTP) │         │
│  └──────────┘   └──────────────┘   └──────────────┘         │
│                                                             │
└─────────────────────────────────────────────────────────────┘
                          │
                          │  (Simulation Mode)
                          │  Only 5 ATMs: ATM-001 to ATM-005
                          │
                          ▼
```

**How the simulation works:**
1. **`sim_engine.py`** — Creates HTTP servers that simulate SNMP metrics (cash levels, card reader status, etc.)
2. **`txn_engine.py`** — Generates fake transactions and writes them to the database
3. **`ej_engine.py`** — Generates fake Electronic Journal logs
4. **`state_manager.py`** — Reads the HTTP servers to determine each ATM's state (IN_SERVICE, OFFLINE, etc.)
5. **`anomaly_detector.py`** — Scans the database for suspicious patterns
6. **`network_correlator.py`** — Correlates network events with transaction failures

**What changes with a real ATM:**
1. **SNMP monitoring** — Instead of reading from simulated HTTP servers, Zabbix reads from the real ATM via SNMP
2. **Transaction feed** — Instead of fake transactions, the real ATM sends real ISO 8583 messages
3. **EJ logs** — Instead of fake logs, the real ATM writes real Electronic Journal logs
4. **State management** — Instead of reading from simulated HTTP servers, the state manager reads from Zabbix (which polls the real ATM)

**What stays the same:**
- The database schema (no changes needed)
- The anomaly detection logic (it reads from the same `atm_transactions` table)
- The network correlation logic (it reads from the same tables)
- The Grafana dashboards (they read from the same database)
- The Report Portal (it reads from the same database)
- The GLPI integration (it reads from Zabbix triggers)

---

## 4. Step 0: Physical ATM Setup — How to Get Data from a Real ATM

This is the most important section. It explains **exactly** what happens when you physically connect to an ATM, and how to get the data you need.

### 4.1 — What You Physically Need

| Item | Why You Need It | Where to Get It |
|------|-----------------|-----------------|
| **Ethernet cable** | Connect ATM to your network | Any electronics store |
| **Laptop/monitor** | To see the ATM's display and access its menus | You already have one |
| **Ethernet port/switch** | To connect the ATM to your network | Your network infrastructure |
| **ATM key** | To open the ATM cabinet (for physical access) | ATM technician |
| **SNMP tool** | To test SNMP connectivity | Install `snmpget` on your laptop |

**Install SNMP tools on your laptop:**
```bash
# Ubuntu/Debian
sudo apt-get install snmp snmp-mibs-downloader

# CentOS/RHEL
sudo yum install net-snmp-utils

# Windows (download from http://www.snmpsoft.com/)
```

### 4.2 — What an ATM Actually Is

An ATM is a **small computer** with:
- **CPU and memory** (like a regular computer)
- **Network interface** (Ethernet port)
- **Cash dispensing mechanism** (cassettes that hold banknotes)
- **Card reader** (reads bank cards)
- **Receipt printer** (prints receipts)
- **Display screen** (shows information to the user)
- **Keypad** (for PIN entry)
- **Camera** (for security)
- **UPS** (uninterruptible power supply)

The ATM runs **proprietary software** (NCR APTRA for NCR, or GRG's own software). This software has:
- **SNMP agent** — responds to SNMP queries (like a monitoring agent)
- **Transaction processor** — handles financial transactions
- **EJ logger** — writes Electronic Journal logs
- **Network interface** — connects to the ATM switch and the bank's network

### 4.3 — How to Physically Connect to the ATM

**Step 1: Find the ATM's network port**
- Open the ATM cabinet (using the ATM key)
- Look for the Ethernet port (usually on the back or bottom)
- Connect an Ethernet cable from the ATM to your network switch/router

**Step 2: Power on the ATM**
- If the ATM is off, turn it on (power switch is usually inside the cabinet)
- Wait for the ATM to boot up (this takes 1-2 minutes)
- The ATM's display should show a welcome screen or idle screen

**Step 3: Find the ATM's IP address**
- There are several ways to find the ATM's IP address (see Section 4.4)

### 4.4 — How to Find the ATM's IP Address

**Method 1: Check the ATM's display**
- Some ATMs show the IP address on the display screen
- Look for "Network Settings" or "Configuration" in the ATM's menu
- Use the keypad to navigate

**Method 2: Check the ATM's configuration menu**
- Most ATMs have a hidden configuration menu
- Press a key combination to access it (usually `F1`, `F2`, `Esc`, or `Menu`)
- Look for "Network" or "TCP/IP" settings
- The IP address should be displayed there

**Method 3: Use a network scanner**
- Install `nmap` on your laptop: `sudo apt-get install nmap`
- Scan your network for ATMs: `nmap -sn 192.168.1.0/24`
- Look for devices that respond to port 161 (SNMP)

**Method 4: Check the ATM's MAC address**
- Each ATM has a unique MAC address (printed on a label inside the cabinet)
- Check your router's DHCP client list for the MAC address
- The IP address should be listed there

**Method 5: Use SNMP to discover the ATM**
- If you know the SNMP community string, you can scan the network for SNMP agents
- Run: `snmpwalk -v2c -c public 192.168.1.0/24 1.3.6.1.2.1.1.1.0`
- This will return the system description for each SNMP agent

### 4.5 — How to Get the SNMP Community String

The SNMP community string is like a password for SNMP access. It's configured on the ATM by the vendor.

**Where to find it:**
1. **Ask the ATM vendor** — They should know the community string
2. **Check the ATM's configuration** — Use the ATM's configuration menu
3. **Check the bank's documentation** — The bank should have the community string
4. **Try common defaults:**
   - `public` (most common default)
   - `private`
   - `dashen_atm` (if Dashen Bank configured it)
   - `community`

**Test the community string:**
```bash
# Replace 192.168.1.100 with your ATM's IP
# Replace "public" with your community string
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.37513.1.1.0
```

If this returns a value (like `1`), the community string is correct. If it says "timeout" or "no such object", the community string is wrong.

### 4.6 — How to Get the Terminal ID

The terminal ID is the ATM's unique physical identifier. It's usually:
- Printed on a label inside the ATM cabinet
- Displayed on the ATM's screen
- Configured in the ATM's software

**How to find it:**
1. **Check the ATM's display** — Look for "Terminal ID" or "TID"
2. **Check the ATM's configuration menu** — Navigate to "Settings" or "Configuration"
3. **Check the ATM's label** — Look inside the cabinet for a label with the terminal ID
4. **Ask the ATM vendor** — They should know the terminal ID

**Format:** Usually something like `TID001`, `TID002`, `ATM001`, `ATM002`, or just a number like `001`, `002`.

### 4.7 — How to Get the Vendor Information

The vendor is the company that manufactured the ATM. For Dashen Bank, there are two vendors:
- **NCR** (formerly NCR Corporation) — American company
- **GRG** (GRG Banking Equipment Co., Ltd.) — Chinese company

**How to find the vendor:**
1. **Check the ATM's label** — Look inside the cabinet for the vendor's name or logo
2. **Check the ATM's display** — Some ATMs show the vendor name on the screen
3. **Check the ATM's model** — Look for the model number (e.g., `SelfServ 34` for NCR, `H22N` for GRG)
4. **Ask the ATM vendor** — They should know the vendor

**How to determine the vendor from the model:**
- **NCR models:** `SelfServ 34`, `SelfServ 27`, `SelfServ 28`, `SelfServ 34`, `SelfServ 56`, `SelfServ 66`, `SelfServ 84`, `SelfServ 88`
- **GRG models:** `H22N`, `H22N-2`, `BRM9`, `BRM9-2`, `CRS 2050`

### 4.8 — How to Test SNMP Connectivity

Once you have the ATM's IP address and SNMP community string, test SNMP connectivity:

**Basic test:**
```bash
# Replace 192.168.1.100 with your ATM's IP
# Replace "public" with your community string
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.37513.1.1.0
```

**If NCR ATM, test these OIDs:**
```bash
# ATM status
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.37513.1.1.0

# Cassette 1 notes
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.37513.1.2.0

# Cassette 2 notes
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.37513.1.3.0

# Cash jam
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.37513.1.7.0

# Card reader
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.37513.2.1.0

# Network link
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.37513.7.1.0

# Temperature
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.37513.4.3.0
```

**If GRG ATM, test these OIDs:**
```bash
# ATM status
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.51234.1.1.0

# Cash module 1
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.51234.2.1.0

# Cash jam
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.51234.2.5.0

# Card unit
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.51234.3.1.0

# Network link
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.51234.8.1.0
```

**What to expect:**
- If SNMP works, you'll see a value returned (like `1`, `2500`, `25`, etc.)
- If SNMP doesn't work, you'll see "timeout" or "no such object"
- If the OID doesn't exist, you'll see "no such object" (this is normal — not all ATMs support all OIDs)

### 4.9 — How to Get All OIDs from the ATM

To discover all OIDs the ATM supports, use `snmpwalk`:

```bash
# Walk all OIDs under the NCR enterprise OID
snmpwalk -v2c -c public 192.168.1.100 1.3.6.1.4.1.37513

# Walk all OIDs under the GRG enterprise OID
snmpwalk -v2c -c public 192.168.1.100 1.3.6.1.4.1.51234

# Walk the entire SNMP tree (this might take a while)
snmpwalk -v2c -c public 192.168.1.100 1.3.6.1
```

This will give you a list of all OIDs the ATM supports. You can then use this to create a custom Zabbix template for your specific ATM.

### 4.10 — How to Get the EJ Logs

Electronic Journal (EJ) logs are detailed records of every transaction at the ATM. They're stored on the ATM's local storage or on a central server.

**How to get EJ logs:**

**Method 1: Access the ATM's file system (via SSH or Telnet)**
- Some ATMs allow SSH or Telnet access
- Use the ATM's IP address to connect
- Look for the EJ log directory (usually `/var/log/atm-ej/` or `/opt/atm/ej/`)

**Method 2: Use the ATM's EJ export function**
- Some ATMs have an EJ export function in the configuration menu
- Use the keypad to navigate to "EJ Export" or "Journal Export"
- The ATM will export the EJ logs to a USB drive or network share

**Method 3: Use the ATM's web interface**
- Some ATMs have a web interface
- Open a web browser and go to `http://<ATM-IP-address>`
- Look for "EJ Logs" or "Journal" in the menu

**Method 4: Use the ATM's API**
- Some ATMs have an API for accessing EJ logs
- Use the API to fetch the EJ logs
- This is the most reliable method for production

**EJ log format (NCR):**
```
2026-07-29 10:30:00 | ATM-006 | TID006 | TXN | WITHDRAWAL | SEQ=123456 | CARD=************1234 | AMOUNT=5000.00 | CURRENCY=ETB | STATUS=APPROVED | AUTH=654321
```

**EJ log format (GRG):**
```
2026-07-29 10:30:00 | ATM-006 | TID006 | VENDOR=GRG | TXN_CODE=WITHDRAWAL | SEQ=123456 | ACCT_NO=************1234 | AMOUNT=5000.00 | CURRENCY=ETB | RESP_CODE=APPROVED
```

### 4.11 — How to Get the Transaction Feed

The transaction feed is how transactions from the ATM get into the system. There are two ways:

**Method 1: Via the ATM switch (ISO 8583)**
- The ATM sends transactions to the ATM switch (a banking switch that routes transactions)
- The ATM switch sends transactions to the monitoring system via ISO 8583 protocol
- This is the production method

**Method 2: Via the ATM's API**
- Some ATMs have an API for accessing transaction data
- Use the API to fetch the transaction data
- This is a simpler method for testing

**How to configure the transaction feed:**
1. **Set up the ISO 8583 gateway** — The `iso8583_gateway.py` service listens for ISO 8583 messages
2. **Connect the ATM switch** — The ATM switch needs to send ISO 8583 messages to the gateway
3. **Test the connection** — Use `nc -zv <gateway-ip> 9876` to test TCP connectivity

### 4.12 — How to Get the ATM's Current State

The ATM's current state is determined by the `state_manager.py` service. It reads the ATM's SNMP metrics and determines the state.

**How to check the ATM's current state:**
```bash
# Check the database for the ATM's state
docker exec -it zabbix-db psql -U zabbix -c \
  "SELECT atm_id, state, previous_state, state_changed_at FROM atm_current_state WHERE atm_id = 'ATM-006';"
```

**State values:**
- `IN_SERVICE` — ATM is working normally
- `OUT_OF_SERVICE` — ATM is out of service
- `OUT_OF_CASH` — ATM has no cash
- `HARDWARE_FAULT` — ATM has a hardware fault
- `OFFLINE` — ATM is not reachable
- `UNREACHABLE` — ATM's SNMP agent is not responding
- `IN_SUPERVISOR` — ATM is in supervisor mode
- `UNKNOWN` — State cannot be determined

### 4.13 — Complete Data Access Summary

Here's a complete summary of how to get data from a real ATM:

| Data Source | How to Get It | Protocol | Port | Example Command |
|------------|--------------|----------|------|----------------|
| **ATM IP Address** | Check ATM display/config | — | — | Look at ATM screen |
| **SNMP Community String** | Check ATM config / ask vendor | — | — | Ask vendor |
| **Terminal ID** | Check ATM label/display | — | — | Look at ATM |
| **Vendor** | Check ATM label/model | — | — | Look at ATM |
| **Hardware Metrics** | SNMP GET | UDP | 161 | `snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.37513.1.1.0` |
| **Transactions** | ISO 8583 via switch | TCP | 9876 | `nc -zv <switch-ip> 9876` |
| **EJ Logs** | File system / API | HTTP/File | varies | `ls -la /var/log/atm-ej/` |
| **ATM State** | Read from database | SQL | 5432 | `SELECT * FROM atm_current_state WHERE atm_id = 'ATM-006';` |
| **Network Metrics** | SNMP GET | UDP | 161 | `snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.37513.7.1.0` |

### 4.14 — What Happens After You Get the Data

Once you have the ATM's IP, SNMP community, terminal ID, and vendor, you can:

1. **Register the ATM in the database** (Step 2)
2. **Configure Zabbix to monitor it** (Step 3)
3. **Configure the transaction feed** (Step 4)
4. **Configure EJ log collection** (Step 5)
5. **Test everything** (Step 8)

### 4.15 — Troubleshooting Physical ATM Issues

| Issue | Possible Cause | Fix |
|-------|---------------|-----|
| ATM not powered on | Power switch off, power cable disconnected | Check power switch and cables |
| ATM not reachable via network | Ethernet cable disconnected, wrong IP | Check Ethernet cable, verify IP |
| SNMP not responding | Wrong community string, firewall blocking | Try different community strings, check firewall |
| No EJ logs | EJ not configured, EJ service not running | Configure EJ, check ATM's EJ settings |
| No transactions | ATM switch not configured | Configure ATM switch, check network |

---

## 5. Step 1: Understand the ATM

Before you do anything, you need to know **exactly** what ATM you're connecting. If you haven't already, read **Step 0** (Physical ATM Setup) first — it explains how to physically connect to the ATM and get the information you need.

### 4.1 — Gather ATM Information

Fill in this table with your ATM's details:

| Field | Your ATM's Value | Example |
|-------|------------------|---------|
| ATM ID | `ATM-006` | `ATM-006` (or whatever you want to call it) |
| Vendor | `NCR` or `GRG` | `NCR` |
| Terminal ID | `TID006` | `TID006` (physical terminal ID) |
| IP Address | `192.168.1.100` | `192.168.1.100` |
| SNMP Community | `dashen_atm` | `dashen_atm` |
| Branch | `Bole International Branch` | `Bole International Branch` |
| District | `EAD` | `EAD` (East Addis Ababa) |
| City | `Addis Ababa` | `Addis Ababa` |
| Region | `Addis Ababa` | `Addis Ababa` |

### 4.2 — Verify SNMP Access

The ATM must respond to SNMP queries. Run this from your monitoring server:

```bash
# Test basic SNMP connectivity
snmpget -v2c -c dashen_atm 192.168.1.100 1.3.6.1.4.1.37513.1.1.0

# If NCR ATM, try these OIDs:
snmpget -v2c -c dashen_atm 192.168.1.100 1.3.6.1.4.1.37513.1.1.0   # ATM status
snmpget -v2c -c dashen_atm 192.168.1.100 1.3.6.1.4.1.37513.1.2.0   # Cassette 1

# If GRG ATM, try these OIDs:
snmpget -v2c -c dashen_atm 192.168.1.100 1.3.6.1.4.1.51234.1.1.0   # ATM status
snmpget -v2c -c dashen_atm 192.168.1.100 1.3.6.1.4.1.51234.2.1.0   # Cash module 1
```

**If SNMP works:** You'll see a value returned (like `1` or `2500`).
**If SNMP doesn't work:** Check the network, firewall, and SNMP community string.

### 4.3 — Understand What OIDs the ATM Supports

Each ATM vendor has its own set of OIDs (Object Identifiers). These are like addresses for each metric the ATM can report.

**NCR OIDs** (enterprise OID: `1.3.6.1.4.1.37513`):
- `1.1.0` — ATM status (1=OK, 2=Fault, 3=Jam, 4=Supervisor)
- `1.2.0` — Cassette 1 notes count
- `1.3.0` — Cassette 2 notes count
- `1.4.0` — Cassette 3 notes count
- `1.5.0` — Cassette 4 notes count
- `1.7.0` — Cash jam (0=No, 1=Yes)
- `2.1.0` — Card reader status (1=OK, 2=Fault)
- `7.1.0` — Network link status (1=Up, 2=Down)

**GRG OIDs** (enterprise OID: `1.3.6.1.4.1.51234`):
- `1.1.0` — ATM status (1=OK, 2=Fault, 3=Jam)
- `2.1.0` — Cash module 1 notes count
- `2.2.0` — Cash module 2 notes count
- `2.3.0` — Cash module 3 notes count
- `2.5.0` — Cash jam (0=No, 1=Yes)
- `3.1.0` — Card unit status (1=OK, 2=Fault)
- `8.1.0` — Network link status (1=Up, 2=Down)

**Important:** The current system uses a **synthetic OID root** `1.3.6.1.4.1.99999` for simulation. The real ATMs use their **vendor's real OID root**. This is one of the most important differences between simulation and production.

---

## 6. Step 2: Register the ATM in the Database

The database is the heart of the system. Every ATM must be registered in the `atm_locations` table. This is how the simulation engines know about the ATM, and how the monitoring system identifies it.

### 5.1 — Open the Database

```bash
# Connect to the PostgreSQL database
docker exec -it zabbix-db psql -U zabbix
```

### 5.2 — Insert the ATM

Run this SQL command to add your real ATM:

```sql
INSERT INTO atm_locations (
    atm_id, branch, district, city, region,
    terminal_id, vendor, model, install_date,
    status, snmp_community, snmp_version,
    ej_log_format, atm_name, ip_address
) VALUES (
    'ATM-006',                    -- atm_id: unique identifier for this ATM
    'Bole International Branch',  -- branch: where the ATM is located
    'EAD',                        -- district: district code
    'Addis Ababa',                -- city: city name
    'Addis Ababa',                -- region: region name
    'TID006',                     -- terminal_id: physical terminal ID
    'NCR',                        -- vendor: NCR or GRG (must match one of these)
    'SelfServ 34',                -- model: ATM model name
    '2026-07-29',                 -- install_date: when the ATM was installed
    'active',                     -- status: 'active' (or 'inactive' to disable)
    'dashen_atm',                 -- snmp_community: SNMP community string
    'v2c',                        -- snmp_version: SNMP version (v2c)
    'NCR_APTRA',                  -- ej_log_format: EJ log format (NCR_APTRA or GRG)
    'Bole International Branch ATM', -- atm_name: display name
    '192.168.1.100'               -- ip_address: real ATM IP
);
```

### 5.3 — Verify the Insert

```sql
SELECT atm_id, branch, vendor, ip_address, status, sim_port
FROM atm_locations
WHERE atm_id = 'ATM-006';
```

You should see your ATM in the results.

### 5.4 — Assign a Simulator Port

The simulation system needs a port for each ATM. The `sim_port` column is automatically assigned by the `common.py` module. But you can also assign it manually:

```sql
-- Assign the next available port
UPDATE atm_locations
SET sim_port = (SELECT COALESCE(MAX(sim_port), 1160) + 1 FROM atm_locations)
WHERE atm_id = 'ATM-006';
```

### 5.5 — Verify Port Assignment

```sql
SELECT atm_id, sim_port FROM atm_locations WHERE atm_id = 'ATM-006';
```

The `sim_port` should be a number between 1161 and 2500.

---

## 7. Step 3: Configure SNMP Monitoring

Now we need to tell Zabbix how to monitor this ATM via SNMP. Zabbix will poll the ATM every 30 seconds to get hardware metrics.

### 6.1 — What Happens Behind the Scenes

When you register the ATM in the database and run the sync script, Zabbix will:
1. Create a host for the ATM
2. Assign the correct template (NCR or GRG)
3. Configure SNMP polling

### 6.2 — Run the Sync Script

The sync script reads all ATMs from the database and registers them in Zabbix:

```bash
# Dry run (see what would happen, without making changes)
python3 scripts/sync_atms_to_zabbix.py

# Apply the changes (register ATMs in Zabbix)
python3 scripts/sync_atms_to_zabbix.py --apply

# If templates are not imported yet, add --import-templates
python3 scripts/sync_atms_to_zabbix.py --apply --import-templates
```

### 6.3 — Verify in Zabbix

Open Zabbix in your browser: `http://localhost:8080`

1. Log in: `Admin` / `zabbix`
2. Go to **Configuration → Hosts**
3. Search for your ATM (e.g., `ATM-006`)
4. Check that the host exists and has the correct template assigned
5. Check that the SNMP interface shows the correct IP address

### 6.4 — What the Template Does

The NCR template (`Dashen Bank ATM Hardware`) and GRG template (`Dashen Bank ATM Hardware - GRG`) define:

| What | How |
|------|-----|
| **SNMP items** | Polls OIDs every 30 seconds (cash levels, card reader, network, etc.) |
| **Triggers** | Alerts when something goes wrong (cash empty, card jam, network down, etc.) |
| **Graphs** | Shows trends over time (cash levels, transaction counts, etc.) |
| **Discovery** | No — templates are per-ATM, not auto-discovered |

### 6.5 — Test SNMP Monitoring

After registering the ATM, wait 1-2 minutes for Zabbix to start polling. Then check:

1. **In Zabbix UI** → **Monitoring → Hosts** → Find your ATM → Check **ZBX** icon is green
2. **In Zabbix UI** → **Monitoring → Latest Data** → Filter by host → See the latest values

If the ZBX icon is red, the ATM is not reachable via SNMP. Check:
- Network connectivity
- SNMP community string
- Firewall rules

---

## 8. Step 4: Configure Transaction Feed

The transaction feed is how transactions from the ATM get into the database. There are two modes:

- **Simulation mode** (current): `txn_engine.py` generates fake transactions
- **Production mode**: `iso8583_gateway.py` receives real transactions from the ATM switch

### 7.1 — Understanding the Transaction Flow

```
Real ATM → ATM Switch → ISO 8583 Gateway → PostgreSQL (atm_transactions table)
```

The ATM sends transactions in ISO 8583 format (a binary message format used in banking). The ISO 8583 gateway parses these messages and writes them to the database.

### 7.2 — What You Need

To receive real transactions, you need:
1. **ATM switch IP address** — The switch that connects the ATM to your network
2. **Switch port** — Usually 9876 (or whatever your switch uses)
3. **ISO 8583 message format** — The switch sends messages in ISO 8583 format

### 7.3 — Configure the Gateway

The ISO 8583 gateway is in `camel/iso8583_gateway.py`. It has two modes:

**Simulation mode** (default, `MODE=simulation`):
- Generates fake ISO 8583 transactions
- Writes them to `atm_transactions` table
- Source tag: `ISO8583_SIM`

**Production mode** (`MODE=tcp`):
- Listens on a TCP port for real ATM switch connections
- Parses incoming ISO 8583 messages
- Writes them to `atm_transactions` table
- Source tag: `ISO8583_REAL`

### 7.4 — How to Switch to Production Mode

**IMPORTANT:** For now, keep the simulation running. When you're ready to connect the real ATM switch, follow these steps:

1. **Set environment variables** in `docker-compose.yml` (or `.env`):
   ```yaml
   iso8583-gateway:
     environment:
       MODE: tcp
       SWITCH_HOST: 0.0.0.0
       SWITCH_PORT: "9876"
   ```

2. **Stop the simulation transaction engine** (optional — you can keep both running):
   ```bash
   docker compose stop atm-txn-engine
   ```

3. **Restart the gateway**:
   ```bash
   docker compose restart iso8583-gateway
   ```

4. **Connect the ATM switch** to the gateway's TCP port (9876)

### 7.5 — What Happens When a Real Transaction Arrives

1. The ATM switch sends a binary ISO 8583 message to the gateway
2. The gateway parses the message (MTI, bitmap, fields)
3. The gateway maps the fields to the database schema:
   - Field 2 (PAN) → `card_masked`
   - Field 3 (Processing code) → `txn_type`
   - Field 4 (Amount) → `amount`
   - Field 11 (STAN) → `seq_number`
   - Field 38 (Auth code) → `auth_code`
   - Field 39 (Response code) → `status`
   - Field 41 (Terminal ID) → `terminal_id`
4. The gateway writes the transaction to `atm_transactions`
5. The anomaly detector picks up the transaction and checks for anomalies

### 7.6 — Important: The ATM ID Must Match

When the gateway receives a transaction, it needs to know which ATM it came from. It uses the **terminal ID** (field 41) to look up the ATM in the database.

**Make sure the terminal ID in the ISO 8583 message matches the terminal ID in the database.**

For example, if your ATM sends `TID006` in field 41, the database must have an ATM with `terminal_id = 'TID006'`.

---

## 9. Step 5: Configure Electronic Journal (EJ) Log Collection

Electronic Journal (EJ) logs are detailed records of every transaction at the ATM. They're used for dispute investigation and audit trails.

### 8.1 — What Happens Behind the Scenes

```
Real ATM → EJ Log File → Filebeat → OpenSearch → OpenSearch Dashboards
```

The ATM writes EJ logs to a file (e.g., `/var/log/atm-ej/ATM-006.log`). Filebeat reads this file and sends it to OpenSearch. OpenSearch Dashboards lets you search the logs.

### 8.2 — Two Approaches

**Approach 1: Use the simulation EJ engine (for now)**
The simulation EJ engine (`ej_engine.py`) already generates EJ logs for all ATMs in the database. Since your real ATM is registered in the database, it will automatically get simulated EJ logs. This is fine for testing.

**Approach 2: Connect to real EJ logs (when ready)**
When you're ready to receive real EJ logs, you need to:
1. Set up a file share on the ATM (or use the ATM's built-in EJ export)
2. Configure Filebeat to read the EJ log file
3. Parse the EJ log format (NCR APTRA or GRG format)

### 8.3 — EJ Log Format

**NCR format** (pipe-delimited):
```
2026-07-29 10:30:00 | ATM-006 | TID006 | TXN | WITHDRAWAL | SEQ=123456 | CARD=************1234 | AMOUNT=5000.00 | CURRENCY=ETB | STATUS=APPROVED | AUTH=654321
```

**GRG format** (pipe-delimited):
```
2026-07-29 10:30:00 | ATM-006 | TID006 | VENDOR=GRG | TXN_CODE=WITHDRAWAL | SEQ=123456 | ACCT_NO=************1234 | AMOUNT=5000.00 | CURRENCY=ETB | RESP_CODE=APPROVED
```

### 8.4 — Filebeat Configuration

The current `filebeat.yml` reads from `/var/log/atm-ej/*.log`. If your real ATM writes EJ logs to a different location, you need to update the Filebeat config:

```yaml
filebeat.inputs:
- type: log
  enabled: true
  paths:
    - /var/log/atm-ej/*.log
  fields:
    source: atm_ej
```

### 8.5 — Verifying EJ Logs

1. Check that the EJ log file exists: `ls -la ej-logs/`
2. Check Filebeat logs: `docker logs filebeat`
3. Check OpenSearch: `curl http://localhost:9200/_cat/indices?v`
4. Open OpenSearch Dashboards: `http://localhost:5601`
5. Go to **Discover** and search for your ATM (e.g., `ATM-006`)

---

## 10. Step 6: Configure Anomaly Detection

The anomaly detector automatically scans transactions for suspicious patterns. It doesn't need any configuration — it reads from the `atm_transactions` table and detects anomalies.

### 10.1 — What It Detects

The anomaly detector has 5 rules:

| Rule | What It Detects | Threshold |
|------|----------------|-----------|
| **VELOCITY** | Same card, 3+ withdrawals in 10 minutes | `VELOCITY_LIMIT=3`, `VELOCITY_WINDOW=10` |
| **FAILURE_SPIKE** | ATM failure rate > 40% in 15 minutes | `FAILURE_THRESHOLD=0.4`, `FAILURE_WINDOW=15` |
| **LARGE_TXN** | Single withdrawal > ETB 8,000 | `LARGE_TXN_ETB=8000` |
| **RAPID_SEQ** | Same card, 5+ transactions in 5 minutes | `RAPID_LIMIT=5`, `RAPID_WINDOW=5` |
| **OFFHOURS_SPIKE** | Transaction volume > 3x normal between 00:00-05:00 | `OFFHOURS_MULT=3.0` |

### 10.2 — How It Works

1. The detector runs every 60 seconds (`CHECK_INTERVAL=60`)
2. It scans the `atm_transactions` table for each rule
3. If it finds an anomaly, it writes a record to `atm_anomalies` table
4. It writes a count to `/tmp/zabbix_anomaly_count` (for Zabbix)
5. It writes per-ATM counts to `/tmp/zabbix_{atm_id}_anomalies` (for Zabbix)

### 10.3 — Current Limitation

Currently, the `/tmp/zabbix_{atm_id}_anomalies` files are only written for 5 hardcoded ATMs (ATM-001 to ATM-005). For your real ATM, the anomaly detector will still **detect** anomalies (because it scans all transactions from the DB), but it won't write the per-ATM file for Zabbix to read.

**Fix:** When we make the anomaly detector dynamic, it will write files for all ATMs in the database, including your real ATM.

### 10.4 — Testing

To test anomaly detection, you can:
1. Insert a large withdrawal (> ETB 8,000) into the `atm_transactions` table
2. Wait 60 seconds
3. Check the `atm_anomalies` table: `SELECT * FROM atm_anomalies WHERE atm_id = 'ATM-006';`
4. Check the `/tmp/anomaly_feed.json` file

---

## 11. Step 7: Configure Network Correlation

The network correlator connects network degradation events (from Zabbix) with transaction failures. This is important for understanding if network issues are causing transaction failures.

### 11.1 — What It Does

1. It polls Zabbix for network latency/packet-loss events
2. It reads the `atm_transactions` table for transactions during the network event
3. It calculates the failure rate during the event vs. the baseline
4. It writes the correlation to `atm_network_correlation` table
5. Grafana shows this data on the correlation panel

### 11.2 — Current Limitation

Currently, the network correlator only processes 5 hardcoded ATMs (ATM-001 to ATM-005). For your real ATM, it won't process network events until we make it dynamic.

**Fix:** When we make the network correlator dynamic, it will process all ATMs in the database, including your real ATM.

### 11.3 — What You Need

For network correlation to work with your real ATM, you need:
1. Zabbix to be monitoring the ATM's network (via SNMP)
2. The ATM's network metrics (latency, packet loss) to be in the Zabbix database

The Zabbix template already includes network monitoring items (latency, packet loss, link status). So once you register the ATM in Zabbix, network correlation should start working automatically.

---

## 12. Step 8: Test Everything

Now that the ATM is registered, let's verify everything works.

### 12.1 — Test 1: Database Check

```bash
docker exec -it zabbix-db psql -U zabbix -c \
  "SELECT atm_id, branch, vendor, ip_address, status, sim_port FROM atm_locations WHERE atm_id = 'ATM-006';"
```

Expected: Your ATM appears in the results.

### 12.2 — Test 2: Zabbix Host Check

1. Open `http://localhost:8080`
2. Log in as `Admin` / `zabbix`
3. Go to **Configuration → Hosts**
4. Search for your ATM
5. Check that the host exists and has the correct template

### 12.3 — Test 3: Zabbix SNMP Polling

1. Go to **Monitoring → Latest Data**
2. Filter by your ATM host
3. Wait 1-2 minutes
4. Check that values are being collected (cash levels, card reader status, etc.)

### 12.4 — Test 4: Transaction Feed

If you're using the simulation (which you probably are for now), check that transactions are being generated for your ATM:

```bash
docker exec -it zabbix-db psql -U zabbix -c \
  "SELECT COUNT(*), atm_id FROM atm_transactions WHERE atm_id = 'ATM-006' GROUP BY atm_id;"
```

Expected: Some transactions should appear.

### 12.5 — Test 5: EJ Logs

Check that EJ logs are being generated:

```bash
ls -la ej-logs/ATM-006.log
wc -l ej-logs/ATM-006.log
```

Expected: The log file exists and has some lines.

### 12.6 — Test 6: Anomaly Detection

Wait for the anomaly detector to scan (it runs every 60 seconds):

```bash
docker exec -it zabbix-db psql -U zabbix -c \
  "SELECT * FROM atm_anomalies WHERE atm_id = 'ATM-006' ORDER BY detected_at DESC LIMIT 5;"
```

Expected: Some anomalies (if the simulation generates them).

### 12.7 — Test 7: Report Portal

1. Open `http://localhost:8888`
2. Log in as `admin` / `admin123`
3. Go to **ATMs** and find your ATM
4. Click **View** to see the detail page
5. Check that transactions and state are showing correctly

### 12.8 — Test 8: Grafana Dashboard

1. Open `http://localhost:3002`
2. Log in as `admin` / `dashen2024`
3. Open the **ATM Operations Centre** dashboard
4. Check that your ATM appears in the dashboard

---

## 13. Step 9: Switch from Simulation to Production

When you're ready to receive real transactions from the ATM (instead of simulated ones), you need to switch the system to production mode.

### 13.1 — What Changes

| Component | Simulation Mode | Production Mode |
|-----------|----------------|-----------------|
| **Transaction feed** | `txn_engine.py` generates fake transactions | `iso8583_gateway.py` receives real ISO 8583 messages |
| **SNMP monitoring** | Reads from simulated HTTP servers (`sim_engine.py`) | Reads from real ATM via SNMP |
| **EJ logs** | `ej_engine.py` generates fake logs | Real ATM writes real logs (via file share or FTP) |
| **State management** | Reads from simulated HTTP servers | Reads from Zabbix API |

### 13.2 — How to Switch

**Step 1: Stop the simulation engines**
```bash
docker compose stop atm-sim-engine atm-txn-engine atm-ej-engine state-manager
```

**Step 2: Configure the ISO 8583 gateway for production**
```bash
# Edit docker-compose.yml
# Change the iso8583-gateway service environment:
iso8583-gateway:
  environment:
    MODE: tcp
    SWITCH_HOST: 0.0.0.0
    SWITCH_PORT: "9876"
```

**Step 3: Update the Zabbix templates**
The current templates use the synthetic OID root (`1.3.6.1.4.1.99999`). For real ATMs, you need templates with the real vendor OIDs:
- NCR: `1.3.6.1.4.1.37513`
- GRG: `1.3.6.1.4.1.51234`

**Step 4: Update the state manager**
The state manager currently reads from the simulator HTTP servers. In production, it should read from the Zabbix API.

**Step 5: Connect the ATM switch**
The ATM switch needs to be configured to send ISO 8583 messages to the gateway's TCP port (9876).

### 13.3 — Important: Don't Break the Simulation

The key principle is: **the simulation should continue to work for simulated ATMs, while the real ATM uses production mode.**

This means:
- The simulation engines should keep running for simulated ATMs
- The real ATM should use real SNMP, real transactions, and real EJ logs
- The system should handle both modes simultaneously

**This is what we'll implement in Phase 1 of the production readiness plan** — a configuration layer that allows simulation and production to coexist.

---

## 14. Common Issues & How to Fix Them

### Issue 1: ATM Not Showing in Zabbix

**Symptoms:** The ATM is in the database but doesn't appear in Zabbix.

**Fix:**
```bash
# Re-run the sync script
python3 scripts/sync_atms_to_zabbix.py --apply --import-templates

# Check the logs for errors
python3 scripts/sync_atms_to_zabbix.py --apply 2>&1 | grep -i error
```

### Issue 2: SNMP Not Working

**Symptoms:** Zabbix shows the ATM as unreachable.

**Fix:**
1. Check network connectivity: `ping 192.168.1.100`
2. Check SNMP: `snmpget -v2c -c dashen_atm 192.168.1.100 1.3.6.1.4.1.37513.1.1.0`
3. Check firewall: Make sure port 161 is open
4. Check SNMP community: Make sure it matches what's in the database

### Issue 3: Transactions Not Appearing

**Symptoms:** No transactions in the database for the ATM.

**Fix:**
1. Check if the simulation is generating transactions: `docker logs atm-txn-engine`
2. Check if the ATM is active in the database: `SELECT status FROM atm_locations WHERE atm_id = 'ATM-006';`
3. Check if the ATM has a sim_port: `SELECT sim_port FROM atm_locations WHERE atm_id = 'ATM-006';`

### Issue 4: EJ Logs Not Appearing

**Symptoms:** No EJ logs for the ATM.

**Fix:**
1. Check if the EJ engine is running: `docker logs atm-ej-engine`
2. Check the EJ log directory: `ls -la ej-logs/`
3. Check if the ATM is registered in the database

### Issue 5: Anomaly Detector Not Writing Per-ATM Files

**Symptoms:** The `/tmp/zabbix_{atm_id}_anomalies` file doesn't exist.

**Fix:**
This is the current limitation. The anomaly detector only writes files for 5 hardcoded ATMs. When we make it dynamic, this will be fixed.

---

## 15. Quick Reference: All Commands

### Physical ATM Commands (New)

```bash
# Install SNMP tools (if not already installed)
sudo apt-get install snmp snmp-mibs-downloader

# Test SNMP connectivity (replace with your ATM's IP and community string)
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.37513.1.1.0

# Test NCR OIDs
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.37513.1.1.0   # ATM status
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.37513.1.2.0   # Cassette 1
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.37513.1.3.0   # Cassette 2
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.37513.1.7.0   # Cash jam
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.37513.2.1.0   # Card reader
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.37513.7.1.0   # Network link

# Test GRG OIDs
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.51234.1.1.0   # ATM status
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.51234.2.1.0   # Cash module 1
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.51234.2.5.0   # Cash jam
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.51234.3.1.0   # Card unit
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.51234.8.1.0   # Network link

# Walk all OIDs (discover what the ATM supports)
snmpwalk -v2c -c public 192.168.1.100 1.3.6.1.4.1.37513   # NCR
snmpwalk -v2c -c public 192.168.1.100 1.3.6.1.4.1.51234   # GRG

# Scan network for ATMs (requires nmap)
nmap -sn 192.168.1.0/24

# Test network connectivity
ping 192.168.1.100

# Test TCP connectivity to ISO 8583 gateway
nc -zv <gateway-ip> 9876
```

### Database Commands

```bash
# Connect to database
docker exec -it zabbix-db psql -U zabbix

# Insert a new ATM
INSERT INTO atm_locations (atm_id, branch, district, city, region, terminal_id, vendor, model, install_date, status, snmp_community, snmp_version, ej_log_format, atm_name, ip_address)
VALUES ('ATM-006', 'Bole International Branch', 'EAD', 'Addis Ababa', 'Addis Ababa', 'TID006', 'NCR', 'SelfServ 34', '2026-07-29', 'active', 'dashen_atm', 'v2c', 'NCR_APTRA', 'Bole International Branch ATM', '192.168.1.100');

# Check all ATMs
SELECT atm_id, branch, vendor, ip_address, status, sim_port FROM atm_locations ORDER BY atm_id;

# Check transactions for an ATM
SELECT COUNT(*) FROM atm_transactions WHERE atm_id = 'ATM-006';

# Check anomalies for an ATM
SELECT * FROM atm_anomalies WHERE atm_id = 'ATM-006' ORDER BY detected_at DESC;
```

### Zabbix Commands

```bash
# Sync ATMs to Zabbix
python3 scripts/sync_atms_to_zabbix.py --apply --import-templates

# Reload Zabbix config cache
docker exec zabbix-server zabbix_server -R config_cache_reload
```

### Docker Commands

```bash
# Check running containers
docker ps --format "table {{.Names}}\t{{.Status}}"

# Check logs
docker logs atm-sim-engine
docker logs atm-txn-engine
docker logs atm-ej-engine
docker logs state-manager
docker logs anomaly-detector
docker logs network-correlator

# Restart a service
docker compose restart anomaly-detector

# Stop simulation engines (when switching to production)
docker compose stop atm-sim-engine atm-txn-engine atm-ej-engine state-manager
```

### SNMP Test Commands

```bash
# Test SNMP connectivity
snmpget -v2c -c dashen_atm 192.168.1.100 1.3.6.1.4.1.37513.1.1.0

# Test NCR OIDs
snmpget -v2c -c dashen_atm 192.168.1.100 1.3.6.1.4.1.37513.1.2.0  # Cassette 1
snmpget -v2c -c dashen_atm 192.168.1.100 1.3.6.1.4.1.37513.2.1.0  # Card reader

# Test GRG OIDs
snmpget -v2c -c dashen_atm 192.168.1.100 1.3.6.1.4.1.51234.2.1.0  # Cash module 1
snmpget -v2c -c dashen_atm 192.168.1.100 1.3.6.1.4.1.51234.3.1.0  # Card unit
```

---

## Appendix: Glossary

| Term | Definition |
|------|------------|
| **ATM** | Automated Teller Machine |
| **SNMP** | Simple Network Management Protocol — used to monitor hardware metrics |
| **OID** | Object Identifier — a unique address for each metric (e.g., `1.3.6.1.4.1.37513.1.2.0`) |
| **ISO 8583** | A binary message format used in banking for financial transactions |
| **EJ** | Electronic Journal — detailed log of every ATM transaction |
| **Zabbix** | Open-source monitoring platform |
| **Grafana** | Open-source dashboard and visualization tool |
| **OpenSearch** | Open-source search and analytics engine (formerly Elasticsearch) |
| **GLPI** | Open-source IT asset management and ticketing system |
| **Filebeat** | Log shipper from Elastic (sends logs to OpenSearch) |
| **Community string** | A password for SNMP access (like `dashen_atm`) |
| **Terminal ID** | The physical ID of the ATM (e.g., `TID006`) |
| **MTI** | Message Type Indicator — the first 4 bytes of an ISO 8583 message |
| **STAN** | System Trace Audit Number — a unique identifier for each transaction |

---

## Appendix: How to Add More ATMs

When you get more ATMs, repeat the same process:

1. **Gather ATM information** (IP, vendor, terminal ID, branch, etc.)
2. **Register in database** (INSERT into `atm_locations`)
3. **Run sync script** (`python3 scripts/sync_atms_to_zabbix.py --apply`)
4. **Verify in Zabbix** (check host appears)
5. **Verify in Report Portal** (check ATM appears)
6. **Verify in Grafana** (check dashboard shows data)

The system is designed to handle multiple ATMs. Each ATM is independent — if one ATM goes offline, the others continue to work.

---

## Appendix: Physical ATM Setup Checklist

Use this checklist when you physically receive a new ATM:

### Before You Start
- [ ] ATM is physically installed at the location
- [ ] ATM is powered on and booted up
- [ ] Ethernet cable is connected from ATM to network
- [ ] ATM is reachable via network (ping the IP)

### Gather ATM Information
- [ ] ATM IP address: ________________
- [ ] ATM vendor: NCR / GRG
- [ ] ATM terminal ID: ________________
- [ ] ATM model: ________________
- [ ] ATM branch: ________________
- [ ] ATM district: ________________
- [ ] ATM city: ________________
- [ ] ATM region: ________________
- [ ] SNMP community string: ________________
- [ ] EJ log format: NCR_APTRA / GRG

### Test SNMP Connectivity
- [ ] Test ATM status OID: `snmpget -v2c -c <community> <ip> <oid>`
- [ ] Test cash cassette OID
- [ ] Test card reader OID
- [ ] Test network link OID

### Register in System
- [ ] Insert ATM into database: `INSERT INTO atm_locations ...`
- [ ] Run sync script: `python3 scripts/sync_atms_to_zabbix.py --apply`
- [ ] Verify in Zabbix: Check host exists
- [ ] Verify in Report Portal: Check ATM appears
- [ ] Verify in Grafana: Check dashboard shows data

### Test Everything
- [ ] Database check: ATM appears in `atm_locations`
- [ ] Zabbix host check: Host exists with correct template
- [ ] Zabbix SNMP polling: Values being collected
- [ ] Transaction feed: Transactions appearing in database
- [ ] EJ logs: Log file exists with content
- [ ] Anomaly detection: Anomalies detected (if any)
- [ ] Report Portal: ATM visible and data showing
- [ ] Grafana dashboard: ATM visible and data showing

### If Something Goes Wrong
- [ ] Check network connectivity: `ping <ip>`
- [ ] Check SNMP: `snmpget -v2c -c <community> <ip> <oid>`
- [ ] Check firewall: Port 161 open
- [ ] Check Zabbix logs: `docker logs zabbix-server`
- [ ] Check simulation engine logs: `docker logs atm-sim-engine`
- [ ] Check database: `SELECT * FROM atm_locations WHERE atm_id = '<id>';`

## Appendix: Physical ATM Setup: Step-by-Step Example

Here's a complete example of how to set up a real NCR ATM:

### Step 1: Physical Setup
```
1. Open the ATM cabinet (using the ATM key)
2. Connect an Ethernet cable from the ATM to your network switch
3. Power on the ATM (power switch inside the cabinet)
4. Wait for the ATM to boot up (1-2 minutes)
5. Close the cabinet
```

### Step 2: Find the ATM's IP Address
```
Method 1: Check the ATM's display screen for "Network Settings"
Method 2: Use the keypad to navigate to "Configuration" → "Network"
Method 3: Check your router's DHCP client list for the ATM's MAC address
Method 4: Use nmap to scan your network: nmap -sn 192.168.1.0/24

Example: ATM IP is 192.168.1.100
```

### Step 3: Get the SNMP Community String
```
Ask the ATM vendor or check the ATM's configuration menu.
Example: SNMP community is "public" (default) or "dashen_atm"
```

### Step 4: Test SNMP Connectivity
```bash
# Test ATM status
snmpget -v2c -c public 192.168.1.100 1.3.6.1.4.1.37513.1.1.0

# Expected output: Integer32: 1 (meaning ATM is OK)
```

### Step 5: Get the Terminal ID
```
Check the ATM's display or configuration menu.
Example: Terminal ID is "TID006"
```

### Step 6: Register in Database
```sql
INSERT INTO atm_locations (
    atm_id, branch, district, city, region,
    terminal_id, vendor, model, install_date,
    status, snmp_community, snmp_version,
    ej_log_format, atm_name, ip_address
) VALUES (
    'ATM-006',
    'Bole International Branch',
    'EAD',
    'Addis Ababa',
    'Addis Ababa',
    'TID006',
    'NCR',
    'SelfServ 34',
    '2026-07-29',
    'active',
    'public',
    'v2c',
    'NCR_APTRA',
    'Bole International Branch ATM',
    '192.168.1.100'
);
```

### Step 7: Sync to Zabbix
```bash
python3 scripts/sync_atms_to_zabbix.py --apply --import-templates
```

### Step 8: Verify
```bash
# Check database
docker exec -it zabbix-db psql -U zabbix -c \
  "SELECT atm_id, branch, vendor, ip_address FROM atm_locations WHERE atm_id = 'ATM-006';"

# Check Zabbix
# Open http://localhost:8080 → Configuration → Hosts → Search for "ATM-006"
```

### Step 9: Wait and Check
```
Wait 1-2 minutes for Zabbix to start polling.
Then check:
- Monitoring → Latest Data → Filter by ATM-006
- You should see values for cash cassettes, card reader, network, etc.
```

### Step 10: Monitor
```
Your ATM is now being monitored! You can:
- View it in Grafana dashboards
- Check its state in the Report Portal
- See transactions in the database
- Monitor for anomalies
```
