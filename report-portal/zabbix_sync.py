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


def _group_id(z, name, cache, create=True):
    """Host group id, creating it when missing. create=False is read-only and
    returns None instead of writing — that is what makes plan_hosts() a true
    dry run rather than a dry run that quietly creates groups."""
    if name in cache:
        return cache[name]
    res = z.call("hostgroup.get", {"output": ["groupid"], "filter": {"name": [name]}})
    if res:
        cache[name] = res[0]["groupid"]
        return cache[name]
    gid = None
    if create:
        res = z.call("hostgroup.create", {"name": name})
        gid = res["groupids"][0] if res else None
    cache[name] = gid
    return gid


def _host_state(z, host):
    """Current Zabbix state of one host, or None when it does not exist."""
    res = z.call("host.get", {"output": ["hostid", "status", "name"],
                              "selectInterfaces": ["interfaceid", "ip", "details"],
                              "selectMacros": ["macro", "value"],
                              "selectParentTemplates": ["templateid"],
                              "selectGroups": ["groupid"],
                              "filter": {"host": [host]}})
    return res[0] if res else None


def _new_summary():
    return {"created": 0, "reenabled": 0, "macros_fixed": 0, "updated": 0,
            "skipped": 0, "no_ip": 0, "failed": 0, "details": []}


def _host_state(z, host):
    """Current Zabbix state of one host, or None when it does not exist."""
    res = z.call("host.get", {"output": ["hostid", "status", "name"],
                              "selectInterfaces": ["interfaceid", "ip", "details"],
                              "selectMacros": ["macro", "value"],
                              "selectParentTemplates": ["templateid"],
                              "selectGroups": ["groupid"],
                              "filter": {"host": [host]}})
    return res[0] if res else None


def _plan_update(a, cur, tids, groups, create_groups):
    """Reconciliation plan for an EXISTING host. Additive only: the portal
    links/adds what it owns and never unlinks templates or drops groups a human
    may have added. Returns (params_or_None, changed_labels)."""
    upd = {"hostid": cur["hostid"]}
    changed = []
    if str(cur.get("status")) == "1":
        upd["status"] = 0
        changed.append("re-enabled")
    want_name = (a.get("atm_name") or "").strip() or (a.get("atm_id") or "").strip()
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
        if (ifaces[0].get("ip") != (a.get("ip_address") or "").strip()
                or det.get("community") != want_community
                or str(det.get("version")) != str(want_version)):
            upd["interfaces"] = [{
                "interfaceid": ifaces[0]["interfaceid"],
                "ip": (a.get("ip_address") or "").strip(),
                "details": {"version": want_version, "bulk": 0,
                            "community": want_community}}]
            changed.append("interface/community")
    have_macros = {m.get("macro"): m.get("value") for m in (cur.get("macros") or [])}
    vendor = (a.get("vendor") or "").strip()
    if have_macros.get("{$ATM_VENDOR}") != vendor:
        upd["macros"] = [{"macro": "{$ATM_VENDOR}", "value": vendor}]
        changed.append("vendor macro")
    want_tid = (tids.get(TEMPLATE_NCR_NAME) if vendor == "NCR"
                else tids.get(TEMPLATE_GRG_NAME) if vendor == "GRG" else None)
    have_tids = {t.get("templateid")
                 for t in (cur.get("parentTemplates") or []) if t.get("templateid")}
    if want_tid and want_tid not in have_tids:
        upd["templates"] = [{"templateid": want_tid}]
        changed.append("vendor template")
    district = (a.get("district") or "").strip() or "UNKNOWN"
    dgroup = _group_id(_CLIENT[0], f"ATM-{district}", groups, create=create_groups)
    want_groups = {g for g in [groups.get("ATMs"),
                               groups.get("ATM-NCR") if vendor == "NCR"
                               else groups.get("ATM-GRG") if vendor == "GRG" else None,
                               dgroup] if g}
    have_groups = {g.get("groupid") for g in (cur.get("groups") or []) if g.get("groupid")}
    if want_groups and not want_groups <= have_groups:
        upd["groups"] = [{"groupid": g} for g in (have_groups | want_groups)]
        changed.append("groups")
    return (upd if changed else None), changed


