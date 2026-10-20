"""P00 isolated baseline: no legacy data, secrets, or external network.
Run: python -X utf8 tools/p00_baseline.py
Artifacts remain in .p00-runs/<timestamp>; no automatic deletion.
"""
from pathlib import Path
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]

def guard(root, *, network_policy=None):
    root = root.resolve()
    def inside(value):
        if isinstance(value, int) or value is None:
            return True
        try:
            return Path(os.fsdecode(value)).resolve().is_relative_to(root)
        except (TypeError, ValueError):
            return False
    def audit(event, args):
        if event == 'open':
            path, mode, flags = args
            writing = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writing and not inside(path):
                raise PermissionError('P00 write outside isolated run denied')
            if not isinstance(path, int) and '熊猫智脑cola v4' in os.fsdecode(path):
                raise PermissionError('P00 legacy runtime access denied')
        elif event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.utime'):
            if not inside(args[0]):
                raise PermissionError('P00 mutation outside isolated run denied')
        elif event in ('os.rename', 'os.link', 'os.symlink'):
            if not inside(args[0]) or not inside(args[1]):
                raise PermissionError('P00 relocation outside isolated run denied')
        elif event == 'sqlite3.connect':
            database = args[0]
            if isinstance(database, str) and database.startswith('file:'):
                from urllib.parse import urlsplit, parse_qs
                from urllib.request import url2pathname
                uri = urlsplit(database)
                if uri.netloc or parse_qs(uri.query) != {'mode': ['ro']}:
                    raise PermissionError('P00 only local read-only database URIs allowed')
                database = url2pathname(uri.path)
            if database != ':memory:' and not inside(database):
                raise PermissionError('P00 database outside isolated run denied')
        elif event in ('socket.connect', 'socket.bind'):
            address = args[1]
            if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1', 'localhost'):
                if network_policy is None or not network_policy(event, args):
                    raise PermissionError('P00 non-loopback network denied')
        elif event == 'socket.getaddrinfo':
            if args[0] not in ('127.0.0.1', '::1', 'localhost'):
                if network_policy is None or not network_policy(event, args):
                    raise PermissionError('P00 external DNS denied')
        elif event in ('subprocess.Popen', 'os.system'):
            raise PermissionError('P00 child test process launch denied')
    sys.addaudithook(audit)


def child(mode, run):
    run = Path(run).resolve()
    os.chdir(run / 'impl')
    sys.path.insert(0, str(run / 'impl'))
    sys.dont_write_bytecode = True
    guard(run)
    if mode == 'build':
        import runpy
        sys.argv = ['tools/build_demo_scenario.py']
        runpy.run_path('tools/build_demo_scenario.py', run_name='__main__')
    else:
        import unittest
        suite = unittest.defaultTestLoader.discover('tests', pattern='test*.py', top_level_dir='.')
        count = suite.countTestCases()
        started = time.monotonic()
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        summary = dict(discovered=count, tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped), expected_failures=len(result.expectedFailures), unexpected_successes=len(result.unexpectedSuccesses), successful=result.wasSuccessful(), elapsed_seconds=round(time.monotonic()-started, 3), failed_tests=[t.id() for t,_ in result.failures+result.errors], skipped_tests=[(t.id(),why) for t,why in result.skipped])
        (run / 'test-result.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        return 0 if count > 0 and result.wasSuccessful() else 1
    return 0


def main():
    run = ROOT / '.p00-runs' / datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    run.mkdir(parents=True, exist_ok=False)
    (run / 'tmp').mkdir()
    hashes = {}
    for folder in ('panda_brain', 'tests', 'config', 'scenarios', 'static', 'tools'):
        source = ROOT / 'impl' / folder
        if not source.exists():
            continue
        for item in source.rglob('*'):
            if not item.is_file() or '__pycache__' in item.parts or 'uploads' in item.parts or item.suffix == '.pyc' or item.name == 'settings.yaml':
                continue
            relative = item.relative_to(ROOT)
            target = run / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, target)
            hashes[relative.as_posix()] = hashlib.sha256(item.read_bytes()).hexdigest()
    # Old tests explicitly assert these public non-secret fixture fields.
    # This is a synthetic test fixture, never the user's runtime settings.
    fixture = "network_enabled: true\nllm_provider: openai_compatible\nllm_base_url: https://api.deepseek.com\nllm_model: deepseek-chat\nllm_timeout_seconds: 1\n"
    (run/'impl/config/settings.yaml').write_text(fixture, encoding='utf-8')
    env = os.environ.copy()
    for key in list(env):
        if any(word in key.upper() for word in ('KEY','TOKEN','SECRET','PASSWORD','CREDENTIAL')) or key.upper().endswith('_PROXY') or key in ('PYTHONPATH','PYTHONSTARTUP'):
            env.pop(key, None)
    env.update(TEMP=str(run/'tmp'), TMP=str(run/'tmp'), TMPDIR=str(run/'tmp'), PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', NO_PROXY='127.0.0.1,localhost,::1')
    metadata = {'source_workspace':str(ROOT), 'run_directory':str(run), 'python':sys.version, 'source_hashes':hashes, 'settings_fixture':'Synthetic public fields from legacy test assertions; no credential; external network blocked', 'data_policy':'Fresh demo generated in isolated copy; original data/snapshots/settings excluded; inherited credential variables removed; audit guard limits writes and network.'}
    (run/'environment.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    print('RUN_DIRECTORY='+str(run), flush=True)
    for mode in ('build','test'):
        command=[sys.executable,'-W','ignore','-X','utf8',str(Path(__file__).resolve()),'--child',mode,str(run)]
        with (run/(mode+'.log')).open('w',encoding='utf-8') as log:
            result=subprocess.run(command,env=env,cwd=run,stdout=log,stderr=subprocess.STDOUT)
        print(mode+' exit='+str(result.returncode),flush=True)
        if result.returncode:
            print((run/(mode+'.log')).read_text(encoding='utf-8')[-10000:])
            return result.returncode
    print((run/'test-result.json').read_text(encoding='utf-8'),flush=True)
    return 0

if __name__ == '__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--child':
        raise SystemExit(child(sys.argv[2],sys.argv[3]))
    raise SystemExit(main())
