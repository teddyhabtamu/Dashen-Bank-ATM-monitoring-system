"""The CI annotation reporter must not itself hide a failure.

A malformed title or an unescaped newline makes GitHub drop the annotation, and
then a red run has no explanation again. Pin the escaping and both shapes of
JUnit failure (test assertion and module import error).
"""
from __future__ import annotations

import importlib.util
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    'ci_annotate_junit', REPO / 'tests' / 'ci_annotate_junit.py')
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

SAMPLE = '''<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" errors="0" failures="1" tests="2">
    <testcase classname="tests.test_demo" name="test_breaks" file="tests/test_demo.py" line="42">
      <failure message="assert 1 == 2">E   assert 1 == 2
E    +  where 1 = value()</failure>
    </testcase>
    <testcase classname="tests.test_demo" name="test_ok" file="tests/test_demo.py" line="50"/>
  </testsuite>
</testsuites>
'''

COLLECTION = '''<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="tests.test_broken" errors="1" failures="0" tests="0">
    <error message="ModuleNotFoundError: No module named 'flask'">Traceback...</error>
  </testsuite>
</testsuites>
'''


def _write(tmp_path, text):
    path = tmp_path / 'report.xml'
    path.write_text(text, encoding='utf-8')
    return path


def test_a_failure_annotation_names_the_test_file_and_line(tmp_path):
    lines = module.annotations(_write(tmp_path, SAMPLE))
    assert len(lines) == 1
    assert lines[0].startswith('::error file=tests/test_demo.py,line=42,')
    assert 'tests.test_demo::test_breaks' in lines[0]
    assert 'assert 1 == 2' in lines[0]


def test_newlines_and_percent_are_escaped(tmp_path):
    lines = module.annotations(_write(tmp_path, SAMPLE))
    assert '\n' not in lines[0]
    assert '%0A' in lines[0]
    assert 'E    +  where' in lines[0]


def test_a_collection_error_is_annotated_too(tmp_path):
    lines = module.annotations(_write(tmp_path, COLLECTION))
    assert len(lines) == 1
    assert 'No module named' in lines[0]
    assert 'tests.test_broken' in lines[0]


def test_a_missing_report_is_not_an_error():
    assert module.main(['/nonexistent/pytest-results.xml']) == 0
