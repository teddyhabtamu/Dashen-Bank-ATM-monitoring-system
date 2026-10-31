"""The fleet sweep must not scale its network calls with the fleet.

Measured motivation: verifying the fleet ran verify_atm() once per ATM, and each
call built a fresh Zabbix client, logged in, then made host.get + item.get +
trigger.get - plus a fourth host.get that re-selected interfaces the first call
had already returned. At 1,300 ATMs that is ~3,900 serial HTTP round-trips and
1,300 logins, inside a daemon thread in a gunicorn worker with --timeout 120.

Zabbix's API is a batch API: item.get and trigger.get take an ARRAY of hostids.
So the fleet snapshot is fetched in chunks and sliced per ATM. These tests pin
both halves: the call count collapses, and the verdicts do not change.
"""
import pathlib
import sys
import types

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'report-portal'))

import verify  # noqa: E402


class CountingClient:
    """Records every Zabbix call so the test can count them."""

    def __init__(self, hosts):
        self.calls = []
        self.hosts = hosts
        self.last_error = None
        self.logins = 0

    def login(self):
        self.logins += 1

    def call(self, method, params):
        self.calls.append((method, params))
        if method == 'host.get':
            wants_ifaces = bool(params.get('selectInterfaces'))
            by_id = {h['hostid']: h for h in self.hosts}
            wanted_ids = params.get('hostids')
            if wanted_ids is not None:
                return [{'hostid': hid,
                         'interfaces': (by_id[hid]['interfaces'] if wants_ifaces and hid in by_id else [])}
                        for hid in wanted_ids]
            wanted = (params.get('filter') or {}).get('host') or []
            return [h for h in self.hosts if h['host'] in wanted]
        if method == 'item.get':
            out = []
            for hid in params['hostids']:
                out.append({'itemid': 'i%s' % hid, 'hostid': hid, 'key_': 'atm.parts',
                            'lastclock': '2000000000', 'state': '0', 'type': '0'})
                out.append({'itemid': 'z%s' % hid, 'hostid': hid,
                            'key_': 'zabbix[host,agent,available]',
                            'lastclock': '2000000000', 'state': '0', 'type': '0'})
            return out
        if method == 'trigger.get':
            return [{'triggerid': 't1', 'hostid': params['hostids'][0],
                     'description': 'Disk nearly full', 'priority': '2'}]
        return []


def _hosts(n):
    """Model the REAL host shape.

    zabbix_sync sets host=<atm_id> and name=<atm_name>, and those differ on
    every real machine: host 'ATM-TEST-NCR', name '4 Kilo Test NCR'. The first
    version of this fake set name == atm_id, which is why a lookup keyed on the
    wrong field passed here and then reported the entire fleet as not
    provisioned in production.
    """
    return [{'hostid': str(i),
             'host': 'ATM-%04d' % i,
             'name': 'Branch %d Test ATM' % i,
             'status': '0',
             'interfaces': [{'interfaceid': 'i', 'type': '1', 'port': '10050',
                             'ip': '10.0.0.%d' % (i % 250 + 1)}]}
            for i in range(1, n + 1)]


def _ids(n):
    return ['ATM-%04d' % i for i in range(1, n + 1)]


# ── the call count ─────────────────────────────────────────────────────────

def test_one_atm_costs_the_same_as_a_thousand():
    """The whole point. Per-ATM cost must not grow with the fleet."""
    small = CountingClient(_hosts(1))
    verify._zabbix_snapshot(small, _ids(1))
    big = CountingClient(_hosts(1000))
    verify._zabbix_snapshot(big, _ids(1000))

    # Chunked, so it grows only with the number of chunks - not with the fleet.
    chunks = 1 + (1000 - 1) // verify.ZABBIX_CHUNK
    assert len(big.calls) == 3 * chunks, (
        'expected host+item+trigger per chunk, got %d calls for %d chunks'
        % (len(big.calls), chunks))
    assert len(big.calls) < 40, 'expected a couple of dozen calls, got %d' % len(big.calls)


