#!/usr/bin/env python3
"""Turn pytest's JUnit XML into GitHub Actions error annotations.

Why: workflow logs are gated even on public repositories ("Sign in to view
logs"; the logs API demands admin rights). Annotations are readable from the
check-run API, so a failing test can name itself and show its assertion without
anyone downloading a log. This is CI infrastructure, not portal code.
"""
from __future__ import annotations

import pathlib
import sys
import xml.etree.ElementTree as ET

MAX_FAILURES = 50
MAX_MESSAGE = 3500


def _data(value):
    """Escape a workflow-command message body."""
    return (value.replace('%', '%25')
                 .replace('\r', '%0D')
                 .replace('\n', '%0A'))


def _prop(value):
    """Escape a workflow-command property value.

    Commas separate properties, so they cannot appear raw. Colons are safe in
    a property value (only the ``::`` after the property list is a delimiter),
    and escaping them would make the title display as %3A.
    """
    return _data(value).replace(',', '%2C')


def _message(problem):
    text = (problem.get('message') or '').strip()
    body = (problem.text or '').strip()
    if text and body and text not in body:
        return text + '\n\n' + body
    return body or text or 'failed'


def annotations(xml_path):
    root = ET.parse(xml_path).getroot()
    lines = []

    def emit(name, path, line, problem):
        if len(lines) >= MAX_FAILURES:
            return
        properties = []
        if path:
            properties.append('file=' + _prop(path))
        if line:
            properties.append('line=' + _prop(line))
        properties.append('title=' + _prop(name))
        message = _message(problem)[:MAX_MESSAGE]
        lines.append('::error ' + ','.join(properties) + '::' + _data(message))

    for case in root.iter('testcase'):
        problem = case.find('failure')
        if problem is None:
            problem = case.find('error')
        if problem is None:
            continue
        name = '::'.join(part for part in
                         (case.get('classname'), case.get('name')) if part)
        emit(name or 'test failure', case.get('file'), case.get('line'), problem)

    # A module that fails to import produces a suite-level <error>, not a
    # testcase. Surface it too, or a collection failure stays invisible.
    for suite in root.iter('testsuite'):
        problem = suite.find('error')
        if problem is None:
            continue
        emit(suite.get('name') or 'collection error', None, None, problem)

    return lines


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    path = pathlib.Path(argv[0] if argv else 'pytest-results.xml')
    if not path.exists():
        print(f'no JUnit report at {path}; nothing to annotate', file=sys.stderr)
        return 0
    lines = annotations(path)
    for line in lines:
        print(line)
    print(f'{len(lines)} failure annotation(s)', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
