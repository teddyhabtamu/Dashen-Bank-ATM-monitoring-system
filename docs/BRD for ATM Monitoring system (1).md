

<!-- Start of picture text -->
| AT >>><br>Face ed oo — o<<br>SEIZING THE NEC VV<br>~~ <><br>RESY <= RES<br>SS, SESDSix T E AA<br><!-- End of picture text -->







## **1. Background** 

Banks operate multiple Automated Teller Machines (ATMs) across different locations. Managing ATM availability, cash levels, transaction errors, and security incidents manually or through delayed reports can lead to service interruptions and customer dissatisfaction. 

A **Real-Time ATM Monitoring Tool** is required to provide instant visibility of ATM operations, enabling faster decision-making, improved uptime, and efficient operational control. 

The proposed solution shall provide end-to-end monitoring and visibility of the Bank’s ATM network, including transactional monitoring, network monitoring, electronic journal (EJ) management, camera monitoring, and alerting, reporting, and mobile access. 

# **2. Business Objectives** 

The system should achieve the following objectives: 

1. Ensure high ATM availability by detecting issues instantly. 

2. Monitor ATM transactions in real time. 

3. Track ATM cash levels to prevent cash-out situations. 

4. Generate operational and management reports. 

5. Improve incident response time. 

6. Enhance fraud monitoring and security oversight. 

7. Provide centralized dashboard monitoring for all ATMs. 

# **3. Scope of the System** 

## **3.1 In Scope** 

- <mark>Real-tme ATM transacton monitoring</mark> 

- ATM status monitoring (online/offline) 

- <mark>Cash level monitoring</mark> 

- ATM error alerting 

- Dashboard visualization 

- Automated alerts and notifications 

- Operational and management reporting 

## **3.2 Out of Scope** 

- ATM hardware maintenance 

- ATM transaction processing system development 

- Core banking transaction processing 

# **4. Key Stakeholders** 

- Channel Support Department 

- IT Infrastructure Department 

- Risk and Compliance Office 

- Bank Management 

- ATM Service Providers 

- Cash Operations Teams 

# **5. Functional Requirements** 

## **5.1 ATM Status Monitoring** 

The system shall: 

- Display **online/offline status of ATMs in real time** . 

- Show ATM location, Branch, District, Terminal ID, and operational status. 

- Provide alerts when: Cash reaches its minimum balance; ATM goes offline; ATM connection is unstable; ATM device failure occurs 

## **5.2 Transaction Monitoring** 

The system shall monitor ATM transactions such as: 

- Cash withdrawal 

- <mark>Balance inquiry</mark> 

- Mini statement 

- Card less transactions 

- Failed transactions 

The system shall display: 

- Transaction time 

- ATM Terminal ID 

- Transaction type 

- Transaction status 

- Error codes 

## **5.3 Cash Level Monitoring** 

The system shall: 

- Monitor ATM . **cassette cash levels in real time** 

- Provide alerts when: 

- Cash level is low 

- ATM is out of cash 

- Provide forecast of . **cash depletion based on withdrawal patterns** 

## **5.4 ATM Error Monitoring** 

The system shall detect and report ATM errors including and **Alerts should be automatically** 

**generated.** 

- Cash dispenser failure 

- Card reader failure 

- Printer failure 

- Network connectivity issues 

- Software errors 

## **<mark>5.5 Alert and Notification System</mark>** 

The system shall send alerts through: 

- Email notifications 

- SMS alerts 

- Dashboard notifications 

Alert triggers include: 

- ATM offline 

- Cash out 

- Hardware error 

- Suspicious transaction pattern 

## **<mark>5.6 Real-Time Dashboard</mark>** 

The dashboard shall display: 

- Total number of ATMs 

- Number of online ATMs 

- Number of offline ATMs 

- ATMs with low cash 

- Total transactions 

- Failed transactions 

Dashboard should allow filtering by: 

- District 

- Branch 

- ATM location 

- <mark>ATM status</mark> 

# **6. Reporting Requirements** 

The system shall generate the following reports. 

## **6.1 ATM Availability Report** 

Shows: 

- ATM uptime percentage 

- ATM downtime duration 

- Reasons for downtime 

Frequency: 

- Daily 

- Weekly 

- Monthly 

## **6.2 Transaction Summary Report** 

Shows: 

- Total transactions per ATM 

- Transaction success rate 

- Failed transactions 

- Transaction value 

## **6.3 Cash Level Report** 

Shows: 

- Current cash level 

- <mark>Cash dispensed</mark> 

- <mark>Cash replenishment history</mark> 

## **<mark>6.4 Error and Incident Report</mark>** 

Shows: 

- <mark>ATM errors</mark> 

- <mark>Incident resoluton tme</mark> 

- <mark>Error frequency</mark> 

## **<mark>6.5 ATM Performance Report</mark>** 

Includes: 

- Most used ATMs 

- Least used ATMs 

- Peak transaction hours 

