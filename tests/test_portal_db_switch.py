"""Guards the switch itself: which services follow PORTAL_DB_NAME, and which
must never do so.

Getting this wrong is silent in both directions. A writer left behind keeps
writing to the database the portal no longer reads, so its data simply stops
appearing - no error, no failed health check, just a page that quietly stops
updating. A Zabbix service pointed at the portal database loses its entire
configuration, which is louder but just as fatal.

The writers live in two compose files. iso8583-gateway is in the second one,
runs on a different host, and writes atm_transactions - which makes it the one
most likely to be forgotten, and also the one feeding the demo.
"""
import pathlib
import re

import pytest

yaml = pytest.importorskip('yaml')
REPO = pathlib.Path(__file__).resolve().parent.parent

# Services that write portal tables and must follow the switch.
PORTAL_WRITERS = {
    'report-portal',        # audit_log, app_users, atm_anomalies, ...
    'cash-watcher',         # atm_cassette_levels, cash_replenishments
    'anomaly-detector',     # atm_anomalies
    'network-correlator',   # atm_network_metrics, _correlation, _events
    'state-manager',        # atm_current_state, atm_state_history
}

# Services that must stay on the Zabbix database no matter what.
MUST_NOT_SWITCH = {
    'postgres', 'zabbix-server', 'zabbix-web', 'zabbix-agent', 'nginx',
    'opensearch', 'opensearch-dashboards',
}


def services(f):
    return (yaml.safe_load((REPO / f).read_text()) or {}).get('services', {}) or {}


def env_of(cfg):
    e = cfg.get('environment') or {}
    return e if isinstance(e, dict) else {}


def test_every_portal_writer_follows_the_switch():
    found = set()
    for f in ('docker-compose.yml', 'docker-compose.gateway.yml'):
        for name, cfg in services(f).items():
            if any('PORTAL_DB_NAME' in str(v) for v in env_of(cfg).values()):
                found.add(name)
    missing = PORTAL_WRITERS - found
    assert not missing, (
        'these services write portal tables but do not follow PORTAL_DB_NAME, '
        'so they would keep writing to the old database after the switch:\n  %s'
        % '\n  '.join(sorted(missing)))


def test_the_gateway_is_in_the_inventory():
    """The one that is easiest to miss: a separate compose file, a different
    host, and the writer that feeds the demo's transactions."""
    assert 'iso8583-gateway' in services('docker-compose.gateway.yml'), (
        'iso8583-gateway must be accounted for; it writes atm_transactions')
    env = env_of(services('docker-compose.gateway.yml')['iso8583-gateway'])
    assert any('PORTAL_DB_NAME' in str(v) for v in env.values()), (
        'iso8583-gateway writes atm_transactions and must follow the switch, '
        'or simulator transactions land where the portal cannot see them')


def test_zabbix_itself_never_follows_the_switch():
    for f in ('docker-compose.yml', 'docker-compose.gateway.yml'):
        for name, cfg in services(f).items():
            if name in MUST_NOT_SWITCH:
                bad = [k for k, v in env_of(cfg).items() if 'PORTAL_DB_NAME' in str(v)]
                assert not bad, (
                    '%s (%s) must never be pointed at the portal database via %s - '
                    'it would lose its entire configuration' % (name, f, bad))


def test_the_default_is_the_pre_split_database():
    """Defaulting to "portal" would be the worst possible choice: a fresh clone
    with no .env would start every writer against a database that may not
    exist yet. Defaulting to "zabbix" keeps today's behaviour for anyone who
    has not chosen yet."""
    for f in ('docker-compose.yml', 'docker-compose.gateway.yml'):
        for name, cfg in services(f).items():
            for k, v in env_of(cfg).items():
                if 'PORTAL_DB_NAME' in str(v):
                    assert '${PORTAL_DB_NAME:-zabbix}' in str(v), (
                        '%s (%s) must default to zabbix, not portal' % (name, f))


def test_the_gateway_reaches_the_database_by_host_not_network():
    """iso8583-gateway runs on UAT-02 and connects across the network. If its
    DB_HOST ever became the compose service name "postgres", it would resolve
    only on its own host and the gateway would fail to start - or, worse, talk
    to the wrong database if another host had a service of that name."""
    env = env_of(services('docker-compose.gateway.yml')['iso8583-gateway'])
    assert env.get('DB_HOST') == '${APPS_VM_IP}', (
        'the gateway must reach the database by host address, not by compose '
        'service name')


def test_no_service_still_pins_the_database_name():
    """A leftover hardcoded DB_NAME: "zabbix" anywhere in the portal services
    would silently ignore PORTAL_DB_NAME entirely - the switch would appear to
    work for four services and not the fifth."""
    for f in ('docker-compose.yml', 'docker-compose.gateway.yml'):
        text = (REPO / f).read_text()
        for m in re.finditer(r'^\s+DB_NAME:\s*"([^"]+)"', text, re.M):
            value = m.group(1)
            assert 'PORTAL_DB_NAME' in value, (
                'a service still pins DB_NAME to %r and would ignore '
                'PORTAL_DB_NAME' % value)


def test_grafana_reads_the_database_the_portal_actually_uses():
    """A reader left behind is quieter than a writer left behind.

    The six writers were inventoried service by service, and every one of them
    was moved. Grafana was not in that list, because it is not a service with a
    DB_NAME - it is a datasource in a provisioning file. It queries six portal
    tables directly, and while it pointed at the old database every dashboard
    rendered frozen data with no error and no visual sign of anything wrong.
    A dashboard is how you notice that data has stopped moving, so a stale
    dashboard removes the very signal you would use to detect the problem.
    """
    uat = (REPO / 'config' / 'grafana' / 'datasources-uat.yml').read_text()
    assert 'database: portal' in uat, (
        'the UAT Grafana datasource must read the portal database; on zabbix it '
        'renders stale data silently')
    assert 'database: zabbix' not in uat, (
        'a datasource still pointing at zabbix will keep showing frozen data')

    prod = (REPO / 'config' / 'grafana' / 'datasources.yml').read_text()
    assert 'database: zabbix' in prod, (
        'the production template must stay on zabbix - production has not '
        'switched, and pointing it at a database that does not exist there '
        'would break every dashboard')


def test_the_dashboards_actually_query_portal_tables():
    """Ties the datasource change to something real: if the dashboards never
    queried portal tables, the previous fix would have been unnecessary, and if
    they start querying new ones later this test should be revisited rather than
    quietly left to rot."""
    import json
    import re
    tables = set()
    for f in (REPO / 'config' / 'grafana' / 'dashboards').glob('*.json'):
        text = f.read_text()
        for m in re.finditer(r'(?i)from\s+([a-z_]+)', text):
            name = m.group(1).lower()
            if name.startswith(('atm_', 'app_', 'audit_', 'ticket_', 'txn_',
                                'cash_', 'fleet_')):
                tables.add(name)
    assert tables, 'expected the dashboards to query portal tables'
    assert 'atm_transactions' in tables
