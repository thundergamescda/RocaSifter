# Source-only candidate; execution requires separate source/dependency admission.
# Exact official base from the preserved owned Linux backend run44791269200.
FROM python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TMPDIR=/tmp/label-tests
WORKDIR /workspace
COPY tools/label_ci_wheel_urls.txt tools/label_ci_requirements.txt /opt/label-ci/
COPY tools/label_ci_admission.json /opt/label-ci/label_ci_admission.json
# Concrete dependency holds cannot be waived through environment/CLI settings.
# External source/security review and execution admission are still required.
RUN python -B -I -S -c 'import json,sys; c=json.load(open("/opt/label-ci/label_ci_admission.json")); sys.exit("LABEL_CI_EXECUTION_HOLD" if c["execution_hold"] is not None else 0)'
# Exact direct URLs plus full mandatory graph. No dependency omission, source
# build, implicit extras, package upgrade, apt growth or product bundle change.
RUN mkdir -p /opt/label-ci/wheels /ci-evidence \
    && python -m pip download --no-index --only-binary=:all: --require-hashes \
       --dest /opt/label-ci/wheels -r /opt/label-ci/label_ci_wheel_urls.txt \
    && python -m pip install --no-index --only-binary=:all: --require-hashes \
       --force-reinstall --no-compile --find-links /opt/label-ci/wheels \
       -r /opt/label-ci/label_ci_requirements.txt \
       --report /opt/label-ci/pip-install-report.json \
    && python -m pip check > /opt/label-ci/pip-check.txt \
    && python -m pip freeze --all > /opt/label-ci/pip-freeze.txt \
    && python -m pip list --format=json > /opt/label-ci/pip-list.json
COPY hosted_labels.py station_receiving_label_plan.py station_receiving_label_render.py station_receiving_label_worker.py test_receiving_label_plan.py test_receiving_label_render.py test_receiving_label_worker.py /workspace/
COPY tools/label_ci_evidence.py tools/label_ci_admission.json tools/label_ci_provenance.py tools/test_label_ci_provenance.py /workspace/tools/
RUN chmod 555 /workspace /workspace/tools \
    && chmod 444 /workspace/*.py /workspace/tools/* \
    && chown 10001:10001 /ci-evidence
USER 10001:10001
CMD ["python", "-B", "-I", "-S", "/workspace/tools/label_ci_evidence.py"]
