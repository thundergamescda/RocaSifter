"""Source-aware isolated PostgreSQL acceptance; never migrates the live schema.

DATABASE_URL must be supplied privately by the process launcher. Every allowed
module creates a fresh ss_test_<uuid> fixture namespace. Skips and source drift
fail the run; partial module selections are labelled diagnostic evidence.
"""
import argparse
from collections import Counter
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest
from urllib.parse import unquote, urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
ALLOWED=('hosted.integration','test_hosted_settlement_postgres',
         'test_hosted_reconciliation_postgres','test_hosted_trades_postgres',
         'test_hosted_mfa_postgres','test_hosted_privacy_postgres')


def fingerprints():
    paths=[p for p in (ROOT/'hosted').rglob('*') if p.is_file() and p.suffix in ('.py','.sql','.js','.html','.css')]
    paths.extend(p for p in ROOT.glob('test_hosted*.py') if p.is_file())
    paths.extend(ROOT/name for name in ('requirements-hosted.txt','render.yaml','tools/test_hosted_postgres.py'))
    return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(paths)) if p.exists()}


def scrub(value):
    url=os.environ.get('DATABASE_URL','')
    if url:
        value=value.replace(url,'[redacted database connection]')
        try:
            password=urlsplit(url).password
            if password:
                value=value.replace(password,'[redacted]').replace(unquote(password),'[redacted]')
        except ValueError:
            pass
    return value


def test_ids(suite):
    for item in suite:
        if isinstance(item,unittest.TestSuite):
            yield from test_ids(item)
        else:
            yield item.id()


class EvidenceResult(unittest.TextTestResult):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.started_ids=[]
        self.results=[]
        self.times={}

    def startTest(self,test):
        self.started_ids.append(test.id());self.times[test.id()]=time.monotonic()
        super().startTest(test)

    def record(self,test,status,detail=None):
        elapsed=time.monotonic()-self.times.get(test.id(),time.monotonic())
        self.results.append({'id':test.id(),'status':status,'elapsed_seconds':round(elapsed,6),
                             **({'detail':scrub(detail)} if detail else {})})

    def addSuccess(self,test):
        self.record(test,'passed');super().addSuccess(test)

    def addFailure(self,test,error):
        self.record(test,'failed',self._exc_info_to_string(error,test));super().addFailure(test,error)

    def addError(self,test,error):
        self.record(test,'error',self._exc_info_to_string(error,test));super().addError(test,error)

    def addSkip(self,test,reason):
        self.record(test,'skipped',reason);super().addSkip(test,reason)

    def addExpectedFailure(self,test,error):
        self.record(test,'expected_failure',self._exc_info_to_string(error,test));super().addExpectedFailure(test,error)

    def addUnexpectedSuccess(self,test):
        self.record(test,'unexpected_success');super().addUnexpectedSuccess(test)

    def addSubTest(self,test,subtest,error):
        if error is not None:
            self.record(subtest,'failed_subtest',self._exc_info_to_string(error,test))
        super().addSubTest(test,subtest,error)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--modules',nargs='+',choices=ALLOWED,default=list(ALLOWED))
    args=parser.parse_args()
    if len(args.modules)!=len(set(args.modules)):
        parser.error('Select each acceptance module once.')
    started=datetime.now(timezone.utc)
    output=ROOT/'Testing'/'next-security'/'postgres-runs'/started.strftime('%Y%m%dT%H%M%S%fZ')
    output.mkdir(parents=True)
    initial=fingerprints()
    buffer=io.StringIO()
    expected=[];result=None;load_error=None
    configured=bool(os.environ.get('DATABASE_URL'))
    with redirect_stdout(buffer),redirect_stderr(buffer):
        if not configured:
            print('DATABASE_URL must be supplied privately by the fixture launcher. No tests ran.')
        else:
            try:
                suite=unittest.defaultTestLoader.loadTestsFromNames(args.modules)
                expected=list(test_ids(suite))
                result=unittest.TextTestRunner(stream=buffer,verbosity=2,resultclass=EvidenceResult).run(suite)
            except Exception as error:
                # Exception class only here; unittest captures bounded case diagnostics.
                load_error=type(error).__name__
                print('Acceptance runner failed with type='+load_error)
    final=fingerprints()
    changed=[name for name in sorted(set(initial)|set(final)) if initial.get(name)!=final.get(name)]
    log=scrub(buffer.getvalue())
    skipped=len(result.skipped) if result else 0
    failures=len(result.failures) if result else 0
    errors=len(result.errors) if result else 0
    unusual=bool(result and (result.expectedFailures or result.unexpectedSuccesses))
    started_ids=result.started_ids if result else []
    passed_ids=[case['id'] for case in result.results if case['status']=='passed'] if result else []
    coverage_complete=bool(expected and result and result.testsRun==len(expected)
                           and Counter(started_ids)==Counter(expected)
                           and Counter(passed_ids)==Counter(expected))
    passed=bool(configured and result and result.wasSuccessful() and coverage_complete
                and not skipped and not unusual and not changed and not load_error)
    exit_code=0 if passed else (2 if not configured else 1)
    summary={'started_utc':started.isoformat(),'finished_utc':datetime.now(timezone.utc).isoformat(),
             'scope':'Actual PostgreSQL tests using fresh ss_test_<uuid> namespaces; no live-schema migration, merchant data, Square, email or hosted deployment.',
             'modules':args.modules,'complete_module_selection':set(args.modules)==set(ALLOWED),
             'run_kind':'complete' if set(args.modules)==set(ALLOWED) else 'diagnostic',
             'database_configured':configured,'python':sys.version.split()[0],
             'expected_test_ids':expected,'expected_count':len(expected),
             'tests_run':result.testsRun if result else 0,
             'started_test_ids':started_ids,'successful_test_ids':passed_ids,'expected_case_coverage_complete':coverage_complete,
             'case_results':result.results if result else [],'skipped_count':skipped,
             'failed_count':failures,'error_count':errors,'runner_error_type':load_error,
             'source_sha256_at_start':initial,'source_sha256':final,
             'source_snapshot_stable':not changed,'changed_during_run':changed,
             'live_square_contacted':False,'production_migration_run':False,
             'log_sha256':hashlib.sha256(log.encode()).hexdigest(),'passed':passed,'exit_code':exit_code}
    (output/'postgres-tests.log').write_text(log,encoding='utf-8')
    (output/'postgres-summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    latest=ROOT/'Testing'/'next-security'/'postgres-latest.json'
    latest.write_text(json.dumps(summary|{'run_directory':str(output)},indent=2),encoding='utf-8')
    print(log[-4500:])
    print(f'PostgreSQL acceptance: {summary["tests_run"]} run, {skipped} skipped, {failures} failed, {errors} errors; stable={not changed}; passed={passed}')
    print('Evidence directory:',output)
    return exit_code


if __name__=='__main__':
    sys.exit(main())