def _create_params(a, tids, groups, create_groups=True):
    """host.create payload for a NEW host — byte-for-byte the shape the portal
    has always sent. create_groups=False (dry run) never creates a district
    group; it is reported in pending_groups instead. Returns
    (params, error_or_None, pending_groups)."""
    host = (a.get("atm_id") or "").strip()
    ip = (a.get("ip_address") or "").strip()
    vendor = (a.get("vendor") or "").strip()
    want_tid = (tids.get(TEMPLATE_NCR_NAME) if vendor == "NCR"
                else tids.get(TEMPLATE_GRG_NAME) if vendor == "GRG" else None)
    if not want_tid:
        return None, f"skipped: unknown vendor '{vendor}'", []
    district = (a.get("district") or "").strip() or "UNKNOWN"
    pending = []
    dgroup = _group_id(_CLIENT[0], f"ATM-{district}", groups, create=create_groups)
    if dgroup is None:
        pending.append(f"ATM-{district}")
    community = (a.get("snmp_community") or "").strip() or "dashen_sim"
    try:
        version = int((a.get("snmp_version") or "2"))
    except (ValueError, TypeError):
        version = 2
    params = {
        "host": host,
        "name": (a.get("atm_name") or "").strip() or host,
        "templates": [{"templateid": want_tid}],
        "groups": [{"groupid": g} for g in
                   [groups.get("ATMs"),
                    groups.get("ATM-NCR") if vendor == "NCR" else groups.get("ATM-GRG"),
                    dgroup] if g],
        "interfaces": [{
            "type": 2, "main": 1, "useip": 1, "ip": ip,
            "dns": "", "port": "161",
            "details": {"version": version, "bulk": 0, "community": community},
        }],
        "macros": [{"macro": "{$ATM_VENDOR}", "value": vendor}],
        "status": 0,
    }
    return params, None, pending


# The planners need the API client for host-group lookups. Holder instead of
# threading a client through every signature keeps the payloads testable.
_CLIENT = [None]


def _build_plans(atm_dicts, create_groups=True, max_batch=MAX_BATCH):
    """Read Zabbix state and decide, per ATM, what must change. Only reads,
    plus hostgroup.create when create_groups=True. Returns (summary, plans,
    client); every plan is {atm_id, action, params, changes, error} with
    action in create|update|skipped|no_ip|blank. One calculation feeds both the
    dry run and the real run, so the two can never disagree."""
    summary = _new_summary()
    plans = []
    rows = (atm_dicts or [])[:max_batch]
    if not rows:
        return summary, plans, None
    try:
        z = _Client()
        _CLIENT[0] = z
        if not z.login():
            summary["failed"] = len(rows)
            summary["details"] = [{"atm_id": r.get("atm_id"), "error": "Zabbix login failed"}
                                  for r in rows]
            return summary, plans, z
        tids = _template_ids(z)
        if not tids.get(TEMPLATE_NCR_NAME) and not tids.get(TEMPLATE_GRG_NAME):
            summary["failed"] = len(rows)
            summary["details"] = [{"atm_id": r.get("atm_id"), "error": "ATM templates not found in Zabbix"}
                                  for r in rows]
            return summary, plans, z
        groups = {}
        for gname in ("ATMs", "ATM-NCR", "ATM-GRG"):
            _group_id(z, gname, groups, create=create_groups)
        for a in rows:
            host = (a.get("atm_id") or "").strip()
            if not host:
                plans.append({"atm_id": host, "action": "blank", "params": None,
                              "changes": [], "error": None, "pending_groups": []})
                continue
            if not (a.get("ip_address") or "").strip():
                plans.append({"atm_id": host, "action": "no_ip", "params": None,
                              "changes": [], "error": "skipped: no IP address", "pending_groups": []})
                continue
            cur = _host_state(z, host)
            if cur:
                upd, changed = _plan_update(a, cur, tids, groups, create_groups)
                if changed:
                    plans.append({"atm_id": host, "action": "update", "params": upd,
                                  "changes": changed, "error": None})
                else:
                    plans.append({"atm_id": host, "action": "skipped", "params": None,
                                  "changes": [], "error": None, "pending_groups": []})
                continue
            params, err, pending = _create_params(a, tids, groups, create_groups)
            if err:
                plans.append({"atm_id": host, "action": "skipped", "params": None,
                              "changes": [], "error": err})
                continue
            plans.append({"atm_id": host, "action": "create", "params": params,
                          "changes": ["host"], "error": None, "pending_groups": pending})
    except Exception as e:
        logger.warning("host planning aborted: %s", e)
    return summary, plans, _CLIENT[0]


