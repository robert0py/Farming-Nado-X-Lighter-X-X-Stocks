"""Reject private runtime data in tracked files before building a release.

This check never reads the user's vault or account configuration. It checks the
Git distribution manifest; user state must stay outside the source distribution.
"""

from pathlib import PurePosixPath
import subprocess


PRIVATE_DIRECTORIES = {'state', 'reports', 'outputs', '.venv', '__pycache__'}
PRIVATE_SUFFIXES = {'.bin', '.key', '.pem', '.pfx', '.p12', '.db', '.sqlite', '.sqlite3', '.log', '.csv', '.zip'}


def private_paths(paths):
    rejected = []
    for name in paths:
        path = PurePosixPath(name.lower())
        if (PRIVATE_DIRECTORIES.intersection(path.parts) or
                path.suffix in PRIVATE_SUFFIXES or
                path.name in {'accounts.json', 'credentials.json', 'secrets.json'} or
                path.name.startswith(('vault-', '.env')) and path.name != '.env.example' or
                '.sqlite' in path.name or path.name.endswith(('.db-wal', '.db-shm'))):
            rejected.append(name)
    return rejected


if __name__ == '__main__':
    paths = subprocess.check_output(['git', 'ls-files', '-z']).decode('utf-8').split('\0')
    rejected = private_paths(filter(None, paths))
    if rejected:
        raise SystemExit('Private runtime files must not be published:\n' + '\n'.join(rejected))
    print('Publication manifest checked: no private runtime files tracked.')
