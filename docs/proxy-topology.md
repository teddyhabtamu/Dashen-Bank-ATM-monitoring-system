# Zabbix Proxy Topology — Phase 2 Scalability Option

> **Status:** Deferred to Phase 2. The initial production rollout (45-day plan) uses a **centralized architecture** without proxies. See `docs/uat-pilot-checklist.md` for the Phase 1 plan.

## Rationale — Why Centralized First

Based on the senior engineer review, proxies add significant complexity for a 45-day timeline:

- Server procurement at each district
- Linux install and proxy configuration
- Certificate/PSK management
- Firewall rules for each proxy
- Proxy monitoring and maintenance

A single Zabbix server (16 vCPU, 64 GB PostgreSQL) comfortably handles 1,200 ATMs × 40 items = 48,000 items. People routinely run 20,000–50,000 devices from one server.

## When to Introduce Proxies

If centralized monitoring shows:

- SNMP polling delays (>3 s per ATM)
- Packet loss across WAN links
- WAN congestion from polling traffic
- Unreachable district clusters

Then introduce proxies **incrementally** — one district at a time — not all 14 at once.

## Proxy Design (if needed later)

### District-to-Proxy Mapping (for scripts/sync_atms_to_zabbix.py)

```
DISTRICT_PROXY = {
    'NAD':     'zabbix-proxy-addis-north',
    'SAD':     'zabbix-proxy-addis-south',
    'EAD':     'zabbix-proxy-addis-east',
    'WAD':     'zabbix-proxy-addis-west',
    'HAWASA':  'zabbix-proxy-hawassa',
    'WOLAITA': 'zabbix-proxy-hawassa',
    'SOUTH WEST': 'zabbix-proxy-hawassa',
    'ADAMA':   'zabbix-proxy-adama',
    'NEKEMTE': 'zabbix-proxy-adama',
    'JIMMA':   'zabbix-proxy-adama',
    'DESSIE':  'zabbix-proxy-dessie',
    'DIRE DAWA': 'zabbix-proxy-dessie',
    'BAHIR DAR': 'zabbix-proxy-bahirdar',
    'MEKELLE': 'zabbix-proxy-bahirdar',
}
```

### Pilot Recommendation (Phase 2)

Start with **one district** experiencing the worst polling latency. Deploy a single proxy there and measure improvement before expanding.

### Scale-Up Path (Phase 2)

1. Pilot: 1 proxy (worst-performing district)
2. Addis full: +3 proxies (NAD, SAD, EAD, WAD) = 528 ATMs
3. Regional: +4 proxies (Hawassa, Adama, Dessie, Bahir Dar) = 1,202 ATMs

### Hardware Suggestion per Proxy

- 4 vCPU, 8 GB RAM, 80 GB SSD
- Ubuntu 22.04 LTS or RHEL 9
- Must have L2/L3 reachability to ATMs' private IPs
