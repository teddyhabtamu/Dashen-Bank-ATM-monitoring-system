**DASHEN BANK**

**ATM MONITORING SYSTEM**

**Production Migration Guide**

_From Proof-of-Concept (Laptop / Simulated ATMs) to Production (Server / Real ATMs)_

Prepared for: ATM Monitoring System Project

Scope: Zabbix, Grafana, ELK, GLPI, PostgreSQL, ISO 8583 Gateway

Status: Internal Working Document

**Table of Contents**

# 1\. Introduction and How to Use This Guide

This document explains how to take the ATM Monitoring System - currently running on a single laptop with five simulated ATMs - and deploy it on a real production server connected to real Dashen Bank ATMs. It is written for the same person who built the proof of concept, so it assumes familiarity with the existing setup but does not assume prior server administration experience.

The guide is organized so that each section can be tackled independently and roughly in order. Some sections (such as server provisioning) are one-time setup tasks. Others (such as ATM onboarding) are repeatable processes you will follow every time a new ATM is connected to the system.

## 1.1 What Stays the Same

The core design principle from the proof of concept holds: every simulator is a placeholder for a real data source, and the database schema, dashboards, alert rules, ticketing integration, and report portal do not change. What changes is only where the data comes from.

| **Component**                               | **Proof of Concept**                          | **Production**                                                   |
| ------------------------------------------- | --------------------------------------------- | ---------------------------------------------------------------- |
| Hardware status (cassettes, doors, sensors) | atm-sim-00X containers (Python HTTP API)      | Real ATM SNMP agent, polled by Zabbix                            |
| Electronic Journal (EJ) logs                | atm-ej-00X containers writing local files     | Real ATM EJ files, shipped via Filebeat or SFTP                  |
| Transactions                                | txn-feed-00X containers writing to PostgreSQL | ISO 8583 Gateway in TCP mode, fed by ATM switch                  |
| ATM locations / metadata                    | Manually inserted SQL rows                    | Populated via admin form or bulk import as ATMs are commissioned |
| Zabbix hosts                                | 5 hosts created manually                      | Auto-discovery + auto-registration for bulk onboarding           |
| Grafana, GLPI, Report Portal, ELK           | Unchanged                                     | Unchanged - same containers, same configuration                  |

**NOTE:** Nothing in Grafana, GLPI, the Report Portal, or the database schema needs to change during this migration. If you find yourself editing a Grafana dashboard query or a Report Portal route to "support real ATMs", stop - that is a sign the data is not arriving in the expected shape, not that the dashboard needs to change.

## 1.2 What You Will Need Before Starting

- A Linux server (Ubuntu 22.04 LTS recommended) provisioned by Dashen Bank IT, with network access to the ATM network segment
- At least one real ATM connected to the network, with its IP address, SNMP community string, and EJ file location
- Contact with the ATM Switch / Channel Support team for ISO 8583 connection details (host, port, message format)
- Your existing GitHub repository (already contains all code, Docker Compose files, and configuration)
- Administrative access to Zabbix, Grafana, and GLPI (same credentials as the proof of concept, unless changed)

## 1.3 Overall Migration Sequence

At a high level, the migration happens in this order:

- Provision the server and deploy the existing stack exactly as-is (Section 2)
- Verify everything works on the server with the simulators still running (Section 2)
- Connect the first real ATM's hardware monitoring via SNMP (Section 3)
- Connect the first real ATM's Electronic Journal logs (Section 4)
- Connect the ATM switch via the ISO 8583 Gateway in production mode (Section 5)
- Populate atm_locations with real GPS and metadata (Section 6)
- Set up Zabbix auto-discovery for bulk onboarding of remaining ATMs (Section 7)
- Decommission the simulators once real ATMs are confirmed working (Section 8)
- Set up backups and basic disaster recovery (Section 9)
- Run through the final production readiness checklist (Section 10)

# 2\. Server Provisioning and Initial Deployment

This section covers getting a server ready and deploying the exact same stack that runs on your laptop today. The goal at the end of this section is: the server runs all 6 tools, all 5 simulated ATMs still work, and you can access every service from a browser on the bank's network.

## 2.1 Requesting the Server

Request the following minimum specification from IT Infrastructure. These numbers assume the current 5-ATM proof of concept scaling to roughly 20-30 ATMs; revisit sizing once you know the real fleet size.

| **Resource**               | **Minimum**                            | **Recommended**                         |
| -------------------------- | -------------------------------------- | --------------------------------------- |
| OS                         | Ubuntu 22.04 LTS                       | Ubuntu 22.04 LTS                        |
| CPU                        | 4 cores                                | 8 cores                                 |
| RAM                        | 16 GB                                  | 32 GB                                   |
| Disk                       | 250 GB SSD                             | 500 GB SSD                              |
| Network                    | Static internal IP, access to ATM VLAN | Same, plus access to ATM switch network |
| Open ports (internal only) | 8080, 8082, 3001, 5601, 8888, 9876     | Same                                    |

**NOTE:** None of these ports should be exposed to the public internet. The server should sit on Dashen's internal network, reachable only from staff workstations and from the ATM network segment.

## 2.2 Installing Docker

Once you have SSH access to the server:

curl -fsSL <https://get.docker.com> | sh