# **7. Non-Functional Requirements** 

## **7.1 System Availability** 

- <mark>System availability should be</mark> **<mark>99.9% uptme</mark>** <mark>.</mark> 

## **7.2 Performance** 

- <mark>Monitoring data should update</mark> **<mark>within 5 seconds</mark>** <mark>of event occurrence.</mark> 

## **7.3 Security** 

- <mark>Role-based access control</mark> 

- <mark>Data encrypton</mark> 

- <mark>Audit logging of system actvites</mark> 

## **<mark>7.4 Scalability</mark>** 

The system should support: 

- Expansion of ATM network 

- <mark>Integraton with additonal monitoring tools</mark> 

### **<mark>8. Integration Requirements</mark>** 

The monitoring tool should integrate with: 

- ATM Switch system 

- <mark>Core Banking System</mark> 

- <mark>Card Management System</mark> 

- <mark>SMS and Email notfcaton systems</mark> 

   **<mark>9. User Roles</mark>** 

### **System Administrator** 

- <mark>Manage users</mark> 

- <mark>Confgure alerts</mark> 

- <mark>System confguraton</mark> 

### **ATM Operations Officer** 

- <mark>Monitor ATM status</mark> 

- <mark>Generate reports</mark> 

- <mark>Respond to alerts</mark> 

### **Management** 

- <mark>View dashboards</mark> 

- <mark>Access performance reports</mark> 

### **<mark>10. Risk and Compliance</mark>** 

The system must comply with: 

- Banking security standards 

- <mark>AML monitoring requirements</mark> 

- <mark>Internal bank operational policies</mark> 

|No|**Dashen Bank Minimum**<br>**Requirement**|**Detail Inclusive:**|**Requirement**<br>**Level:**<br>**M = Mandatory**|
|---|---|---|---|
|1.|**ATM Health & Status Monitoring**|The system shall:|**M**|
|||<br>Monitor ATM operational<br>status in real time:<br>`o`<br>In Service<br>`o`<br>Out of Service<br>`o`<br>Partial Service<br>`o`<br>Supervisor /<br>Maintenance Mode||
|||`o`<br>Agent Disconnected<br><br>Detect ATM application<br>failures and reboot events<br><br>Support configurable polling<br>intervals||



||||Maintain historical ATM<br>health records||
|---|---|---|---|---|
|2.|**Cash Dispenser**|<br><br><br><br>|Cassette-wise cash level<br>monitoring<br>Cash low and cash empty<br>thresholds<br>Reject bin status<br>Cash jam detection<br>Partial dispense detection|**M**|
|3.|**Card Handling**|<br><br>|Card reader health<br>Card capture incidents<br>Shutter failure detection|**M**|
|4.|**Printers**|<br><br>|Receipt printer paper level<br>Journal printer status<br>Printer fault detection||
|5.|**Security & Sensors**|<br>|Safe door and cabinet door<br>status<br>Temperature and humidity<br>monitoring|**M**|
|||Vibra|tion and intrusion alerts||
|6.|**Transactional Monitoring**|The s<br>transa<br><br><br><br>|ystem shall provide end-to-end<br>ctional visibility and analytics:<br>Real-time monitoring of<br>ATM transactions<br>Support ISO 8583 message<br>monitoring and parsing<br>Track transaction outcomes:<br>`o`<br>Successful<br>`o`<br>Failed<br>`o`<br>Reversed<br>`o`<br>Timed-out<br>Categorize failures:<br>`o`<br>Host decline<br>`o`<br>Network timeout<br>`o`<br>Hardware error<br>`o`<br>Cash dispense failure|**M**|



||<br>Detect abnormal transaction<br>patterns<br><br>Support masked PAN-based<br>transaction search<br><br>Correlate transaction failures<br>with ATM, network, and<br>power events||
|---|---|---|
|7.<br>**Network Monitoring**|The system shall:<br><br>Monitor ATM network<br>connectivity end-to-end<br><br>Track:<br>`o`<br>Link up/down status<br>`o`<br>Latency<br>`o`<br>Packet loss<br>`o`<br>Jitter<br><br>Support MPLS, VSAT,<br>GSM/LTE, and Fiber links<br><br>Generate alerts for network<br>degradation or outage<br><br>Correlate network issues<br>with transaction failures|**M**|
|8.<br>**Electronic Journal (EJ)**<br>**Management**|The system shall:<br><br>Automatically collect EJ<br>logs from ATMs<br><br>Support multi-vendor EJ<br>formats<br><br>Enable EJ search by:<br>`o`<br>ATM ID<br>`o`<br>Date and time<br>`o`<br>Transaction reference<br>`o`<br>Masked card number<br><br>Correlate EJ data with<br>switch and transaction<br>records<br><br>Support dispute resolution<br>and audit investigations<br><br>Allow secure EJ export and<br>retention as per regulatory<br>requirements|M|



