"""
zabbix_sync.py — shared Zabbix host-sync logic for the ATM Monitoring System.

Extracted from scripts/sync_atms_to_zabbix.py so the portal can create /
disable hosts server-side (no SSH, no CSV refresh). All entry points are
best-effort: they return a summary dict and never raise — a Zabbix outage
must never fail a portal save.

Env (already in docker-compose for report-portal):
  ZABBIX_URL / ZBX_URL, ZABBIX_USER / ZBX_USER, ZABBIX_PASS / ZBX_PASS
"""
import logging
import os

import requests

logger = logging.getLogger(__name__)

TEMPLATE_NCR_NAME = "Dashen Bank ATM Hardware"
TEMPLATE_GRG_NAME = "Dashen Bank ATM Hardware - GRG"

TIMEOUT = int(os.environ.get("ZABBIX_SYNC_TIMEOUT", "8"))
MAX_BATCH = int(os.environ.get("ZABBIX_SYNC_MAX_BATCH", "200"))


def _cfg():
    url = os.environ.get("ZABBIX_URL") or os.environ.get("ZBX_URL") or \
        "http://zabbix-web:8080/api_jsonrpc.php"
    user = os.environ.get("ZABBIX_USER") or os.environ.get("ZBX_USER") or "Admin"
    pwd = os.environ.get("ZABBIX_PASS") or os.environ.get("ZBX_PASS") or ""
    return url, user, pwd


class _Client:
    def __init__(self):
        self.url, self.user, self.pwd = _cfg()
        self.token = None
        self.session = requests.Session()
        self.last_error = ""

    def call(self, method, params):
        if method != "user.login" and not self.token:
            self.login()
            if not self.token:
                return None
        try:
            r = self.session.post(self.url, json={
                "jsonrpc": "2.0", "method": method,
                "params": params, "auth": self.token, "id": 1,
            }, timeout=TIMEOUT)
            data = r.json()
            if "error" in data:
                self.last_error = str(data["error"].get("data") or data["error"].get("message") or data["error"])
                logger.warning("Zabbix API [%s]: %s", method, self.last_error)
                return None
            self.last_error = ""
            return data.get("result")
        except Exception as e:
            self.last_error = str(e)
            logger.warning("Zabbix API [%s] failed: %s", method, e)
            return None

    def login(self):
        try:
            r = self.session.post(self.url, json={
                "jsonrpc": "2.0", "method": "user.login",
                "params": {"username": self.user, "password": self.pwd}, "id": 1,
            }, timeout=TIMEOUT)
            self.token = r.json().get("result")
        except Exception as e:
            logger.warning("Zabbix login failed: %s", e)
            self.token = None
        return self.token


def _template_ids(z):
    out = {}
    for name in (TEMPLATE_NCR_NAME, TEMPLATE_GRG_NAME):
        res = z.call("template.get", {"output": ["templateid"], "filter": {"name": [name]}})
        out[name] = res[0]["templateid"] if res else None
    return out


def _group_id(z, name, cache):
    if name in cache:
        return cache[name]
    res = z.call("hostgroup.get", {"output": ["groupid"], "filter": {"name": [name]}})
    if res:
        cache[name] = res[0]["groupid"]
        return cache[name]
    res = z.call("hostgroup.create", {"name": name})
    gid = res["groupids"][0] if res else None
    cache[name] = gid
    return gid


