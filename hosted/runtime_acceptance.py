"""Exercise the actual Linux WSGI worker runtime before a pilot deploy.

Uses the existing database only for schema readiness reads. No accounts,
customers, balances or Square data are changed. Proxy headers model Render's
HTTPS edge; the test listener binds loopback only and is terminated on exit.
"""
import os
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit
import requests


def main():
    origin=os.environ.get('PUBLIC_BASE_URL') or os.environ.get('RENDER_EXTERNAL_URL','')
    uri=urlsplit(origin)
    if uri.scheme!='https' or not uri.netloc:raise SystemExit('Configure the hosted HTTPS origin before runtime acceptance.')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    command=[sys.executable,'-m','gunicorn','hosted.wsgi:app','--bind',f'127.0.0.1:{port}',
             '--workers','2','--threads','2','--timeout','30','--error-logfile','-']
    process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    session=requests.Session();session.trust_env=False
    headers={'Host':uri.netloc,'X-Forwarded-Proto':'https'}
    base=f'http://127.0.0.1:{port}';deadline=time.monotonic()+45
    passed=False
    try:
        while time.monotonic()<deadline:
            if process.poll() is not None:raise RuntimeError('WSGI process exited before readiness.')
            try:
                response=session.get(base+'/readyz',headers=headers,timeout=12)
                if response.status_code==200 and response.json().get('service_ready'):
                    passed=True;break
            except (requests.RequestException,ValueError):pass
            time.sleep(.5)
        if not passed:raise RuntimeError('WSGI worker readiness did not pass within 45 seconds.')
        # Repeated new connections exercise concurrent worker dispatch and pool reuse.
        from concurrent.futures import ThreadPoolExecutor
        def probe(_):
            with requests.Session() as client:
                client.trust_env=False
                result=client.get(base+'/readyz',headers=headers,timeout=12)
                return result.status_code==200 and result.json().get('database_ready') is True
        with ThreadPoolExecutor(max_workers=4) as pool:
            if not all(pool.map(probe,range(12))):raise RuntimeError('Concurrent WSGI readiness failed.')
        result=session.get(base+'/api/me',headers=headers,timeout=12)
        if result.status_code!=401:raise RuntimeError('Unauthenticated WSGI access was not rejected.')
        print('PASS actual Gunicorn: two workers, concurrent PostgreSQL readiness, unauthenticated rejection.')
    finally:
        session.close();process.terminate()
        try:output,_=process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill();output,_=process.communicate()
        # The app logs no DSNs, credentials or request bodies; failure types are enough.
        if not passed:print('Runtime acceptance failed. Inspect private provider runtime logs; no secret values printed.')


if __name__=='__main__':main()
