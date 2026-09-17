"""Record the actual collection without a second potentially different collection."""
import json
import os
from pathlib import Path


def pytest_sessionstart(session):
    if os.environ.get('REFACTOR_GUARD_ACTIVE') != '1':
        raise RuntimeError('Offline sitecustomize guard was not loaded')


def pytest_collection_finish(session):
    path = Path(os.environ['REFACTOR_ARTIFACTS']) / 'collected-nodeids.json'
    path.write_text(json.dumps([item.nodeid for item in session.items], indent=2) + '\n')
