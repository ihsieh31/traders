#!/usr/bin/env python3
"""Offline pytest runner. Artifacts and all default mutable state live outside repo."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', default=str(ROOT / '.venv-p2/bin/python'))
    parser.add_argument('--artifacts', help='New or empty directory outside the repository')
    options, pytest_args = parser.parse_known_args()
    if pytest_args[:1] == ['--']:
        pytest_args.pop(0)
    if any(a in ('-x', '--exitfirst', '--disable-warnings') or a.startswith('--maxfail') for a in pytest_args):
        parser.error('Early exit and warning suppression are not allowed')
    artifacts = Path(options.artifacts or tempfile.mkdtemp(prefix='tradingalpaca-refactor-')).resolve()
    if artifacts == ROOT or ROOT in artifacts.parents:
        parser.error('Artifacts must be outside the repository')
    artifacts.mkdir(parents=True, exist_ok=True)
    if list(artifacts.iterdir()):
        parser.error('Artifacts directory must be empty')
    dirs = {name: artifacts / name for name in ('home', 'tmp', 'state', 'locks', 'results', 'cache')}
    for path in dirs.values():
        path.mkdir()
    env = {
        'PATH': str(Path(options.python).absolute().parent) + ':/usr/bin:/bin:/usr/sbin:/sbin',
        'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8',
        'HOME': str(dirs['home']), 'TMPDIR': str(dirs['tmp']),
        'XDG_CONFIG_HOME': str(dirs['home'] / 'config'),
        'XDG_DATA_HOME': str(dirs['state']), 'XDG_CACHE_HOME': str(dirs['cache']),
        'PYTHON_DOTENV_DISABLED': '1', 'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONHASHSEED': '0', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',
        'PYTHONPATH': os.pathsep.join((str(Path(__file__).resolve().parent), str(ROOT))),
        'REFACTOR_OFFLINE': '1', 'REFACTOR_ARTIFACTS': str(artifacts),
        'TRADINGBUFFETT_LONG_RUN_DIR': str(dirs['state'] / 'long_run'),
        'TRADINGBUFFETT_EXECUTION_DB': str(dirs['state'] / 'execution.sqlite3'),
        'TRADINGBUFFETT_EXECUTION_LOCK_DIR': str(dirs['locks']),
        'TRADINGBUFFETT_RESULTS_DIR': str(dirs['results']),
        'TRADINGBUFFETT_CACHE_DIR': str(dirs['cache']),
        'TRADINGBUFFETT_MEMORY_LOG_PATH': str(dirs['state'] / 'memory.md'),
        'TRADINGBUFFETT_AGENT_MEMORY_DIR': str(dirs['state'] / 'agent_memory'),
        'PIP_NO_INDEX': '1', 'PIP_DISABLE_PIP_VERSION_CHECK': '1',
        'PIP_CONFIG_FILE': os.devnull,
    }
    command = [options.python, '-m', 'pytest', '-p', 'refactor_pytest_plugin',
               '-o', 'addopts=', '-o', 'cache_dir=' + str(dirs['cache'] / 'pytest'),
               '--basetemp=' + str(dirs['tmp'] / 'pytest'),
               '--junitxml=' + str(artifacts / 'junit.xml'), *(pytest_args or ['tests'])]
    metadata = {'command': command, 'cwd': str(ROOT), 'environment': env,
                'started_at': time.time(), 'returncode': None}
    (artifacts / 'run.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print('Artifacts: ' + str(artifacts), flush=True)
    with (artifacts / 'stdout.log').open('wb') as out, (artifacts / 'stderr.log').open('wb') as err:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=out, stderr=err)
    metadata.update(returncode=result.returncode, finished_at=time.time())
    (artifacts / 'returncode.txt').write_text(str(result.returncode) + '\n')
    (artifacts / 'run.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print('Actual pytest returncode: ' + str(result.returncode), flush=True)
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
