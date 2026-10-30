# Security and privacy release gates

Review baseline October 27 2026. This is an engineering register, not a legal
certification or a public privacy-policy promise. Commercial release requires
the applicable legal review plus evidence from the deployed system.

## Initial data use

Collect only information needed for store accounts, trade agreements, credit,
inventory, order fulfillment and support. Contact details remain optional where
not needed. No advertising SDKs, customer-data sale feeds, cross-store profiling
or model-training pipeline are part of this release. Do not collect card numbers,
CVVs, Social Security numbers, identity-document images or sensitive free-text
notes for credit tracking. Square handles payment entry.

For every category record source, purpose, authority/consent, processors,
retention, deletion method and access roles before cloud ingestion. Distinguish
Square-derived data from independently supplied store records. Store staff
must have authority to record customer information. Optional marketing needs
separate choices; creating a credit account is not permission for marketing.

## Implemented and tested in the local preview

Loopback-only service, exact local Host/Origin checks, HttpOnly SameSite admin
cookies, password hashing, login throttling, credential vault, no credential
fields in operator settings, reviewed stock approval and retry journal are
preserved. New manager adjustments require Admin authorization. Financial
amounts are stored in cents; reservations are transactional. Customer names,
references and notes render as escaped text; CSV references/notes are protected
against formula interpretation. Readiness reports omit credentials and user
paths. New OAuth-domain state is session/tenant-bound, short-lived and single
use; token exchanges and refreshes are tested with fictional responses only.

The local SQLite customer database is not application-level encrypted. Protect
the workstation account, OS disk and backups. Do not describe local credit
records as encrypted merely because the Square token uses DPAPI. Local Windows
account security is not a substitute for hosted per-store/per-staff access.

## Commercial cloud release requirements

- Authenticated stores and staff, least privilege, MFA for owners/operators of
  hosting, access review and tenant isolation tests against PostgreSQL.
- TLS at public boundaries, private database networking, encrypted server
  credentials with rotation/revocation, no secrets in desktop installers/logs.
- Signed Square webhooks, durable idempotent jobs, rate limits and replay tests.
- Verified backup restoration, log redaction, defined retention and secure
  disposal, dependency scanning, signing and reviewed update delivery.
- Customer access/correction/deletion workflow with identity verification,
  audit and retention exceptions reviewed by counsel. Do not erase required
  accounting records blindly; reconcile those requirements with Square's
  revocation/deletion obligations before promising a retention period.
- Incident owner, contact paths, evidence preservation, vendor escalation and
  jurisdiction-specific notification deadlines; exercise an incident drill.
- Published accurate privacy notice, store service agreement, processor/DPA
  terms and subprocessor list. Confirm permitted launch states/countries.
- Payment/security scope assessed for the selected Square checkout integration;
  outsourcing card entry does not by itself certify PCI compliance.

## Legal and platform review

Idaho's Attorney General identifies Title 28 chapter 51 as its breach framework.
Assess covered information, investigation and notice duties; do not copy the
public-agency 24-hour AG notice rule into a commercial-business policy as though
it applies universally.
https://www.ag.idaho.gov/consumer-protection/security-breaches/

The FTC recommends minimizing stored personal information, protecting it,
disposing of it safely and planning incident response. These are engineering
inputs; statutory and contractual obligations still need an applicability review.
https://www.ftc.gov/business-guidance/resources/protecting-personal-information-guide-business

California privacy rights and applicability depend on operations and thresholds,
not solely on the developer's Idaho address. Assess CCPA/CPRA and other state
requirements as stores/customers expand; review GDPR/UK rules before targeting
those markets. Address minors if their information enters store workflows.
https://oag.ca.gov/privacy/ccpa

Square's developer terms restrict use/redistribution of content, require seller
authorization, require honoring revocation/deletion and impose security/privacy
obligations. They also restrict using Square data for AI training/enhancement,
including anonymized data. Do not infer that seller OAuth consent grants general
resale or training rights. Recheck current terms before launch.
https://squareup.com/us/en/legal/general/developers

Square's information-collection guidance recommends minimization, explicit
permission, avoiding PII in logs, respecting marketing choices and HTTPS.
https://developer.squareup.com/docs/build-basics/general-considerations/collecting-information

## Future revenue ideas

Prioritize subscription fees, paid store analytics and workflow features. A
future opted-in aggregate benchmarking product is a research candidate only:
obtain necessary store/customer permissions and provider rights, apply minimum
cohort sizes and suppression, prohibit re-identification and review privacy and
competition risks. Removing names alone is not adequate anonymization. Do not
collect extra information now on the assumption it can be sold later.

Owner decisions: legal operating entity/privacy contact, launch jurisdictions,
retention periods, processors, incident ownership and counsel review. All
monetization of data remains outside the initial scope.