def test_logins_drop_to_one_for_the_whole_fleet():
    """The old path built a _Client and logged in per ATM."""
    src = (REPO / 'report-portal' / 'verify.py').read_text()
    fleet_body = src[src.index('def fleet('):]
    assert fleet_body.count('client.login()') == 1, (
        'fleet() must log in exactly once for the whole sweep')


def test_snapshot_chunks_rather_than_asking_for_the_whole_fleet_at_once():
    """1,300 hosts x ~90 items is tens of megabytes in one response."""
    assert 0 < verify.ZABBIX_CHUNK <= 500, (
        'chunk size must stay bounded or the response will not fit in a worker')
    client = CountingClient(_hosts(600))
    verify._zabbix_snapshot(client, _ids(600))
    sizes = [len(p.get('hostids') or p.get('filter', {}).get('host') or [])
             for _, p in client.calls]
    assert max(sizes) <= verify.ZABBIX_CHUNK, 'a chunk exceeded the cap: %s' % sizes


def test_no_per_host_filter_calls_survive():
    """A single-host filter is the N+1 signature we are removing."""
    client = CountingClient(_hosts(50))
    verify._zabbix_snapshot(client, _ids(50))
    for method, params in client.calls:
        if method == 'item.get':
            assert len(params['hostids']) > 1, 'item.get was asked about one host'
        if method == 'trigger.get':
            assert len(params['hostids']) > 1, 'trigger.get was asked about one host'


# ── the verdicts must not change ───────────────────────────────────────────

def test_snapshot_reproduces_the_per_atm_facts():
    """Same numbers, same clocks, same problems, same interface detection."""
    hosts = _hosts(3)
    snap = verify._zabbix_snapshot(CountingClient(hosts), _ids(3))
    per_atm = verify._zabbix_facts('ATM-0001', CountingClient(hosts))

    batched = snap['ATM-0001']
    for field in ('items_total', 'items_reporting', 'items_unsupported',
                  'items_internal', 'keys_reporting', 'agent_interface',
                  'interfaces', 'problems', 'status_enabled'):
        assert batched[field] == per_atm[field], (
            '%s differs: batched=%r per-atm=%r' % (field, batched[field], per_atm[field]))
    assert batched['key_clocks'] == per_atm['key_clocks']
    assert batched['host']['hostid'] == per_atm['host']['hostid']


def test_internal_items_never_count_as_reporting():
    """zabbix[...] items report on Zabbix, not on the ATM. Keyed by KEY, not
    type - internal items are type 0, as is every passive agent item including
    atm.parts, and keying off type once discarded the one item that matters."""
    snap = verify._zabbix_snapshot(CountingClient(_hosts(1)), _ids(1))
    f = snap['ATM-0001']
    assert f['items_internal'] == 1
    assert f['items_reporting'] == 1, 'the internal item must not count as reporting'
    assert 'atm.parts' in f['key_clocks']


def test_atm_missing_from_zabbix_is_not_provisioned_not_unknown():
    """Asked and found nothing is different from could not ask."""
    snap = verify._zabbix_snapshot(CountingClient(_hosts(1)), _ids(3))
    absent = snap['ATM-0003']
    assert absent['host'] is None
    assert absent['error'] == '', 'a missing host is not an API error'
    assert snap['ATM-0001']['host'] is not None


def test_api_failure_marks_every_atm_rather_than_inventing_verdicts():
    """The failure direction matters most: 'could not ask' must never become
    'healthy' or 'unreachable'."""
    class Broken(CountingClient):
        def call(self, method, params):
            self.calls.append((method, params))
            self.last_error = 'connection refused'
            return None

    snap = verify._zabbix_snapshot(Broken(_hosts(3)), _ids(3))
    for f in snap.values():
        assert f['error'], 'an API failure must be recorded, not swallowed'
        assert f['host'] is None


