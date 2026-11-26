# SOURCE-ONLY / UNADMITTED candidate. No mutable package ranges or image tags.
ARG PYTHON_IMAGE=python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
FROM ${PYTHON_IMAGE} AS context_audit
ARG SS_CI_SOURCE_SHA
ARG SS_FIXED_HOSTED_CONTEXT_SHA256
ENV SS_CI_SOURCE_SHA=${SS_CI_SOURCE_SHA} \
    SS_FIXED_HOSTED_CONTEXT_SHA256=${SS_FIXED_HOSTED_CONTEXT_SHA256} \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
COPY . /context
RUN --network=none python -I -S -B /context/tools/fixed_hosted_ci/fixed_hosted_ci_context.py --audit

FROM context_audit AS dependency_install
# Immutable base-image pip is retained; no bootstrap-tool network upgrade.
RUN mkdir -p /opt/hosted-ci /runtime-controls/wheels \
    && python -I -S -B -c 'import json,sys,sysconfig; from importlib import metadata; from pathlib import Path; versions=[d.version for d in metadata.distributions(path=[sysconfig.get_path("purelib")]) if d.metadata["Name"]=="pip"]; assert len(versions)==1; p=Path("/opt/hosted-ci/pip-bootstrap-report.json"); p.write_text(json.dumps({"operation":"retained_immutable_image_pip_no_upgrade","python":sys.version,"pip":versions[0]}),encoding="utf-8")'
# Exact official URLs + SHA256; no dependency resolver or source distribution.
RUN python -m pip download --no-index --no-deps --only-binary=:all: --require-hashes \
    --dest /runtime-controls/wheels -r /runtime-controls/fixed_hosted_wheel_urls.txt
RUN --network=none python -m pip install --no-index --find-links=/runtime-controls/wheels \
    --no-deps --only-binary=:all: --require-hashes --force-reinstall --no-compile \
    --report /opt/hosted-ci/pip-install-report.json -r /runtime-controls/fixed_hosted_requirements.txt \
    && python -m pip check \
    && python -m pip freeze --all > /opt/hosted-ci/pip-freeze.txt

ARG PYTHON_IMAGE=python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PSYCOPG_IMPL=binary
WORKDIR /workspace
COPY --from=dependency_install /usr/local /usr/local
COPY --from=dependency_install /opt/hosted-ci /opt/hosted-ci
COPY --from=dependency_install /runtime-controls /opt/fixed-hosted-ci
COPY --from=dependency_install /runtime-source /workspace
RUN chmod -R a-w /workspace /opt/fixed-hosted-ci /opt/hosted-ci \
    && mkdir /workspace/Testing /ci-evidence \
    && chown 10001:10001 /workspace/Testing /ci-evidence
USER 10001:10001
CMD ["python", "-I", "-S", "-B", "/opt/fixed-hosted-ci/fixed_hosted_ci_entry.py"]
