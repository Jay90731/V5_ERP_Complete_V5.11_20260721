"""Offline ZIP verification; never extracts, installs or opens a business database."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile


FIXED_ROOT = r"C:\Users\auntstella\Desktop\V6_DB_WEB"
FIXED_DB = FIXED_ROOT + r"\data\orders.db"


def verify(path):
    path = Path(path)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    with zipfile.ZipFile(path) as archive:
        members = archive.namelist()
        if len(members) != len(set(members)):
            raise ValueError('Duplicate ZIP members')
        for name in members:
            parts = PurePosixPath(name).parts
            if '\\' in name or name.startswith('/') or '..' in parts or any(':' in part for part in parts):
                raise ValueError(f'Unsafe ZIP member: {name}')
        roots = {name.split('/')[0] for name in members}
        if len(roots) != 1:
            raise ValueError('Expected one preparation directory')
        prefix = roots.pop() + '/'
        files = {name[len(prefix):]: name for name in members if not name.endswith('/')}
        manifest = json.loads(archive.read(files['PACKAGE_MANIFEST.json']).decode('utf-8-sig'))
        if manifest['fixed_root'] != FIXED_ROOT or manifest['included_database'] or manifest['bhq_enabled']:
            raise ValueError('Invalid preparation manifest')
        if manifest['status'] != 'prepared_only_requires_latest_R3_and_approval':
            raise ValueError('Package is not pending preparation')
        listed = {item['path']: item for item in manifest['files']}
        if len(listed) != len(manifest['files']) or set(files) != set(listed) | {'PACKAGE_MANIFEST.json'}:
            raise ValueError('Manifest membership mismatch')
        for relative, item in listed.items():
            content = archive.read(files[relative])
            if len(content) != item['bytes'] or hashlib.sha256(content).hexdigest().upper() != item['sha256']:
                raise ValueError(f'Hash mismatch: {relative}')
        forbidden = []
        for relative in files:
            item = PurePosixPath(relative)
            lower = relative.lower()
            if (item.suffix.lower() in {'.db', '.sqlite', '.sqlite3', '.xls', '.xlsx'}
                    or lower.startswith('v6_db_web/data/')
                    or item.name.lower().startswith(('activate_bhq', 'install_bhq', 'promote_r3', 'promote_r4', 'replay_r4'))
                    or item.name.lower() == 'deployment_acceptance.json'):
                forbidden.append(relative)
        if forbidden:
            raise ValueError(f'Forbidden live data/activation files: {forbidden}')
        def read_json(relative):
            return json.loads(archive.read(files['V6_DB_WEB/' + relative]).decode('utf-8-sig'))
        phase, bhq = read_json('phase3_config.json'), read_json('bhq_watcher_formal.json')
        modes = read_json('formal_database_modes.json')
        if not (phase['database_path'] == bhq['database'] == modes['formal']['database'] == FIXED_DB):
            raise ValueError('Database paths are not unified')
        if phase['database_mode'] != 'formal' or bhq['formal_enabled'] is not False or bhq['formal_enable_confirmation']:
            raise ValueError('BHQ must stay disabled')
        if phase['python_candidates'] != [r'runtime\python\python.exe']:
            raise ValueError('Bundled Python only')
        required = {
            'CAPTURE_LATEST_R3.ps1', 'DEPLOYMENT_PLAN.md', 'READ_FIRST.txt',
            'V6_DB_WEB/runtime/python/python.exe', 'V6_DB_WEB/runtime/tesseract/tesseract.exe',
            'V6_DB_WEB/RUN_FIXED_UNIFIED.ps1', 'V6_DB_WEB/tools/r4_deployment_preflight.py',
            'V6_DB_WEB/web_v1/app.py', 'V6_DB_WEB/web_v1/wedding_web/repository.py',
            'V6_DB_WEB/src/v6_db_web/database_lock.py', 'V6_DB_WEB/src/v6_db_web/bhq_watcher.py',
        }
        if not required <= files.keys():
            raise ValueError(f'Missing required files: {sorted(required - files.keys())}')
        components = read_json('components/COMPONENT_SOURCES.json')
        for name, expected in components['files'].items():
            actual = hashlib.sha256(archive.read(files['V6_DB_WEB/components/' + name])).hexdigest().upper()
            if actual != expected:
                raise ValueError(f'ERP/OCR component metadata mismatch: {name}')
        return {
            'status': 'verified_preparation_only', 'package': str(path.resolve()),
            'sha256': digest.hexdigest().upper(), 'bytes': path.stat().st_size,
            'manifest_files_verified': len(listed), 'database_included': False,
            'formal_data_directory_included': False, 'fixed_database': FIXED_DB,
            'bhq_formal_enabled': False, 'activation_installer_included': False,
            'deployment_receipt_included': False, 'target_machine_accessed': False,
            'erp_ocr_component_hashes_match': True,
        }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = verify(args.package)
    content = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        if args.output.suffix.lower() != '.json':
            raise ValueError('Output must be JSON')
        with args.output.open('x', encoding='utf-8') as stream:
            stream.write(content + '\n')
    print(content)
