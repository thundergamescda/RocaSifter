"""Explicit public-code allowlist; excludes data, logs, builds and credentials."""
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
names=[p.name for p in ROOT.iterdir() if p.is_file() and p.suffix in ('.py','.js','.css','.html','.cmd','.vbs')]
names += ['.gitignore','requirements.txt','requirements-dev.txt','requirements-build.txt','requirements-hosted.txt','render.yaml','roca_edition.json','product_brand.json','README.md']
names += [p.relative_to(ROOT).as_posix() for p in (ROOT/'hosted').rglob('*') if p.is_file() and p.suffix in ('.py','.sql','.md','.html','.css','.js')]
names += ['tools/'+n for n in ('build_desktop.py','build_app_icon.py','install_shortcuts.ps1','test_release.py','user_acceptance.py','package_preview.py','stage_publish.py')]
names += ['docs/'+n for n in ('RELEASE_2_6_0.md','DESKTOP_PREVIEW.md','EDITIONS.md','PRICING_PREVIEW.md','STORE_TRIAL.md','SECURITY_PRIVACY_RELEASE_GATES.md')]
files={name:(ROOT/name).read_text(encoding='utf-8-sig') for name in sorted(set(names))}
if len(sys.argv)>1:
    keys=list(files);part=int(sys.argv[1]);keys=keys[part*12:(part+1)*12]
    print(json.dumps({k:files[k] for k in keys},ensure_ascii=True))
else:
    print(json.dumps({'files':list(files),'chunks':(len(files)+11)//12}))