|9.|**Camera & Surveillance**<br>**Monitoring**|The system shall:<br><br>Monitor ATM camera<br>availability and health<br><br>Detect camera and storage<br>failures<br><br>Generate alerts for camera<br>tampering or downtime<br><br>Support snapshot or video<br>retrieval (subject to<br>regulatory approval)<br><br>Correlate camera events with<br>ATM incidents<br><br>Capturing image during<br>Balance Check|**M**|
|---|---|---|---|
|10.|**Power & Environmental**<br>**Monitoring**|The system shall:<br><br>Monitor main power and<br>UPS status<br><br>Track battery health and<br>power fluctuation events<br><br>Correlate power failures with<br>ATM downtime<br><br>Support environmental alerts<br>(temperature, flood, etc., if<br>sensors exist)|**M**|
|11.|**Alert & Notification**<br>**Management**|The system shall:<br><br>Generate real-time alerts for:<br>`o`<br>ATM downtime<br>`o`<br>Component failures<br>`o`<br>Transaction<br>anomalies<br>`o`<br>Network outages<br>`o`<br>Cash low / empty<br>`o`<br>Camera failures<br><br>Support alert delivery via:<br>`o`<br>Email<br>`o`<br>SMS<br>`o`<br>Mobilepush||



|||notifications<br><br>Support severity levels and<br>configurable escalation rules<br><br>Allow alert acknowledgment<br>and suppression||
|---|---|---|---|
|12.|**Incident, Ticket & SLA**<br>**Management**|The system shall:<br><br>Automatically create<br>incident tickets from alerts<br><br>Support manual ticket<br>creation<br><br>Track full incident lifecycle:<br>`o`<br>Open<br>`o`<br>Acknowledged<br>`o`<br>In Progress<br>`o`<br>Resolved<br>`o`<br>Closed<br><br>Assign incidents to internal<br>teams or vendors<br><br>Track response and<br>resolution SLAs<br><br>Require Root Cause<br>Analysis (RCA) before ticket<br>closure<br><br>Support document and image<br>attachments|M|
|13.|**Mobile Application**<br>**Requirements**|The solution shall include a mobile<br>application that:<br><br>Supports Android<br>(mandatory) and iOS<br>(preferred)<br><br>Displays real-time ATM<br>status and alerts<br><br>Enables ticket<br>acknowledgment and<br>updates<br><br>Allows field engineers to<br>upload photos and notes<br><br>Supports role-based access<br>and offline operation|M|



|14.|**Dashboards & Visualization**|The system shall provide:|M|
|---|---|---|---|
|||<br>Real-time operational<br>dashboards<br><br>Management KPI<br>dashboards<br><br>Geo-map visualization of<br>ATMs<br><br>Drill-down views from<br>summary to ATM level<br><br>Customizable widgets and<br>filters||
|15.|**Transaction Monitoring and**<br>**Reporting & Analytics**|The system shall provide:<br><br>ATM uptime and availability<br>reports<br><br>Downtime reason analysis<br><br>SLA compliance reports<br><br>Vendor performance reports<br><br>Transaction success and<br>failure analysis<br><br>Cash-out frequency reports<br><br>Export in PDF, Excel, and<br>CSV formats<br><br>Scheduled and on-demand<br>reporting<br><br>Periodic report with duration<br>for unresolved issues|M|
|16.|**Integration Requirements**|The system shall integrate with:<br><br>ATM Switch<br><br>Core Banking System (read-<br>only)<br><br>Vendor monitoring tools<br><br>Network monitoring systems<br>Supported standards include:<br><br>ISO 8583<br><br>SNMP|M|



||<br>|NDC / DDC<br>REST / SOAP APIs||
|---|---|---|---|
|17.|**Security And Compliance**|The system shall:<br><br>Enforce role-based<br>access control<br>(RBAC)<br><br>Maintain full audit<br>trails<br><br>Encrypt data in<br>transit and at rest<br><br>Comply with PCI<br>DSS standards<br><br>Comply with Central<br>Bank and regulatory<br>requirements<br><br>Support secure data<br>retention and<br>archiving|M|
|18.|**Non-Functional**<br>**Requirements**|<br>System availability ≥<br>99.9%<br><br>High availability and<br>redundancy<br><br>Disaster recovery<br>and backup<br>capability<br><br>Scalable architecture<br><br>Web-based solution<br>(no thick client)|M|
|19.|**Vendor Responsibilities**|The vendor shall provide:<br><br>Detailed solution<br>architecture<br><br>Implementation and<br>rollout plan<br><br>Integration and<br>testing support<br><br>User training and<br>documentation|M|



|||<br>Ongoing support and<br>maintenance<br><br>Defined support SLA||
|---|---|---|---|
|19.|**Proposal Submission**<br>**Requirements**|Vendors shall submit:<br><br>Technical proposal<br><br>Commercial proposal<br><br>Implementation<br>timeline<br><br>Company profile and<br>references<br><br>Compliance matrix<br>against RFP<br>requirements|M|