sudo usermod -aG docker \$USER

sudo systemctl enable docker

\# Log out and back in for group membership to take effect, then verify:

docker --version

docker compose version

## 2.3 Cloning the Repository

git clone &lt;YOUR_GITHUB_URL&gt;

cd Dashen-Bank-ATM-monitoring-system

**NOTE:** The folder name becomes part of the Docker network name (e.g. dashen-bank-atm-monitoring-system_default). Keep the folder name consistent between your laptop and the server to avoid re-deriving gateway IPs, though this is not strictly required - Section 2.6 covers what to do if the network name differs.

## 2.4 Running the Setup Script

bash scripts/setup_new_machine.sh

This script builds all Docker images, starts every container, and restores the PostgreSQL data (ATM locations and historical transactions) from the backup committed to the repository. It takes 5-10 minutes on a server with good internet connectivity.

Verify everything is running:

docker ps --format "table {{.Names}}\\t{{.Status}}" | grep -v "Exited"

You should see around 20 containers, all showing "Up". This includes the 5 ATM simulator sets (atm-sim, atm-ej, txn-feed), Zabbix, Grafana, ELK, GLPI, the Report Portal, the ISO 8583 Gateway, the anomaly detector, and the network correlator.

## 2.5 Importing Manual Configuration

Following the README in the repository, complete the manual import steps:

- Import the Zabbix template, hosts, media types, and actions from config/zabbix/
- Confirm Grafana dashboards loaded automatically from config/grafana/dashboards/
- Generate a new GLPI App Token and update the Zabbix GLPI Ticket media type
- Click through the GLPI installation wizard once if this is a fresh GLPI database

## 2.6 Fixing the Zabbix Agent Network Configuration

ATM-001 in the proof of concept uses a real Zabbix Agent installed on the host machine (representing a 'local' ATM). On the server, this agent needs to be installed and configured the same way as documented in the README (Steps 6-7), using the server's own network IP instead of your laptop's WSL IP.

\# Install the agent

wget <https://repo.zabbix.com/zabbix/6.4/ubuntu/pool/main/z/zabbix-release/zabbix-release_6.4-1+ubuntu22.04_all.deb>

sudo dpkg -i zabbix-release_6.4-1+ubuntu22.04_all.deb

sudo apt update && sudo apt install -y zabbix-agent2

\# Find the server's network IP and Docker gateway

ip addr show eth0 | grep 'inet '

docker network inspect &lt;project-name&gt;\_default | grep -i gateway

Update /etc/zabbix/zabbix_agent2.conf with Server=127.0.0.1,172.16.0.0/12 and ServerActive=&lt;gateway-ip&gt;:10051 as described in the README, then restart the agent and update ATM-001's interface IP in Zabbix to the server's own IP.

## 2.7 Accessing the System from Staff Workstations

On a real server (not WSL), there is no need for the Windows port-proxy script used during development. Staff on the bank's network can access the services directly using the server's IP address:

| **Service**   | **URL**                       |
| ------------- | ----------------------------- |
| Zabbix        | http://&lt;server-ip&gt;:8080 |
| Grafana       | http://&lt;server-ip&gt;:3001 |
| Kibana        | http://&lt;server-ip&gt;:5601 |
| GLPI          | http://&lt;server-ip&gt;:8082 |
| Report Portal | http://&lt;server-ip&gt;:8888 |
| pgAdmin       | http://&lt;server-ip&gt;:5050 |

If Dashen's IT team prefers DNS names over raw IPs, ask them to create internal DNS records (e.g. atm-monitor.dashenbank.com) pointing to the server's IP - this is optional and can be done at any time without affecting the application.

## 2.8 Checkpoint

Before moving to Section 3, confirm:

- All containers show "Up" in docker ps
- Zabbix shows all 5 ATM hosts as available (green ZBX)
- Grafana's ATM Operations Centre dashboard loads and shows live data
- The Report Portal generates a PDF report successfully
- Kibana's Discover view shows EJ log entries

If all of the above work with the simulators still running on the server, the platform itself is ready. Everything from this point forward is about replacing simulated data sources with real ones, one at a time.

# 3\. Connecting Real ATM Hardware Monitoring (SNMP)

This is the most technically involved part of the migration, because it is the first point where the system talks to a real, physical ATM instead of a Python script. The goal of this section is to take one real ATM and make Zabbix display its real cassette levels, door status, printer status, and so on - in the exact same dashboard panels that currently show simulated ATM-001 through ATM-005.

## 3.1 What Changes and What Does Not

In the proof of concept, Zabbix items use the HTTP agent type, polling URLs like <http://atm-sim-001:1161/oid/1.1.0>. For real ATMs, the items must be changed to the SNMP agent type, pointing at the real ATM's IP address and OID.

| **Stays the Same**                                    | **Changes**                                                       |
| ----------------------------------------------------- | ----------------------------------------------------------------- |
| Item names (e.g. "Cash - Cassette 1 Notes Remaining") | Item type: HTTP agent to SNMP agent                               |
| Value maps (In Service, Out of Service, etc.)         | Target: simulator URL to real ATM IP                              |
| Triggers and severities                               | OID values: simulator's custom OID scheme to real ATM vendor OIDs |
| Grafana dashboards and panels                         | Polling interval may need adjustment based on ATM responsiveness  |
| GLPI ticket automation                                | Authentication: SNMP community string from the ATM vendor         |

