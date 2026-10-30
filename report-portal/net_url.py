"""Internal service URL construction.

UAT serves Zabbix, OpenSearch and the simulator over plain HTTP on the
private network.  A deployment that terminates TLS in front of those
services can set ES_SCHEME / ZBX_SCHEME / SIM_SCHEME to https without a
code change.
"""
import os


def service_url(host, path='', scheme_env='SERVICE_SCHEME'):
    """Return host+path with a configurable scheme.

    ``host`` may already carry a scheme (for example an operator-supplied
    ES_HOST); in that case it is used unchanged.
    """
    host = str(host or '')
    if '://' in host:
        return f'{host}{path}'
    scheme = os.environ.get(scheme_env, 'http')
    return f'{scheme}://{host}{path}'
