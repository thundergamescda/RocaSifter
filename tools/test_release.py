"""Strict source-aware evidence for isolated unit/loopback tests; no live Square.

Tests run in a fresh child. A zero exit alone is insufficient: exact nonempty
case coverage, no skips/failures/errors and stable source are required.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import subprocess
import sys
import unittest

root=Path(__file__).resolve().parents[1]
modules=['test_station_paths','test_station_runtime','test_station','test_roca_to_square',
         'test_roca_update','test_pricing_review','test_credit_cleanup_oauth','test_price_variations',
         'test_hosted_boundaries','test_hosted_auth_abuse','test_hosted_square_status',
         'test_station_cloud','test_scanner_adapters','test_intake_integration','test_pricing_metadata',
         'test_hosted_settlement','test_hosted_credit_lots','test_hosted_reconciliation',
         'test_cloud_http_boundary','test_hosted_provider_ids','test_hosted_trades','test_import_preview','test_package_preview',
         'test_shared_trade','test_receipt_summary_cache','test_hosted_mfa','test_station_mfa','test_hosted_privacy','tools.test_pg_restore_drill_guards']
def fingerprints():
    paths=[p for p in root.iterdir() if p.is_file() and p.suffix in ('.py','.js','.css','.html','.cmd','.vbs')]
    paths.extend(p for p in (root/'hosted').rglob('*') if p.is_file() and p.suffix in ('.py','.js','.css','.html','.sql'))
    paths.extend(p for p in (root/'tools').iterdir() if p.is_file() and p.suffix in ('.py','.ps1','.js'))
    paths.extend(root/name for name in ('requirements.txt','requirements-dev.txt','requirements-build.txt','requirements-hosted.txt',
                                      'render.yaml','roca_edition.json','product_brand.json','assets/stock-and-sleeve.ico','assets/stock-and-sleeve.png'))
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(paths)) if p.is_file()}


def evidence_helper():
    spec=importlib.util.spec_from_file_location('_ss_acceptance_evidence',root/'tools'/'test_hosted_postgres.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def complete_coverage(evidence):
    expected=evidence.get('expected_test_ids',[])
    started=evidence.get('started_test_ids',[])
    passed=evidence.get('successful_test_ids',[])
    if (any(not isinstance(value,list) or any(not isinstance(item,str) for item in value)
            for value in (expected,started,passed)) or type(evidence.get('tests_run')) is not int
            or type(evidence.get('expected_count')) is not int):return False
    duplicates=[key for key,count in Counter(expected).items() if count!=1]
    return bool(expected and not duplicates and evidence.get('tests_run')==len(expected)
                and evidence.get('expected_count')==len(expected)
                and Counter(started)==Counter(expected) and Counter(passed)==Counter(expected))


def child(output):
    sys.path.insert(0,str(root))
    helper=evidence_helper()
    suite=unittest.defaultTestLoader.loadTestsFromNames(modules)
    expected=list(helper.test_ids(suite))
    result=unittest.TextTestRunner(stream=sys.stdout,verbosity=2,resultclass=helper.EvidenceResult).run(suite)
    evidence={'modules':modules,'expected_test_ids':expected,'expected_count':len(expected),
              'tests_run':result.testsRun,'started_test_ids':result.started_ids,
              'successful_test_ids':[case['id'] for case in result.results if case['status']=='passed'],
              'case_results':result.results,'skipped_count':len(result.skipped),
              'failed_count':len(result.failures),'error_count':len(result.errors),
              'expected_failure_count':len(result.expectedFailures),'unexpected_success_count':len(result.unexpectedSuccesses)}
    evidence['expected_case_coverage_complete']=complete_coverage(evidence)
    evidence['passed']=bool(result.wasSuccessful() and evidence['expected_case_coverage_complete']
                            and not any(evidence[key] for key in ('skipped_count','failed_count','error_count',
                                                                  'expected_failure_count','unexpected_success_count')))
    output.write_text(json.dumps(evidence,indent=2),encoding='utf-8')
    return 0 if evidence['passed'] else 1


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-child',type=Path,help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.evidence_child:
        destination=args.evidence_child.resolve()
        if not destination.is_relative_to((root/'Testing'/'next-security').resolve()):
            parser.error('Child evidence must remain inside this snapshot Testing/next-security folder.')
        return child(destination)
    report=root/'Testing'/'next-security';report.mkdir(parents=True,exist_ok=True)
    started=datetime.now(timezone.utc)
    run=report/'unit-runs'/started.strftime('%Y%m%dT%H%M%S%fZ');run.mkdir(parents=True)
    source_start=fingerprints()
    result=subprocess.run([sys.executable,'-X','utf8',str(Path(__file__).resolve()),'--evidence-child',str(run/'case-evidence.json')],
                          cwd=root,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,encoding='utf-8')
    helper=evidence_helper()
    log=helper.scrub(result.stdout)
    source_end=fingerprints()
    changed=[name for name in sorted(set(source_start)|set(source_end)) if source_start.get(name)!=source_end.get(name)]
    evidence={};evidence_error=None
    try:evidence=json.loads((run/'case-evidence.json').read_text(encoding='utf-8'))
    except (ValueError,OSError) as error:evidence_error=type(error).__name__
    covered=complete_coverage(evidence)
    no_outcomes=all(evidence.get(key)==0 for key in ('skipped_count','failed_count','error_count',
                                                   'expected_failure_count','unexpected_success_count'))
    passed=bool(result.returncode==0 and evidence.get('modules')==modules and evidence.get('passed') is True
                and covered and no_outcomes and not changed and not evidence_error)
    exit_code=0 if passed else (2 if changed else 1)
    summary=evidence|{'timestamp_utc':datetime.now(timezone.utc).isoformat(),'started_utc':started.isoformat(),
         'exit_code':exit_code,'child_exit_code':result.returncode,'passed':passed,
         'expected_case_coverage_complete':covered,'case_evidence_error_type':evidence_error,
         'python':platform.python_version(),'platform':platform.platform(),'modules':modules,
         'live_square_contacted':False,'scope':'Isolated unit and loopback integration tests with fictional Square responses. Actual PostgreSQL/browser/provider/hardware acceptance is separate.',
         'source_sha256':source_end,'source_sha256_at_start':source_start,
         'source_snapshot_stable':not changed,'changed_during_run':changed,
         'log_sha256':hashlib.sha256(log.encode()).hexdigest(),'run_directory':str(run)}
    (run/'unit-tests.log').write_text(log,encoding='utf-8')
    (run/'unit-summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    (report/'unit-latest.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(log[-4500:])
    print(f'Strict unit acceptance: {evidence.get("tests_run",0)} run; exact_coverage={covered}; skips={evidence.get("skipped_count","unknown")}; stable={not changed}; passed={passed}')
    print('Evidence directory:',run)
    return exit_code


if __name__=='__main__':
    sys.exit(main())