def sync_hosts(atm_dicts):
    """Create missing Zabbix hosts; re-enable ones disabled by portal delete. Never raises."""
    summary = {"created": 0, "reenabled": 0, "macros_fixed": 0, "updated": 0,
               "skipped": 0, "no_ip": 0, "failed": 0, "details": []}
    rows = (atm_dicts or [])[:MAX_BATCH]
    if not rows:
        return summary
    try:
        z = _Client()
        if not z.login():
            summary["failed"] = len(rows)
            summary["details"] = [{"atm_id": r.get("atm_id"), "error": "Zabbix login failed"} for r in rows]
            return summary
        tids = _template_ids(z)
        if not tids.get(TEMPLATE_NCR_NAME) and not tids.get(TEMPLATE_GRG_NAME):
            summary["failed"] = len(rows)
            summary["details"] = [{"atm_id": r.get("atm_id"), "error": "ATM templates not found in Zabbix"} for r in rows]
            return summary
        groups = {}
        grp_all = _group_id(z, "ATMs", groups)
        grp_ncr = _group_id(z, "ATM-NCR", groups)
        grp_grg = _group_id(z, "ATM-GRG", groups)
        for a in rows:
            host = (a.get("atm_id") or "").strip()
            ip = (a.get("ip_address") or "").strip()
            vendor = (a.get("vendor") or "").strip()
            if not host:
                summary["skipped"] += 1
                continue
            if not ip:
                summary["no_ip"] += 1
                summary["details"].append({"atm_id": host, "error": "skipped: no IP address"})
                continue
            existing = z.call("host.get", {"output": ["hostid", "status", "name"],
                                              "selectInterfaces": ["interfaceid", "ip", "details"],
                                              "selectMacros": ["macro", "value"],
                                              "selectParentTemplates": ["templateid"],
                                              "selectGroups": ["groupid"],
                                              "filter": {"host": [host]}})
            if existing:
                # Full reconciliation: an edit in the portal must reach the
                # host (name, IP/community, vendor macro, template, groups).
                # Additive only — we link/add what the portal owns and never
                # unlink templates or drop groups a human may have added.
                cur = existing[0]
                upd = {"hostid": cur["hostid"]}
                changed = []
                if str(cur.get("status")) == "1":
                    upd["status"] = 0
                    changed.append("re-enabled")
                want_name = (a.get("atm_name") or "").strip() or host
                if (cur.get("name") or "") != want_name:
                    upd["name"] = want_name
                    changed.append("visible name")
                want_community = (a.get("snmp_community") or "").strip() or "dashen_sim"
                try:
                    want_version = int(a.get("snmp_version") or "2")
                except (ValueError, TypeError):
                    want_version = 2
                ifaces = cur.get("interfaces") or []
                if ifaces:
                    det = ifaces[0].get("details") or {}
                    if (ifaces[0].get("ip") != ip
                            or det.get("community") != want_community
                            or str(det.get("version")) != str(want_version)):
                        upd["interfaces"] = [{
                            "interfaceid": ifaces[0]["interfaceid"], "ip": ip,
                            "details": {"version": want_version, "bulk": 0,
                                        "community": want_community}}]
                        changed.append("interface/community")
                have_macros = {m.get("macro"): m.get("value")
                               for m in (cur.get("macros") or [])}
                if have_macros.get("{$ATM_VENDOR}") != vendor:
                    upd["macros"] = [{"macro": "{$ATM_VENDOR}", "value": vendor}]
                    changed.append("vendor macro")
                want_tid = (tids.get(TEMPLATE_NCR_NAME) if vendor == "NCR"
                            else tids.get(TEMPLATE_GRG_NAME) if vendor == "GRG"
                            else None)
                have_tids = {t.get("templateid")
                             for t in (cur.get("parentTemplates") or []) if t.get("templateid")}
                if want_tid and want_tid not in have_tids:
                    upd["templates"] = [{"templateid": want_tid}]
                    changed.append("vendor template")
                want_vgroup = (grp_ncr if vendor == "NCR"
                               else grp_grg if vendor == "GRG" else None)
                district = (a.get("district") or "").strip() or "UNKNOWN"
                dgroup = _group_id(z, f"ATM-{district}", groups)
                want_groups = {g for g in [grp_all, want_vgroup, dgroup] if g}
                have_groups = {g.get("groupid")
                               for g in (cur.get("groups") or []) if g.get("groupid")}
                if not want_groups <= have_groups:
                    upd["groups"] = [{"groupid": g} for g in (have_groups | want_groups)]
                    changed.append("groups")
                if not changed:
                    summary["skipped"] += 1
                else:
                    res = z.call("host.update", upd)
                    if res:
                        if "re-enabled" in changed:
                            summary["reenabled"] += 1
                        else:
                            summary["updated"] += 1
                    else:
                        summary["failed"] += 1
                        summary["details"].append({
                            "atm_id": host,
                            "error": (z.last_error or "host.update failed")
                                     + f" (wanted: {', '.join(changed)})"})
                continue
            if vendor == "NCR" and tids.get(TEMPLATE_NCR_NAME):
                template_ids, vgroup = [tids[TEMPLATE_NCR_NAME]], grp_ncr
            elif vendor == "GRG" and tids.get(TEMPLATE_GRG_NAME):
                template_ids, vgroup = [tids[TEMPLATE_GRG_NAME]], grp_grg
            else:
                summary["skipped"] += 1
                summary["details"].append({"atm_id": host, "error": f"skipped: unknown vendor '{vendor}'"})
                continue
            name = (a.get("atm_name") or "").strip() or host
            community = (a.get("snmp_community") or "").strip() or "dashen_sim"
            try:
                version = int((a.get("snmp_version") or "2"))
            except (ValueError, TypeError):
                version = 2
            district = (a.get("district") or "").strip() or "UNKNOWN"
            dgroup = _group_id(z, f"ATM-{district}", groups)
            params = {
                "host": host, "name": name,
                "templates": [{"templateid": t} for t in template_ids],
                "groups": [{"groupid": g} for g in [grp_all, vgroup, dgroup] if g],
                "interfaces": [{
                    "type": 2, "main": 1, "useip": 1, "ip": ip,
                    "dns": "", "port": "161",
                    "details": {"version": version, "bulk": 0, "community": community},
                }],
                "macros": [{"macro": "{$ATM_VENDOR}", "value": vendor}],
                "status": 0,
            }
            res = z.call("host.create", params)
            if res:
                summary["created"] += 1
            else:
                summary["failed"] += 1
                summary["details"].append({"atm_id": host, "error": z.last_error or "host.create failed"})
    except Exception as e:
        logger.warning("sync_hosts aborted: %s", e)
        summary["failed"] += (len(rows) - summary["created"] - summary["reenabled"]
                              - summary["macros_fixed"] - summary["updated"]
                              - summary["skipped"] - summary["no_ip"])
    return summary


