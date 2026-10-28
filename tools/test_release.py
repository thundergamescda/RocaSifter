"""Save reproducible, redacted logs for isolated tests. No live Square calls."""
from pathlib import Path
import subprocess
import sys
import datetime
import json
import hashlib
import platform
import re
sys.stdout.reconfigure(encoding='utf-8')

root=Path(__file__).resolve().parents[1]
report=root/'Testing'/'2.6.0';report.mkdir(parents=True,exist_ok=True)
modules=['test_station_paths','test_station_runtime','test_station','test_roca_to_square',
         'test_roca_update','test_pricing_review','test_credit_cleanup_oauth','test_price_variations',
         'test_hosted_boundaries','test_hosted_auth_abuse','test_hosted_square_status',
         'test_station_cloud','test_scanner_adapters','test_intake_integration','test_pricing_metadata',
         'test_hosted_settlement','test_hosted_credit_lots','test_hosted_reconciliation',
         'test_cloud_http_boundary','test_hosted_provider_ids']
started=datetime.datetime.now(datetime.timezone.utc)
run=report/'runs'/started.strftime('%Y%m%dT%H%M%S%fZ');run.mkdir(parents=True)
result=subprocess.run([sys.executable,'-X','utf8','-m','unittest','-v',*modules],cwd=root,
                      stdout=subprocess.PIPE,stderr=subprocess.STDOUT,encoding='utf-8')
(report/'automated-tests.log').write_text(result.stdout,encoding='utf-8')
summary={'timestamp_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'exit_code':result.returncode,
         'started_utc':started.isoformat(),'python':platform.python_version(),'platform':platform.platform(),
         'modules':modules,'live_square_contacted':False,'scope':'Isolated unit and loopback integration tests with fictional Square responses. Actual PostgreSQL acceptance is separate.',
         'tests_run':int(re.search(r'Ran (\d+) tests?',result.stdout)[1]) if re.search(r'Ran (\d+) tests?',result.stdout) else None,
         'source_sha256':{str(p.relative_to(root)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest()
                          for folder in (root,root/'hosted') for p in sorted(folder.glob('*.py'))},
         'log_sha256':hashlib.sha256(result.stdout.encode()).hexdigest()}
(report/'automated-summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
(run/'automated-tests.log').write_text(result.stdout,encoding='utf-8')
(run/'automated-summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
print(result.stdout[-4500:]);print('Full log:',report/'automated-tests.log')
sys.exit(result.returncode)
