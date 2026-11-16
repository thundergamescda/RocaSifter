# Candidate Linux hosted-SQL acceptance image; use run_hosted_ci.sh.
# The harness resolves this exact version tag to a recorded digest before build.
ARG PYTHON_IMAGE=python:3.12.16-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /workspace
COPY requirements-hosted.txt /workspace/requirements-hosted.txt
RUN mkdir -p /opt/hosted-ci \
    && python -m pip install --upgrade pip==26.2.2 \
       --report /opt/hosted-ci/pip-bootstrap-report.json \
    && python -m pip install -r requirements-hosted.txt \
       --report /opt/hosted-ci/pip-install-report.json \
    && python -m pip freeze --all > /opt/hosted-ci/pip-freeze.txt

# The adjacent Dockerfile-specific ignore is an allowlist for public source.
COPY . /workspace
RUN mkdir -p /workspace/Testing /ci-evidence \
    && chown 10001:10001 /workspace/Testing /ci-evidence
USER 10001:10001
CMD ["python", "tools/hosted_ci_evidence.py"]
