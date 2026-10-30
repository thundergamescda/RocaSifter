"""Install pinned build tooling before the hosted service dependency set."""
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


if __name__=='__main__':
    subprocess.run([sys.executable,'-m','pip','install','--upgrade','pip==26.2.2'],cwd=ROOT,check=True)
    subprocess.run([sys.executable,'-m','pip','install','-r','requirements-hosted.txt'],cwd=ROOT,check=True)