def _summarise(plans):
    """Planned-outcome summary (what a dry run reports)."""
    summary = _new_summary()
    for p in plans:
        act = p["action"]
        if act == "create":
            summary["created"] += 1
        elif act == "update":
            summary["updated"] += 1
        elif act == "no_ip":
            summary["no_ip"] += 1
            summary["details"].append({"atm_id": p["atm_id"], "error": p["error"]})
        elif act in ("skipped", "blank"):
            summary["skipped"] += 1
            if p["error"]:
                summary["details"].append({"atm_id": p["atm_id"], "error": p["error"]})
    return summary


def plan_hosts(atm_dicts):
    """DRY RUN. What would change in Zabbix for these ATMs, changing nothing.
    Returns (summary, plans) — the summary counts would-be outcomes."""
    summary, plans, _z = _build_plans(atm_dicts, create_groups=False)
    if not plans and summary["failed"]:
        return summary, plans
    return _summarise(plans), plans


def _sync(atm_dicts):
    """Plan, then apply. Returns (summary, applied) where applied[i] is the
    plan plus `applied` (bool) and `error` (str) so a caller can report what
    actually happened per ATM, not just totals."""
    summary = _new_summary()
    rows = (atm_dicts or [])[:MAX_BATCH]
    if not rows:
        return summary, []
    planned, plans, z = _build_plans(atm_dicts, create_groups=True)
    if not plans and planned["failed"]:
        return planned, []
    applied = []
    try:
        if z is None or not z.login():
            summary["failed"] = len(rows)
            summary["details"] = [{"atm_id": r.get("atm_id"), "error": "Zabbix login failed"}
                                  for r in rows]
            return summary, applied
        for p in plans:
            act, host = p["action"], p["atm_id"]
            rec = {"atm_id": host, "action": act, "changes": p.get("changes") or [],
                   "pending_groups": p.get("pending_groups") or [], "applied": False,
                   "error": p.get("error") or ""}
            if act == "no_ip":
                summary["no_ip"] += 1
                summary["details"].append({"atm_id": host, "error": rec["error"]})
                applied.append(rec)
                continue
            if act in ("skipped", "blank"):
                summary["skipped"] += 1
                if rec["error"]:
                    summary["details"].append({"atm_id": host, "error": rec["error"]})
                applied.append(rec)
                continue
            if act == "create":
                if z.call("host.create", p["params"]):
                    summary["created"] += 1
                    rec["applied"] = True
                else:
                    summary["failed"] += 1
                    rec["error"] = z.last_error or "host.create failed"
                    summary["details"].append({"atm_id": host, "error": rec["error"]})
                applied.append(rec)
                continue
            if z.call("host.update", p["params"]):
                rec["applied"] = True
                if "re-enabled" in p["changes"]:
                    summary["reenabled"] += 1
                else:
                    summary["updated"] += 1
            else:
                summary["failed"] += 1
                rec["error"] = (z.last_error or "host.update failed") + \
                    f" (wanted: {', '.join(p['changes'])})"
                summary["details"].append({"atm_id": host, "error": rec["error"]})
            applied.append(rec)
    except Exception as e:
        logger.warning("sync_hosts aborted: %s", e)
        summary["failed"] += (len(rows) - summary["created"] - summary["reenabled"]
                              - summary["macros_fixed"] - summary["updated"]
                              - summary["skipped"] - summary["no_ip"])
    return summary, applied


def sync_hosts(atm_dicts):
    """Create missing Zabbix hosts; re-enable ones disabled by portal delete.
    Never raises. Same summary keys as before the plan/apply split."""
    return _sync(atm_dicts)[0]


def sync_hosts_detailed(atm_dicts):
    """Same as sync_hosts but also returns the per-ATM outcome, for callers
    that must show what happened (onboarding wizard, repair report)."""
    return _sync(atm_dicts)


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
