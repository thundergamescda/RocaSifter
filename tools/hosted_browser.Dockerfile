# Isolated Linux hosted-portal fixture; no desktop or native test selection.
# Derived from Planning/hosted-browser-candidate-20261117-r1 (PROVENANCE.txt).
# The harness resolves this version tag to a recorded official image digest.
ARG PYTHON_IMAGE=python:3.12.16-slim
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ss-playwright
WORKDIR /workspace
COPY requirements-hosted.txt /workspace/requirements-hosted.txt
COPY tools/hosted_browser_constraints.txt tools/hosted_browser_requirements.txt /workspace/tools/
RUN mkdir -p /opt/hosted-browser-evidence \
    && python -m pip install --only-binary=:all: pip==26.2.2 --report /opt/hosted-browser-evidence/pip-bootstrap-report.json \
    && python -m pip install --only-binary=:all: -r tools/hosted_browser_requirements.txt --report /opt/hosted-browser-evidence/pip-install-report.json \
    && python -m playwright install --with-deps chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends libnss3-tools openssl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip freeze --all > /opt/hosted-browser-evidence/pip-freeze.txt \
    && dpkg-query -W > /opt/hosted-browser-evidence/os-packages.txt \
    && groupadd --gid 10001 ssbrowser \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/ssbrowser --shell /usr/sbin/nologin ssbrowser
# The adjacent deny-by-default Dockerfile.dockerignore is part of this contract.
COPY . /workspace
RUN mkdir -p /ci-runtime /ci-evidence \
    && chown 10001:10001 /ci-runtime /ci-evidence \
    && chmod 700 /ci-runtime /ci-evidence /home/ssbrowser
USER 10001:10001
CMD ["python", "tools/hosted_browser_ci.py"]
