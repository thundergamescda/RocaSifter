"""Explicit public-code allowlist; excludes data, logs, builds and credentials."""
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
names=[p.name for p in ROOT.iterdir() if p.is_file() and p.suffix in ('.py','.js','.css','.html','.cmd','.vbs')]
names += ['.gitignore','requirements.txt','requirements-dev.txt','requirements-build.txt','requirements-hosted.txt','render.yaml','roca_edition.json','product_brand.json','README.md']
names += [p.relative_to(ROOT).as_posix() for p in (ROOT/'hosted').rglob('*') if p.is_file() and p.suffix in ('.py','.sql','.md','.html','.css','.js')]
names += ['tools/'+n for n in ('build_desktop.py','audit_dependencies.py','install_hosted_dependencies.py','build_app_icon.py','install_shortcuts.ps1','test_release.py','user_acceptance.py','user_acceptance_cloud.py','user_acceptance_intake.py','user_acceptance_desktop.py','user_acceptance_import.py','user_acceptance_shared_trade.py','test_cloud_races.py','test_hosted_portal.py','test_hosted_onboarding.py','test_hosted_auth_transitions.py','test_portal_races.js','package_preview.py','stage_publish.py')]
names += ['docs/'+n for n in ('RELEASE_2_6_0.md','DESKTOP_PREVIEW.md','EDITIONS.md','PRICING_PREVIEW.md','STORE_TRIAL.md','SECURITY_PRIVACY_RELEASE_GATES.md','SCANNER_INTAKE.md','SHARED_TRADE_BRIDGE.md','HOSTED_RECONCILIATION_INTEGRATION.md')]
names += ['tools/pilot_installer.ps1','tools/benchmark_receipt_lists.py']
names += ['tools/test_hosted_mfa.py']
names += ['tools/test_hosted_postgres.py','docs/HOSTED_MFA.md']
names += ['tools/user_acceptance_mfa.py','docs/DESKTOP_MFA.md']
names += ['tools/user_acceptance_privacy.py','docs/HOSTED_CUSTOMER_EXPORT.md']
names += ['tools/pg_logical_restore_drill.py','tools/test_pg_restore_drill_guards.py','Planning/LOCAL_LOGICAL_RESTORE_DRILL.md']
names += ['docs/SHARED_PILOT_PACKAGE.md','docs/RECEIPT_LIST_PERFORMANCE.md']
names += ['docs/HOSTED_TRADES.md','docs/SHARED_TRADE_BRIDGE.md','docs/PRICING_RESEARCH.md']
names += ['Planning/SECURITY_PRIVACY_RELEASE_GATES.md']
names += ['Planning/SQUARE_IDAHO_ACCEPTANCE_2026-10-28.json']
names += ['tools/test_evidence_guards.py','Planning/SQUARE_TOKEN_AUTHORITY_EVIDENCE.json']
names += ['docs/PHYSICAL_INTAKE_CLAIMS.md','Planning/OAUTH_RECEIVING_DOMAIN_CONTRACT.md',
          'Planning/OAUTH_RECEIVING_ACCEPTANCE_MATRIX.json']
names += ['docs/DESKTOP_INTAKE_CLAIMS.md','Planning/OAUTH_RECEIVING_SCHEMA6_PROPOSAL.md']
files={name:(ROOT/name).read_text(encoding='utf-8-sig') for name in sorted(set(names))}
if len(sys.argv)>1:
    keys=list(files);part=int(sys.argv[1]);keys=keys[part*12:(part+1)*12]
    print(json.dumps({k:files[k] for k in keys},ensure_ascii=True))
else:
    print(json.dumps({'files':list(files),'chunks':(len(files)+11)//12}))
