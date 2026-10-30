"""Shared test helpers.

Kept in conftest rather than duplicated per module: the chart-query extractor
is needed by both the timezone tests and the fleet-consistency tests, and two
copies of a parser is two chances to disagree.
"""
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent


def chart_sql(name):
    """Return exactly one named chart query from the dashboard endpoint.

    Slicing to the next query assignment swallows the comment block that
    follows, so an assertion can fail on a comment instead of on SQL. This
    slices between the first pair of triple-quote delimiters only.
    """
    routes = (REPO / 'report-portal' / 'routes.py').read_text()
    opener = routes.index('%s = %s' % (name, '"""')) + len('%s = ' % name) + 3
    closer = routes.index('"""', opener)
    return routes[opener:closer]