def test_every_requested_atm_is_present_in_the_snapshot():
    """An ATM dropped from the map would silently fall back to the per-ATM path
    mid-sweep - slow again, with no error."""
    snap = verify._zabbix_snapshot(CountingClient(_hosts(3)), _ids(5))
    assert set(snap) == set(_ids(5))
    assert snap['ATM-0004']['host'] is None and snap['ATM-0005']['host'] is None


def test_db_snapshot_returns_a_row_for_every_requested_atm():
    """Same contract as the Zabbix snapshot: no key missing, so no silent
    fallback to a per-ATM query."""
    fake = types.SimpleNamespace(cursor=lambda: types.SimpleNamespace(
        execute=lambda *a, **k: None,
        fetchall=lambda: [],
        close=lambda: None))
    out = verify._db_snapshot(['ATM-0001'])
    assert set(out) == {'ATM-0001'}
    assert set(out['ATM-0001']) == {
        'row', 'state', 'last_seen', 'db_error',
        'identity', 'ej', 'image_roots'}


def test_fleet_falls_back_rather_than_failing_the_page():
    """If batching fails the sweep must still produce a report."""
    src = (REPO / 'report-portal' / 'verify.py').read_text()
    body = src[src.index('def fleet('):]
    assert 'falling back per ATM' in body
    assert 'results = None' in body, 'must be able to abandon the batched path'
    assert body.count('results = [verify_fn(') == 1, 'exactly one fallback path'

# ── the regression that shipped in eb2fee6 ─────────────────────────────────

def test_snapshot_matches_on_the_technical_host_name_not_the_visible_name():
    """Shipped bug: the batched host.get matched on Zabbix's 'name' (the ATM's
    display name, e.g. '4 Kilo Test NCR') and reported every real ATM as not
    provisioned. The provisioner sets host=<atm_id> and name=<atm_name>."""
    hosts = _hosts(2)
    snap = verify._zabbix_snapshot(CountingClient(hosts), _ids(2))
    assert snap['ATM-0001']['host'] is not None, (
        'host.get must be matched on the technical name')
    assert snap['ATM-0002']['host'] is not None


def test_visible_name_never_counts_as_an_atm_id():
    """If a visible name were used as a key, a host called 'Branch 1 Test ATM'
    would be reported against an ATM that does not exist."""
    hosts = _hosts(2)
    snap = verify._zabbix_snapshot(CountingClient(hosts), _ids(2))
    assert 'Branch 1 Test ATM' not in snap
    assert set(snap) == set(_ids(2)), 'the map is keyed by atm_id and nothing else'


def test_batch_requests_the_technical_name_field():
    """Asking for 'name' alone is what broke it; the field must be requested and
    matched explicitly."""
    client = CountingClient(_hosts(1))
    verify._zabbix_snapshot(client, _ids(1))
    host_get = next(p for m, p in client.calls if m == 'host.get')
    assert 'host' in host_get['output'], (
        "output must include the technical name 'host'")


def test_every_host_lookup_in_the_snapshot_uses_the_technical_name():
    """I fixed the first occurrence of `out.get(h.get('name'))` and missed the
    second, so hosts were assigned but their items never attached - the report
    said "not provisioned" for some machines and "0 items" for the rest. A
    partial fix of a repeated pattern needs a test that counts occurrences
    rather than trusting a search-and-replace to have hit all of them."""
    import ast
    src = (REPO / 'report-portal' / 'verify.py').read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == '_zabbix_snapshot')
    body = ast.get_source_segment(src, fn)
    bare = body.count("out.get(h.get('name'))")
    correct = body.count("out.get(h.get('host') or h.get('name'))")
    assert bare == 0, (
        'found %d lookup(s) keyed on the visible name only' % bare)
    assert correct == 2, (
        'expected both the host assignment and the item loop to use the '
        'technical name; found %d' % correct)
