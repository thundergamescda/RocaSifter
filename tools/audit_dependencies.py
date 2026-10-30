"""Record installed dependency metadata and release-specific PyPI advisories.

Only public package names/versions leave this machine. No project/customer files
are uploaded. This checks known advisories, not application security or licenses.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from importlib import metadata
import json
from pathlib import Path
import platform
import sys
from urllib.parse import quote
import requests

ROOT=Path(__file__).resolve().parents[1]


def inspect_distribution(distribution):
    value=distribution.metadata
    row={'name':value['Name'],'version':distribution.version,
         'license_expression':value.get('License-Expression'),
         'license_metadata':(value.get('License') or '')[:4000],
         'license_classifiers':[v for v in value.get_all('Classifier',[]) if v.startswith('License ::')],
         'requires_dist':list(distribution.requires or [])}
    url='https://pypi.org/pypi/'+quote(row['name'],safe='')+'/'+quote(row['version'],safe='')+'/json'
    row['source_url']=url
    try:
        with requests.Session() as session:
            session.trust_env=False
            response=session.get(url,headers={'Accept':'application/json'},timeout=(5,20),allow_redirects=False)
        if response.status_code!=200 or len(response.content)>3_000_000:
            row.update(status='unverified',http_status=response.status_code);return row
        body=response.json()
        if not isinstance(body.get('vulnerabilities'),list):raise ValueError()
        row.update(status='checked',active_advisories=[{key:item.get(key) for key in
                   ('id','aliases','summary','fixed_in','link','source')} for item in body['vulnerabilities'] if item.get('withdrawn') is None],
                   withdrawn_advisory_ids=[item.get('id') for item in body['vulnerabilities'] if item.get('withdrawn') is not None],
                   release_files_yanked=bool(body.get('urls')) and all(file.get('yanked') is True for file in body['urls']))
    except (requests.RequestException,ValueError,TypeError):row['status']='unverified'
    return row


def run():
    directory=ROOT/'Testing'/'2.6.0';directory.mkdir(parents=True,exist_ok=True)
    distributions=sorted(metadata.distributions(),key=lambda item:item.metadata['Name'].lower())
    with ThreadPoolExecutor(max_workers=6) as pool:rows=list(pool.map(inspect_distribution,distributions))
    unverified=[row['name'] for row in rows if row['status']!='checked']
    affected=[{'name':row['name'],'version':row['version'],'advisories':row['active_advisories']}
              for row in rows if row.get('active_advisories')]
    result={'timestamp_utc':datetime.now(timezone.utc).isoformat(),'python':platform.python_version(),
            'platform':platform.platform(),'scope':'Installed Windows build/test environment including transitive distributions. Render Linux dependencies and the final frozen binary require their own inventory/scan.',
            'method':'Release-specific PyPI JSON vulnerability field; withdrawn advisories excluded.',
            'documentation':'https://docs.pypi.org/api/json/','packages_checked':len(rows)-len(unverified),
            'unverified':unverified,'affected_packages':affected,'packages':rows,
            'limitations':['No known advisory is not proof of safety. This is not an independent application/penetration review.',
                           'License metadata is an inventory input, not a commercial redistribution rights determination.',
                           'Installed development/test dependencies may not ship in the product. Final bundle and deployed service inventories remain required.']}
    (directory/'dependency-audit.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    lines=[f"Checked {result['packages_checked']}/{len(rows)} installed package releases against PyPI.",
           f'Active-advisory packages: {len(affected)}. Unverified: {len(unverified)}.']
    lines.extend(row['name']+' '+row['version']+': '+', '.join(item['id'] for item in row['advisories']) for row in affected)
    if unverified:lines.append('Unverified: '+', '.join(unverified))
    lines.append(result['scope'])
    (directory/'dependency-audit.log').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print('\n'.join(lines))
    return 1 if affected or unverified else 0


if __name__=='__main__':sys.exit(run())
