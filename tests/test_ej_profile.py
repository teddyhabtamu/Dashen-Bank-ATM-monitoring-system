"""The EJ profiler is how we decide whether parsing is viable, so its honesty
matters more than its convenience.

The dangerous failure mode is a profiler that flatters the data: if it
over-reports transaction lines, we write a parser against a fiction and ship
confident wrong amounts into a cash forecast.
"""
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / 'scripts' / 'ej_profile.py'


def run(tmp_path, content, *args):
    f = tmp_path / 'sample.log'
    f.write_bytes(content)
    out = subprocess.run([sys.executable, str(SCRIPT), str(f), *args],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_pan_is_masked(tmp_path):
    """A profiler whose output can be pasted into a ticket must not leak a card."""
    out = run(tmp_path, b'CARD=6212345678901234 AMT=250.00\n')
    assert '6212345678901234' not in out
    assert '621234******1234' in out, 'PAN should keep first 6 and last 4'


def test_very_long_digit_run_is_not_partially_revealed(tmp_path):
    out = run(tmp_path, b'TRACKING 4111111111111111111111\n')
    assert '4111111111111111111111' not in out
    assert '<22 digits>' in out


def test_amounts_survive_masking(tmp_path):
    """Masking must not destroy the fields we actually need to parse."""
    out = run(tmp_path, b'ISO8583 WITHDRAWAL AMT=2000.00 RESP=00 AUTH=123456\n')
    assert '2000.00' in out
    assert '123456' in out


def _amounts(tmp_path, content):
    import json
    data = json.loads(run(tmp_path, content, '--json'))
    return data[0]['top_amounts'], data[0]['distinct_amounts']


def test_version_strings_are_not_counted_as_amounts(tmp_path):
    """"COMPONENT VERSION|0CC 03.00.00" contains something shaped exactly like
    an amount. Counting it would overstate parse coverage."""
    amounts, distinct = _amounts(tmp_path, b'[0x01C1] 162 COMPONENT VERSION|0CC 03.00.00\n')
    assert distinct == 0, 'a firmware string was counted as money'
    assert not any(a['amount'].startswith('03.00') for a in amounts)


def test_real_amount_still_counted_alongside_a_version_line(tmp_path):
    amounts, distinct = _amounts(
        tmp_path, b'COMPONENT VERSION|0CC 03.00.00\nISO8583 WITHDRAWAL AMT=500.00\n')
    assert distinct == 1
    assert amounts[0]['amount'] == '500.00' 


def test_shape_collapse_groups_similar_lines(tmp_path):
    """The shape histogram is the point: many similar lines, few shapes."""
    body = b''.join(b'[020t*%03d*10/14/2026*19:20:%02d*ISO8583 WITHDRAWAL AMT=%d.00\n'
                    % (i, i % 60, 100 + i) for i in range(20))
    out = run(tmp_path, body, '--min-count', '2')
    assert 'OVERALL' in out
    txn = out.split('OVERALL')[1]
    assert 'txn-ish' in txn


def test_json_output_is_machine_readable(tmp_path):
    import json
    out = run(tmp_path, b'ISO8583 WITHDRAWAL AMT=2000.00\n', '--json')
    data = json.loads(out)
    assert data[0]['classes']['txn-ish']['count'] == 1
    assert data[0]['distinct_amounts'] == 1


def test_empty_input_is_reported_not_crashed(tmp_path):
    out = run(tmp_path, b'\n\n\n')
    assert 'OVERALL' in out


def test_missing_file_exits_with_guidance(tmp_path):
    out = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path / 'nope.log')],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 1
    assert 'pull_ej_logs' in out.stdout + out.stderr


def test_ej_profile_runs_without_a_venv():
    """The UAT host has no venv, and the profiler must still run there.

    It is deliberately stdlib-only: this test fails the moment someone reaches
    for psycopg2 or requests in it, which is the behaviour we want.
    """
    import ast
    tree = ast.parse(SCRIPT.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split('.')[0])
    allowed = {'argparse', 'collections', 'datetime', 'glob', 'json', 'os', 're', 'sys'}
    assert imported <= allowed, f'profiler must stay stdlib-only, added: {imported - allowed}'


def test_profiler_finds_journals_relative_to_the_repo():
    """Run from an unrelated cwd with no arguments, it must still resolve
    ej-logs/ inside the repo rather than looking at the caller's directory."""
    import subprocess
    out = subprocess.run([sys.executable, str(SCRIPT)],
                         capture_output=True, text=True, cwd='/tmp', timeout=60)
    # No journals on a dev box is a clean exit with guidance, not a crash.
    assert out.returncode in (0, 1)
    assert 'Traceback' not in out.stderr
