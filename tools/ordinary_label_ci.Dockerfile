# Source-only candidate. Actual context audit must finish before foreign install.
# No external Dockerfile frontend or additional base image is introduced.
FROM python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS context_audit
WORKDIR /filtered-context
COPY . /filtered-context/
ARG SS_ORDINARY_CONTEXT_EXPECTED_B64
RUN --network=none python -B -I -S /filtered-context/tools/ordinary_label_ci_context.py

FROM python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
# This cross-stage dependency gates every later download/install instruction.
COPY --from=context_audit /context-proof/context-audit.json /opt/ordinary-label-ci/context-audit.json
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TMPDIR=/tmp/ordinary-label-tests \
    ROCA_DATA_HOME=/tmp/ordinary-label-tests/data-home \
    PSYCOPG_IMPL=binary
WORKDIR /workspace
COPY tools/ordinary_label_ci_wheel_urls.txt tools/ordinary_label_ci_requirements.txt /opt/ordinary-label-ci/
COPY tools/ordinary_label_ci_admission.json /opt/ordinary-label-ci/ordinary_label_ci_admission.json
# Concrete dependency holds cannot be waived through environment/CLI settings.
# External source/security review and execution admission are still required.
RUN python -B -I -S -c 'import json,sys; c=json.load(open("/opt/ordinary-label-ci/ordinary_label_ci_admission.json")); sys.exit("ORDINARY_LABEL_CI_EXECUTION_HOLD" if c["execution_hold"] is not None else 0)'
# Exact direct URLs plus full mandatory graph. No dependency omission, source
# build, implicit extras, package upgrade, apt growth or product bundle change.
RUN mkdir -p /opt/ordinary-label-ci/wheels /ci-evidence \
    && python -m pip download --no-index --only-binary=:all: --require-hashes \
       --dest /opt/ordinary-label-ci/wheels -r /opt/ordinary-label-ci/ordinary_label_ci_wheel_urls.txt \
    && python -m pip install --no-index --only-binary=:all: --require-hashes \
       --force-reinstall --no-compile --find-links /opt/ordinary-label-ci/wheels \
       -r /opt/ordinary-label-ci/ordinary_label_ci_requirements.txt \
       --report /opt/ordinary-label-ci/pip-install-report.json \
    && python -m pip check > /opt/ordinary-label-ci/pip-check.txt \
    && python -m pip freeze --all > /opt/ordinary-label-ci/pip-freeze.txt \
    && python -m pip list --format=json > /opt/ordinary-label-ci/pip-list.json
COPY catalog_cleanup.py catalog_physical_ownership.py credit_tracker.py hosted_labels.py intake_label_contract.py intake_manifest.py intake_physical_identity.py pricing_review.py receiving_evidence.py roca_edition.json roca_to_square.py scanner_adapters.py square_variations.py station_cloud_journal_cas.py station_cloud.py station_edition.py station_financial_record.py station_hosted_receive.py station_http_json.py station_intake_claim.py station_intake_receiving_projection.py station_intake_receiving_registry.py station_intake_receiving_review.py station_intake_stock_action_codec.py station_intake_stock_action_registry.py station_intake_stock_action_transport.py station_intake_stock_actions.py station_label_legacy_worker.py station_label_publication.py station_paths.py station_pricing.py station_profile.py station_receiving_action_registry.py station_receiving_action_transport.py station_receiving_actions.py station_receiving_budget.py station_receiving_journal_cas.py station_receiving_label_plan.py station_receiving_label_publication.py station_receiving_label_render.py station_receiving_label_worker.py station_receiving_labels.py station_receiving_reference_fences.py station_receiving_reference_retirement.py station_receiving_registry.py station_receiving_restoration_proof.py station_receiving_restore.py station_recovery_review.py station_server.py station_service.py station_setup.py station_shared_trade.py station_store.py station_trade_recovery.py store_pricing.py test_hosted_receive.py test_shared_trade.py test_station_cloud.py test_station_label_publication.py test_receiving_label_plan.py test_receiving_label_render.py test_receiving_label_worker.py /workspace/
COPY hosted/__init__.py hosted/credit_authority.py hosted/credit_lots.py hosted/database.py hosted/intake.py hosted/ledger.py hosted/receiving_plan.py hosted/security.py hosted/settlement.py hosted/trades.py /workspace/hosted/
COPY tools/ordinary_label_ci_evidence.py tools/ordinary_label_ci_admission.json tools/label_ci_provenance.py tools/test_label_ci_provenance.py /workspace/tools/
RUN chmod 555 /workspace /workspace/tools /workspace/hosted \
    && chmod 444 /workspace/*.py /workspace/roca_edition.json /workspace/tools/* /workspace/hosted/*.py \
    && chown 10001:10001 /ci-evidence
USER 10001:10001
CMD ["python", "-B", "-I", "-S", "/workspace/tools/ordinary_label_ci_evidence.py"]
