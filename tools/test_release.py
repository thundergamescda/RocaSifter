"""Save reproducible, redacted logs for isolated tests. No live Square calls."""
from pathlib import Path
import subprocess
import sys
import datetime
import json
sys.stdout.reconfigure(encoding='utf-8')

root=Path(__file__).resolve().parents[1]
report=root/'Testing'/'2.6.0';report.mkdir(parents=True,exist_ok=True)
modules=['test_station_paths','test_station_runtime','test_station','test_roca_to_square',
         'test_roca_update','test_pricing_review','test_credit_cleanup_oauth','test_price_variations',
         'test_hosted_boundaries']
result=subprocess.run([sys.executable,'-X','utf8','-m','unittest','-v',*modules],cwd=root,
                      stdout=subprocess.PIPE,stderr=subprocess.STDOUT,encoding='utf-8')
(report/'automated-tests.log').write_text(result.stdout,encoding='utf-8')
summary={'timestamp_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'exit_code':result.returncode,
         'modules':modules,'live_square_contacted':False,'scope':'Isolated unit and loopback integration tests with fictional Square responses.'}
(report/'automated-summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
print(result.stdout[-4500:]);print('Full log:',report/'automated-tests.log')
sys.exit(result.returncode)
