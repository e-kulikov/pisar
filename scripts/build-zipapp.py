#!/usr/bin/env python3
"""Usage: build-zipapp.py <output>

Builds the architecture-independent pisar executable: a standard-library zipapp
with a python3 shebang and the pyproject.toml version embedded. The archive is
reproducible for a given SOURCE_DATE_EPOCH (default: the last commit time).
Prints the output path.
"""
from pathlib import Path
import os
import stat
import subprocess
import sys
import time
import tomllib
import zipfile

REPO = Path(__file__).resolve().parents[1]
SHEBANG = b'#!/usr/bin/env python3\n'
# Kept parseable by old interpreters so they report the requirement clearly.
MAIN = '''import sys
if sys.version_info < (3, 11):
    sys.stderr.write('pisar requires Python >= 3.11\\n')
    raise SystemExit(1)
from pisar.cli import main
raise SystemExit(main())
'''


def source_date_epoch():
    value = os.environ.get('SOURCE_DATE_EPOCH')
    if not value:
        value = subprocess.run(['git', '-C', str(REPO), 'log', '-1', '--format=%ct'],
                               capture_output=True, text=True).stdout.strip()
    # ZIP timestamps start in 1980 and have two-second resolution.
    return max(int(value or 0), 315532800)


def entries(version):
    files = {'__main__.py': MAIN.encode(),
             'pisar/_version.py': f'VERSION = {version!r}\n'.encode()}
    for path in sorted((REPO / 'pisar').glob('*.py')):
        if path.name == '_version.py':
            continue
        files[f'pisar/{path.name}'] = path.read_bytes()
    skill = REPO / 'pisar' / 'skill.md'
    files['pisar/skill.md'] = skill.read_bytes()
    return dict(sorted(files.items()))


def build(output):
    project = tomllib.loads((REPO / 'pyproject.toml').read_text(encoding='utf-8'))['project']
    stamp = time.gmtime(source_date_epoch())[:6]
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f'.{output.name}.tmp')
    with temporary.open('wb') as stream:
        stream.write(SHEBANG)
        with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries(project['version']).items():
                info = zipfile.ZipInfo(name, stamp)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(info, data, compresslevel=9)
    temporary.chmod(0o755)
    temporary.replace(output)
    return output


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit(__doc__.strip().splitlines()[0])
    print(build(sys.argv[1]))
