# Zabbix Proxy Topology — Dashen ATM Fleet

## Source
Abinet's ATM inventory Excel — 1,202 ATMs across 14 districts.

## Design Constraints
- 5-sec polling freshness per BRD §7.2 → each proxy handles ≤250 ATMs
- Private IPs (172.x.x.x, 192.x.x.x) → proxy must be in same L2/L3 segment
- 19 admin districts consolidated into 14 normalized districts, then into 8 proxies

## Proxy Map

| Proxy | Region | Districts | ATMs | Suggested Host |
|---|---|---|---|---|
| P01-Addis-North | Addis Ababa | NAD | 149 | `zabbix-proxy-addis-north` |
| P02-Addis-South | Addis Ababa | SAD | 135 | `zabbix-proxy-addis-south` |
| P03-Addis-East | Addis Ababa | EAD | 134 | `zabbix-proxy-addis-east` |
| P04-Addis-West | Addis Ababa | WAD | 110 | `zabbix-proxy-addis-west` |
| P05-Hawassa | SNNP | HAWASA, WOLAITA, SOUTH WEST | 218 | `zabbix-proxy-hawassa` |
| P06-Adama | Oromia | ADAMA, NEKEMTE, JIMMA | 156 | `zabbix-proxy-adama` |
| P07-Dessie | Amhara | DESSIE, DIRE DAWA | 153 | `zabbix-proxy-dessie` |
| P08-BahirDar | Amhara/Tigray | BAHIR DAR, MEKELLE | 147 | `zabbix-proxy-bahirdar` |

## District-to-Proxy Mapping (for scripts/sync_atms_to_zabbix.py)

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

## Pilot Recommendation
Start with **P01-Addis-North (NAD)** — 149 ATMs, all within Addis Ababa, convenient for on-site validation. Deploy proxy on a VM at the NAD data centre or branch.

## Scale-Up Path
1. Pilot: 1 proxy (NAD, 149 ATMs)
2. Addis full: +3 proxies (SAD, EAD, WAD) = 528 ATMs
3. Regional: +4 proxies (Hawassa, Adama, Dessie, Bahir Dar) = 1,202 ATMs

## Hardware Suggestion per Proxy
- 4 vCPU, 8 GB RAM, 80 GB SSD
- Ubuntu 22.04 LTS, Zabbix proxy 6.4 (active or passive)
- Must have L2/L3 reachability to ATMs' private IPs