## 3.2 Discovering What a Real ATM Reports via SNMP

Before changing anything in Zabbix, find out what the real ATM actually exposes. Every ATM vendor (NCR, Diebold, Wincor/Diebold Nixdorf, etc.) implements a different SNMP MIB (Management Information Base), so the OIDs will not match the custom 1.1.0 through 8.3.0 scheme used by the simulators.

From the server (or any machine on the same network as the ATM), install the SNMP tools and walk the ATM's MIB tree:

sudo apt install -y snmp snmp-mibs-downloader

\# Replace with the real ATM's IP and community string

snmpwalk -v2c -c public 10.10.1.50 1.3.6.1.4.1.37513

This returns a long list of OIDs and their current values - for example, cassette note counts, door sensor states, printer status codes, and temperature readings. Save this output; it is your map from "real ATM data" to "Zabbix item".

**NOTE:** The community string (often "public" by default, but should be changed to something bank-specific for security) and the base OID (1.3.6.1.4.1.37513 in the example above is NCR's enterprise OID - Diebold, Wincor, and others differ) must be obtained from the ATM vendor or from Dashen's ATM hardware team.

## 3.3 Mapping Real OIDs to Existing Zabbix Items

Go through the snmpwalk output and match each value to the corresponding item in the Dashen Bank ATM Hardware template. Build a mapping table like this one (fill in real values once you have the snmpwalk output):

| **Zabbix Item**                   | **Simulator OID (old)** | **Real ATM OID (new - example)** |
| --------------------------------- | ----------------------- | -------------------------------- |
| ATM Operational Status            | 1.1.0                   | 1.3.6.1.4.1.37513.1.1.0          |
| Cash - Cassette 1 Notes Remaining | 1.2.0                   | 1.3.6.1.4.1.37513.2.1.1          |
| Card - Reader Status              | 2.1.0                   | 1.3.6.1.4.1.37513.3.1.0          |
| Printer - Receipt Paper Level     | 3.2.0                   | 1.3.6.1.4.1.37513.4.2.0          |
| Security - Safe Door Status       | 4.1.0                   | 1.3.6.1.4.1.37513.5.1.0          |
| Environment - Temperature         | 4.3.0                   | 1.3.6.1.4.1.37513.5.3.0          |

**NOTE:** Not every simulated item will have a direct real-world equivalent, and not every real OID will map to something the simulator tracked. This is expected. Items with no real equivalent can be left in the template but will simply show "no data" for that ATM - they do not need to be deleted.

## 3.4 Creating the Real ATM Host in Zabbix

Rather than modifying one of the existing ATM-00X hosts (which would disrupt the simulator), create a new host for the real ATM. This lets you validate the real connection while simulators continue running for the demo/training environment.

- Configuration → Hosts → Create host
- Host name: e.g. "ATM-006 | NCR | &lt;Real Branch Name&gt;"
- Host groups: "Dashen Bank ATMs" (same group as existing ATMs)
- Interfaces → Add → Type: SNMP
- IP address: the real ATM's IP (e.g. 10.10.1.50)
- Port: 161 (standard SNMP port, not 1161 used by simulators)
- SNMP version: v2 (or v3 if the bank requires authentication - see Section 3.7)
- SNMP community: the string obtained from the ATM vendor
- Templates tab → Link template: "Dashen Bank ATM Hardware"
- Click Add

## 3.5 Updating Item Types from HTTP Agent to SNMP Agent

Because the existing template items are HTTP agent type with simulator-specific OIDs, and SNMP agent items need different configuration, the cleanest approach is to clone the template rather than edit it in place.

- Configuration → Templates → Dashen Bank ATM Hardware → Clone
- Name the clone "Dashen Bank ATM Hardware - SNMP (Real)"
- For each item in the cloned template, change:
  - Type: from "HTTP agent" to "SNMP agent"
  - SNMP OID: the real OID from your mapping table (Section 3.3)
  - Remove the URL field (not used for SNMP)
  - Keep the same Key, Name, Type of information, Units, and Value map
- Unlink the HTTP-based template from the real ATM host and link the new SNMP template instead

**NOTE:** This cloning approach means you now maintain two templates - one for simulators (HTTP), one for real ATMs (SNMP). This is intentional and temporary: once all ATMs are real, the HTTP template and the simulator containers can be retired (Section 8), leaving only the SNMP template.

## 3.6 Testing the Connection

After linking the SNMP template, go to Monitoring → Latest data, filter by the new ATM host, and confirm values are populating within one polling interval (default 30s, but SNMP devices are sometimes polled less frequently - check the item configuration).

If items show "Not supported" with an SNMP timeout error:

- Confirm the ATM's firewall allows inbound UDP on port 161 from the Zabbix server's IP
- Re-run snmpwalk from the server itself (not just your laptop) to confirm network reachability
- Double-check the community string - a wrong community string returns no response, not an error message

## 3.7 Security Note: SNMP v3

SNMP v2 community strings are sent in plain text. For a banking environment, ask the ATM hardware team whether SNMP v3 (which supports authentication and encryption) is available. If so, configure the Zabbix host interface with SNMP v3 credentials instead of a v2 community string - the item-level configuration (OIDs, types) does not change, only the interface authentication settings.

## 3.8 Checkpoint

- One real ATM appears as a host in Zabbix with SNMP interface configured
- At least the core items (operational status, cash levels, door sensors) show live values from the real ATM
- Triggers fire correctly when real values cross thresholds (test by, for example, checking what happens if a door is opened)
- The new ATM appears in Grafana's ATM Fleet Overview table once Section 6 (location data) is also complete

# 4\. Connecting Real Electronic Journal (EJ) Logs

The proof of concept's atm-ej-00X containers write fake EJ log lines to local files, which Filebeat ships to Elasticsearch. For real ATMs, the EJ files exist on the ATM itself (typically running Windows Embedded, Windows 7, or Windows 10 IoT), and need to reach the same Elasticsearch index by a different path.

## 4.1 Locating Real EJ Files on the ATM

The exact path depends on the ATM vendor and software. Common locations for NCR APTRA-based ATMs include paths such as C:\\Program Files\\NCR\\APTRA\\Journal\\ or vendor-specific journal directories. Ask the ATM hardware/vendor support team for:

- The exact EJ file path and naming convention (e.g. one file per day, rolling logs)
- The log format/encoding (EJ logs are often fixed-width text, sometimes with vendor-specific control characters)
- Whether the bank has any existing process that already collects these files (some banks already pull EJ files to a central server for compliance - if so, point Filebeat at that existing location instead of the ATM directly)

## 4.2 Three Ways to Get EJ Files to the Server

Choose the approach that best fits Dashen's network policy and the ATM vendor's constraints. All three result in the same outcome: EJ log text reaching the server's filesystem, where Filebeat picks it up exactly as it does for the simulated logs today.

| **Approach**          | **How it works**                                                                                                        | **When to use**                                                            |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| Filebeat on the ATM   | Install a lightweight Filebeat agent directly on the ATM's Windows OS, configured to ship to the server's Elasticsearch | Best long-term option if ATM vendor allows installing additional software  |
| Shared network folder | ATM writes/copies EJ files to a network share; server-side Filebeat watches that share                                  | Good middle ground - no agent on the ATM, but requires network share setup |
| SFTP/FTP push         | ATM (or an existing collection process) pushes EJ files via SFTP to a folder on the server                              | Works if the bank already has an EJ collection process in place            |

**NOTE:** Whichever method is chosen, the end result must be: real EJ log files land in a directory on the server, in roughly the same place the simulated files land today (the ej-logs/ directory mounted into the Filebeat container).

## 4.3 Updating the Filebeat Configuration

The current filebeat.yml watches /var/log/atm-ej/ATM-\*.log (mapped from ./ej-logs on the host). For real ATMs, add a second input pointing at wherever real EJ files land, or - if the file naming convention matches - simply add the real files into the same ej-logs/ directory using a naming pattern Filebeat already matches.

Example: adding a second input for real ATM logs arriving via SFTP into /data/real-ej-logs/:

filebeat.inputs:

\- type: log

enabled: true

paths:

\- /var/log/atm-ej/ATM-\*.log # existing simulated logs

fields:

log_type: atm_ej

fields_under_root: true

\- type: log

enabled: true

paths:

\- /data/real-ej-logs/\*.log # NEW: real ATM logs

fields:

log_type: atm_ej

fields_under_root: true

Mount the new path into the Filebeat container by adding a volume in docker-compose.yml:

filebeat:

\# ...existing config...

volumes:

\- ./ej-logs:/var/log/atm-ej

\- /data/real-ej-logs:/data/real-ej-logs # NEW

Restart Filebeat after editing:

sudo chown root:root filebeat.yml && sudo chmod 644 filebeat.yml

docker compose up -d filebeat

## 4.4 Handling Different Log Formats

The simulated EJ logs were written in a specific NCR-style format with masked PANs, matched by the Kibana data view .ds-atm-ej-live-\*. Real ATM logs may use a different field layout, delimiters, or timestamp format.

If the real log format differs significantly, two options exist:

- Use a Filebeat processor or an Elasticsearch ingest pipeline to reformat real log lines into the same structure as the simulated ones before indexing - this keeps Kibana searches and dashboards unchanged.
- Index real logs into a separate data stream (e.g. .ds-atm-ej-real-\*) and create a second Kibana Data View - this is simpler initially but means operations staff search two places.

For most banks, option 1 (normalizing at ingest time) is preferable because it preserves the single-search-box experience for fraud and dispute investigation that the BRD requires.

## 4.5 Verifying Real EJ Data

\# Confirm new indices/documents are arriving

curl -s "<http://localhost:9200/\_cat/indices?v>"

\# Check document count growth over a minute

curl -s "<http://localhost:9200/.ds-atm-ej-live-\*/\_count>"

In Kibana Discover, search for the real ATM's terminal ID or branch name and confirm entries appear with masked card numbers and realistic timestamps.

## 4.6 PCI DSS / Card Number Masking

**NOTE:** Verify that real EJ logs already mask PANs (Primary Account Numbers) before they reach Elasticsearch, or that the ingest pipeline masks them. The simulated logs were generated already-masked; real ATM logs may contain full card numbers depending on configuration. This is a compliance requirement, not just a technical one - confirm with Dashen's security/compliance team before indexing real EJ data.

## 4.7 Checkpoint

- Real EJ files reach the server (via agent, share, or SFTP)
- Filebeat ships them into the same .ds-atm-ej-live-\* index pattern (or a normalized equivalent)
- Kibana Discover shows real transactions with masked card numbers
- Retention policy (Index Lifecycle Management) is configured for the desired retention period (90 days per the BRD)

# 5\. Connecting the ATM Switch (ISO 8583 Production Mode)

The ISO 8583 Gateway (camel/iso8583_gateway.py) was built from the start to support two modes: simulation (generates fake transactions) and tcp (listens for a real ATM switch connection). This section covers switching to tcp mode.

## 5.1 Getting Information from the ATM Switch Team

Contact Dashen's Channel Support / ATM Switch team (the team managing systems like Base24, Postilion, or similar) and request:

- Whether the switch can push ISO 8583 messages to an external TCP listener (our gateway listens; the switch connects as a client) - or whether our gateway needs to connect to the switch as a client instead
- The exact ISO 8583 variant in use (different switches use slightly different field definitions, bitmaps, and message length headers)
- A test/UAT environment or sample message captures to validate parsing before going live
- Network firewall rules needed to allow the connection between the switch and the monitoring server

**NOTE:** The parse_iso8583_message function in iso8583_gateway.py implements a general ISO 8583 bitmap parser, but real-world switches often have vendor-specific quirks (custom private fields, different length encodings). Budget time for adjusting this parser based on real sample messages - this is the most likely place for unexpected work in the entire migration.

## 5.2 Switching the Gateway to TCP Mode

In docker-compose.yml, change the iso8583-gateway service environment from simulation to tcp mode:

iso8583-gateway:

build:

context: ./camel

dockerfile: Dockerfile.gateway

container_name: iso8583-gateway

environment:

MODE: tcp # was: simulation

SWITCH_HOST: 0.0.0.0 # listen on all interfaces

SWITCH_PORT: "9876"

DB_HOST: postgres

DB_NAME: zabbix

DB_USER: zabbix

DB_PASS: zabbix_pass

ports:

\- "9876:9876"

depends_on:

\- postgres

restart: unless-stopped

docker compose up -d iso8583-gateway

docker logs iso8583-gateway --tail=20

The log should show: "ISO 8583 TCP server listening on 0.0.0.0:9876" and "Waiting for ATM switch connection...". Give the switch team the server's IP address and port 9876.

## 5.3 If the Switch Requires Our Gateway to Connect Out (Client Mode)

Some switches expect the monitoring system to connect to them, rather than the reverse. If this is the case, the gateway's start_tcp_server function needs to be replaced with a client connection function that connects to SWITCH_HOST:SWITCH_PORT and reconnects automatically if the connection drops. This is a moderate code change - flag it early if the switch team confirms this is their model, so it can be planned for.

## 5.4 Adjusting the Parser for Real Messages

Once sample messages or a UAT connection is available:

- Capture a handful of real ISO 8583 messages (the switch team can usually provide a packet capture or sample log)
- Run them through parse_iso8583_message() in a test script and compare the output to the expected fields
- Adjust field lengths, bitmap parsing, or add handling for additional fields (e.g. field 48 private use data) as needed
- Update RESPONSE_CODES and PROCESSING_CODES mappings if the real switch uses different codes than the simulated ones

## 5.5 Source Tagging During Transition

The gateway already tags transactions with a source column - ISO8583_SIM for simulated data and ISO8583_REAL for real data once parse_iso8583_message processes actual switch messages. During the transition period, both can coexist in atm_transactions.

Use this to your advantage: filter Grafana panels and Report Portal queries by source = 'ISO8583_REAL' to validate real data quality before fully cutting over, without disrupting the existing simulated dashboards used for training or demos.

\-- Example: compare real vs simulated transaction counts

SELECT source, COUNT(\*), MAX(recorded_at)

FROM atm_transactions

WHERE source LIKE 'ISO%'

GROUP BY source;

## 5.6 Checkpoint

- The ISO 8583 Gateway is in tcp mode and listening (or connected, if client mode)
- Real transactions from at least one ATM appear in atm_transactions with source = 'ISO8583_REAL'
- Amounts, card masks, and status codes look correct when spot-checked against the switch's own records
- Grafana panels and Report Portal reports correctly include real transactions alongside (or instead of) simulated ones

# 6\. Populating Real ATM Location Data

Grafana's geo-map and the ATM Fleet Overview table read from the atm_locations table. For the 5 simulated ATMs, this table was populated manually with hand-picked coordinates. For real ATMs, this needs to scale - both for the first ATM and for the eventual full fleet.

## 6.1 Information Needed Per ATM

For each real ATM, collect the following from the branch network team or ATM deployment records:

| **Field**                | **Example**            | **Source**                                                    |
| ------------------------ | ---------------------- | ------------------------------------------------------------- |
| atm_id                   | ATM-006                | Assigned sequentially or matched to existing asset tags       |
| branch                   | Adama Main Branch      | Branch network list                                           |
| district / city / region | Adama / Adama / Oromia | Branch network list                                           |
| latitude / longitude     | 8.5400, 39.2700        | GPS coordinates of the branch (Google Maps pin is sufficient) |
| terminal_id              | TID0006                | ATM switch configuration                                      |
| vendor / model           | NCR / SelfServ 84      | ATM asset register                                            |
| install_date             | 2024-01-10             | ATM asset register                                            |

## 6.2 One-Off Manual Entry (First Few ATMs)

For the first handful of real ATMs, the same SQL approach used for the simulators works fine:

docker exec zabbix-db psql -U zabbix -d zabbix << 'SQL'

INSERT INTO atm_locations VALUES

('ATM-006','Adama Main Branch',

'Adama','Adama','Oromia',

8.5400,39.2700,'TID0006','NCR','SelfServ 84',

'2024-01-10','active')

ON CONFLICT (atm_id) DO NOTHING;

SQL

## 6.3 Bulk Import for the Full Fleet

Once the bank provides a fuller list of ATMs (commonly as an Excel spreadsheet from the branch network or asset management team), the fastest approach is a CSV import directly into PostgreSQL:

- Ask the branch network team for a spreadsheet with: ATM ID/asset tag, branch name, district, city, region, GPS coordinates, terminal ID, vendor, model, install date
- Save it as a CSV file matching the atm_locations column order
- Copy the CSV into the postgres container and import:

docker cp atm_locations_bulk.csv zabbix-db:/tmp/

docker exec zabbix-db psql -U zabbix -d zabbix -c \\

"\\\\copy atm_locations FROM '/tmp/atm_locations_bulk.csv' WITH CSV HEADER"

**NOTE:** If GPS coordinates are not available for some branches, use the branch's city-center coordinates as a placeholder and flag these rows (e.g. a notes column or a separate tracking sheet) for correction later. An approximate map position is far more useful for a fleet-wide view than no position at all, and is easy to refine once exact coordinates are obtained.

## 6.4 An Admin Page for Ongoing ATM Registration

As new ATMs are installed over time, typing SQL by hand does not scale and is error-prone. A simple admin form added to the Report Portal - where a non-technical user enters the fields from Section 6.1 through a web form that writes to atm_locations - removes this friction entirely.

This is a small addition to the existing Flask-based Report Portal (report-portal/app.py): one new route serving an HTML form, and one route handling the form submission with an INSERT ... ON CONFLICT DO UPDATE statement. This is recommended as an early task once the first few real ATMs are working, since it directly supports the BRD's requirement for ongoing ATM onboarding without engineering involvement.

## 6.5 Checkpoint

- Real ATMs appear correctly positioned on the Grafana geo-map
- ATM Fleet Overview table shows correct branch names, districts, and vendor/model for real ATMs
- Drill-down dashboards for real ATMs show the correct identity information

# 7\. Bulk ATM Onboarding via Zabbix Auto-Discovery

Manually creating a Zabbix host per ATM (Section 3.4) is fine for the first few real ATMs, but does not scale to Dashen's full fleet. Zabbix's built-in Network Discovery and Auto-Registration features can find new ATMs on the network and configure them automatically, using the SNMP template created in Section 3.5.

## 7.1 How Auto-Discovery Works

Zabbix periodically scans a configured IP range. For each device that responds to a configured check (in our case, an SNMP OID that all Dashen ATMs are expected to expose, such as the operational status OID), Zabbix can automatically:

- Create a new host with a name derived from the device
- Add it to the "Dashen Bank ATMs" host group
- Link the "Dashen Bank ATM Hardware - SNMP (Real)" template
- Begin monitoring immediately, with zero manual steps

## 7.2 Creating the Discovery Rule

- Configuration → Discovery → Create discovery rule
- Name: "Dashen ATM Network Scan"
- IP range: the ATM network's address range, e.g. 10.10.1.1-254 (ask the network team for the correct VLAN/subnet)
- Checks → Add → Type: SNMPv2 agent, Port: 161, OID: the operational status OID identified in Section 3.2 (e.g. 1.3.6.1.4.1.37513.1.1.0), SNMP community: the bank-wide ATM community string
- Device uniqueness criterion: IP address
- Update interval: 1h (ATMs do not move networks often; hourly scanning is more than sufficient)
- Enable the rule

## 7.3 Creating the Auto-Registration / Discovery Action

- Configuration → Actions → Discovery actions → Create action
- Name: "Auto-register Dashen ATM"
- Conditions: "Service type equals SNMPv2 agent" and "Discovery rule equals Dashen ATM Network Scan"
- Operations → Add host
- Operations → Add to host group → "Dashen Bank ATMs"
- Operations → Link template → "Dashen Bank ATM Hardware - SNMP (Real)"
- Enable the action

**NOTE:** Auto-discovered hosts will have generic names (often the IP address) until host metadata or naming conventions are configured. Plan a naming convention in advance - for example, deriving the ATM ID from the last octet of the IP address via a Host Inventory mapping, or working with the network team to assign IPs that encode branch information.

## 7.4 What Auto-Discovery Does Not Do

Auto-discovery handles the Zabbix side only. It does not populate atm_locations (Section 6), and it does not set up EJ log shipping (Section 4) or ISO 8583 transaction sourcing (Section 5) for the newly discovered ATM. Those remain per-ATM tasks, though Section 6.4's admin form reduces the location-data portion to a short web form entry.

A realistic onboarding workflow for a new branch ATM, once all of Sections 3-7 are in place, looks like:

- ATM is installed and connected to the network by the hardware team
- Within the hour, Zabbix auto-discovery finds it and begins monitoring hardware status - no action needed
- Operations staff fill in the ATM's location details via the Report Portal admin form (Section 6.4) - a few minutes
- If the ATM's EJ logs use the same shipping method already configured (Section 4), they begin flowing automatically; if a new shipping path is needed, it is a one-time Filebeat config addition
- Transactions begin appearing automatically once the ISO 8583 gateway processes messages tagged with the new terminal ID - no per-ATM gateway configuration needed, since the gateway is switch-wide, not per-ATM

## 7.5 Checkpoint

- A test ATM (or a device responding on the expected OID) is automatically discovered and added to Zabbix within one scan cycle
- The auto-registered host has the correct template linked and begins showing data immediately
- The onboarding workflow above has been documented for operations staff, not just engineering

# 8\. Decommissioning the Simulators

This section should only be actioned once real ATMs are confirmed working end-to-end (hardware monitoring, EJ logs, and transactions) for a meaningful portion of the fleet. There is no requirement to do this all at once, and the simulators can be kept running indefinitely for training, demos, or testing - they do not interfere with real ATM data since they use different host entries, source tags, and (if desired) separate Grafana filters.

## 8.1 Recommended Approach: Gradual, Not All-at-Once

Rather than a single cutover, consider this staged approach:

- Keep simulators running alongside real ATMs during the rollout period (weeks to months, depending on rollout pace)
- Use the source field (ISO8583_SIM vs ISO8583_REAL) and host naming (ATM-00X vs real ATM IDs) to filter dashboards as needed during this period
- Once a critical mass of real ATMs are live and stable, stop the simulator containers but leave their configuration in the repository (commented out or in a separate compose override file) for future reference
- Only remove simulator code and Zabbix templates entirely once there is confidence they will not be needed again - for example, after a full quarter of stable real-ATM operation

## 8.2 Stopping Simulator Containers

\# Stop all simulator-related containers without deleting them

docker compose stop atm-sim-001 atm-sim-002 atm-sim-003 atm-sim-004 atm-sim-005 \\

atm-ej-001 atm-ej-002 atm-ej-003 atm-ej-004 atm-ej-005 \\

txn-feed-001 txn-feed-002 txn-feed-003 txn-feed-004 txn-feed-005

\# Optionally set the ISO 8583 gateway to stop generating simulated transactions

\# (only relevant if MODE was left as 'simulation' for any reason)

## 8.3 Cleaning Up Zabbix

- Disable (do not delete) the ATM-001 through ATM-005 hosts - disabling stops monitoring without losing historical data
- Once confident historical simulator data is no longer needed for reference, the HTTP-based "Dashen Bank ATM Hardware" template can be unlinked from disabled hosts
- Keep the SNMP-based template ("Dashen Bank ATM Hardware - SNMP (Real)") as the primary template going forward

## 8.4 Cleaning Up Grafana and the Report Portal

Dashboards and reports do not need structural changes - they already query by date range and will simply stop showing new simulated data once the containers are stopped. If desired, add a filter to exclude the ATM-001 through ATM-005 host names or the ISO8583_SIM source tag from default views, while keeping historical data queryable for anyone who needs it.

## 8.5 Checkpoint

- Simulators are stopped without breaking any dashboard, report, or alert for real ATMs
- Historical simulated data remains queryable if needed, but is excluded from default operational views
- The decision to fully remove simulator code is deferred until real-ATM operation has been stable for an agreed period

# 9\. Backup and Basic Disaster Recovery

On a laptop, losing data is inconvenient. On a production banking system, it is unacceptable. This section sets up the minimum backup practices that should be in place before real ATM data starts flowing - ideally completed during Section 2 (initial server setup), but no later than before Section 3 (first real ATM).

## 9.1 What Needs to Be Backed Up

| **Data**                                                   | **Why it matters**                                                  | **Backup method**                                    |
| ---------------------------------------------------------- | ------------------------------------------------------------------- | ---------------------------------------------------- |
| PostgreSQL (atm_transactions, atm_locations)               | Transaction history, dispute evidence, fleet metadata               | Daily pg_dump, see 9.2                               |
| Zabbix configuration (hosts, templates, triggers, actions) | Recreating monitoring setup after a failure                         | Periodic XML export, see 9.3                         |
| Grafana dashboards                                         | Already version-controlled in the repo (config/grafana/dashboards/) | Git - no additional action needed if kept up to date |
| Elasticsearch EJ data                                      | Dispute investigation history (90-day retention)                    | Elasticsearch snapshots, see 9.4                     |
| GLPI database                                              | Ticket history, SLA records                                         | Database dump, see 9.5                               |

## 9.2 Automated PostgreSQL Backups

The existing scripts/backup_db.sh script already dumps atm_transactions and atm_locations. Schedule it to run daily via cron, and store backups outside the container (and ideally outside the server itself):

crontab -e

\# Add this line - daily backup at 2am

0 2 \* \* \* cd /home/&lt;user&gt;/Dashen-Bank-ATM-monitoring-system && bash scripts/backup_db.sh >> /var/log/atm-backup.log 2>&1

**NOTE:** Off-server storage is essential. A nightly job that copies the backup files to another server, network share, or object storage (e.g. an internal S3-compatible store) protects against the server itself failing. Coordinate with IT Infrastructure on where backups should be sent - this is likely already standardized for other bank systems.

## 9.3 Zabbix Configuration Backups

In addition to the database dumps (which include Zabbix's own configuration tables), periodically re-export the template, hosts, media types, and actions as XML (the same process used in Section 2.5) and commit them to the repository. This ensures that even a full Zabbix database loss can be recovered by re-importing these files onto a fresh Zabbix installation.

## 9.4 Elasticsearch Snapshots

Elasticsearch supports snapshot/restore to a shared filesystem or object storage repository. At minimum, configure a weekly snapshot of the EJ indices:

\# Register a snapshot repository (one-time setup)

curl -X PUT "localhost:9200/\_snapshot/atm_ej_backup" -H 'Content-Type: application/json' -d '{

"type": "fs",

"settings": { "location": "/mnt/es-backups" }

}'

\# Take a snapshot

curl -X PUT "localhost:9200/\_snapshot/atm*ej_backup/snapshot*\$(date +%Y%m%d)?wait_for_completion=false"

The /mnt/es-backups path must be mounted into the Elasticsearch container and should point to storage outside the container's own data volume.

## 9.5 GLPI Database Backup

\# GLPI uses its own MySQL/MariaDB database (glpi-db container)

docker exec glpi-db mysqldump -u root -p&lt;password&gt; glpi | gzip > glpi-backup-\$(date +%Y%m%d).sql.gz

Add this to the same daily cron job as the PostgreSQL backup.

## 9.6 Restore Testing

A backup that has never been restored is not a verified backup. At least once before going fully live with real ATMs, perform a full restore test on a separate machine or VM:

- Clone the repository onto a fresh machine
- Run scripts/setup_new_machine.sh (which calls restore_db.sh)
- Confirm transaction counts, ATM locations, and Zabbix configuration match the production server
- Document how long the restore took - this becomes your Recovery Time Objective (RTO) for planning purposes

## 9.7 Checkpoint

- Daily automated backups of PostgreSQL and GLPI are running and stored off-server
- Weekly Elasticsearch snapshots are configured
- Zabbix configuration XML exports are current in the repository
- A full restore has been tested at least once and the recovery time is documented

# 10\. Final Production Readiness Checklist

Use this checklist as a final review before declaring the production deployment "live" for a given ATM or batch of ATMs. Not every item applies to every stage - use judgment for partial rollouts.

## 10.1 Infrastructure

- Server provisioned with agreed specifications and on the correct network segment
- All Docker containers running and set to restart automatically (restart: unless-stopped)
- Internal DNS names configured (optional but recommended)
- Firewall rules allow only necessary internal traffic; no ports exposed to the internet

## 10.2 Monitoring (Zabbix)

- Real ATM(s) report hardware status via SNMP with correct OID mappings
- Triggers fire correctly for at least: cash low/empty, door open, printer fault, network down
- Auto-discovery is configured and tested for bulk onboarding
- GLPI tickets are created automatically when triggers fire, with correct severity and details

## 10.3 Dashboards (Grafana)

- Real ATMs appear correctly on the geo-map with accurate locations
- ATM Fleet Overview table shows correct status, branch, and performance data for real ATMs
- Drill-down dashboards work for real ATMs

## 10.4 Transactions and EJ

- ISO 8583 Gateway receives and correctly parses real switch messages
- Transaction amounts, statuses, and timestamps match the switch's own records for a sample of transactions
- Real EJ logs are searchable in Kibana with masked card numbers
- EJ retention policy matches the bank's compliance requirements (90 days per BRD, confirm with compliance team)

## 10.5 Reporting

- Report Portal generates correct PDF/Excel/CSV reports including real ATM data
- Scheduled reports (if configured) are delivering to the correct recipients

## 10.6 Security and Compliance

- SNMP community strings (or v3 credentials) are bank-specific, not default values like "public"
- PCI DSS card masking confirmed for real EJ data (Section 4.6)
- Access to Zabbix, Grafana, GLPI, and the Report Portal is restricted to authorized staff (interim measure until Dashen's IAM integration is available)

## 10.7 Backup and Recovery

- Daily database backups running and verified
- Weekly Elasticsearch snapshots running
- Full restore tested at least once with documented recovery time

## 10.8 Documentation and Handover

- README.md in the repository reflects the production environment (server IPs, real OID mappings, switch connection details)
- OID mapping table (Section 3.3) is saved in the repository for future ATM onboarding
- Operations staff have been walked through the onboarding workflow (Section 7.4)
- A short "what to do if X breaks" troubleshooting note exists for the most common failure modes (container crash, agent disconnection, switch connection drop)

**NOTE:** This checklist is intentionally long. It does not need to be completed for the entire fleet before any ATM goes live - a sensible approach is to satisfy all items for the first 1-2 real ATMs as a pilot, then use auto-discovery and the admin form (Sections 6-7) to scale to the rest of the fleet with much less per-ATM effort.