def disable_hosts(atm_ids):
    """Disable (not delete) Zabbix hosts so history is preserved. Never raises."""
    summary = {"disabled": 0, "skipped": 0, "failed": 0, "details": []}
    ids = [i.strip() for i in (atm_ids or []) if (i or "").strip()][:MAX_BATCH]
    if not ids:
        return summary
    try:
        z = _Client()
        if not z.login():
            summary["failed"] = len(ids)
            return summary
        for host in ids:
            existing = z.call("host.get", {"output": ["hostid"], "filter": {"host": [host]}})
            if not existing:
                summary["skipped"] += 1
                continue
            res = z.call("host.update", {"hostid": existing[0]["hostid"], "status": 1})
            if res:
                summary["disabled"] += 1
            else:
                summary["failed"] += 1
                summary["details"].append({"atm_id": host, "error": "host.update failed"})
    except Exception as e:
        logger.warning("disable_hosts aborted: %s", e)
    return summary


def host_exists_map(atm_ids):
    """Return {atm_id: bool} for UI badges. Never raises."""
    out = {i: False for i in (atm_ids or [])}
    if not out:
        return out
    try:
        z = _Client()
        if not z.login():
            return out
        res = z.call("host.get", {"output": ["host"], "filter": {"host": list(out)}})
        for h in res or []:
            out[h["host"]] = True
    except Exception as e:
        logger.warning("host_exists_map failed: %s", e)
    return out
