# Stock & Sleeve receiving test pilot

November 1, 2026. Internal test build using fictional data. Commercial release remains in development.

The receiving milestone now handles an existing Square variation and a reviewed new price under its existing parent item. A saved operation keeps the original request and provider key through lost replies and recovery on another computer. Catalog confirmation and inventory confirmation are separate steps. Reading or recovering an operation does not authorize additional stock.

The new desktop package runs on Python 3.12.16 and includes the chosen Stock & Sleeve logo and icon. It passed an actual launch, repeated launch, price-research review, canceled quit, and save-and-quit test in a temporary profile. This download does not upgrade other computers or deploy the hosted service.

## What passed

| Check | Result and scope |
| --- | --- |
| Complete local suite | 690 cases passed, with exact case coverage, stable source, and no skips, failures, or errors. Fictional providers and isolated local services. |
| Complete PostgreSQL suite | 258 cases passed in isolated test namespaces, with exact coverage, stable source, and no skips, failures, or errors. |
| Receiving through Chrome and PostgreSQL | Four cases passed using two local desktop profiles, real authentication and local API calls, and fictional Square responses. Concurrent stock authorization and one lost completed reply produced one catalog effect and one inventory effect. |
| Owner catalog review | 16 Chrome cases and five separate Chrome/API/PostgreSQL cases passed. Exact SKU whitespace, owner permissions, fresh MFA, saved approvals, and bounded pagination are covered. |
| Receiving workflow | 10 Chrome cases passed using fictional hosted responses, including recovery and exact card, slab, and sports identity. |
| Shared trade workflow | Nine Chrome cases passed on the final transport code. A shared physical claim could issue fictional credit once; lost replies recovered the original operation without local credit fallback. |
| Privacy export | 11 scoped Chrome cases passed for version 4 customer export; the complete suites also cover the classified hosted collections. Export is not account deletion. |
| Labels | All 18 PDF label profiles rendered and independently decoded the exact fictional payload. Six legacy ZPL comparisons were byte-identical. Physical printer and scanner acceptance is still required. |
| Upgrade rehearsal | Six tests passed for historical schema 3 to schema 6 migration and reapplication using fictional data, including preservation and rollback checks. The production service was not migrated. |
| Desktop package | Five actual executable lifecycle checks and read-only packaged readiness passed on this Windows computer. This does not establish clean-machine installation or every supported workstation. |
| Distribution contents | Exact 142 reviewed notice files and all 56 native DLL/PYD files matched their reviewed sources. Unrelated native files from the earlier build are absent. No unresolved native-library warning was found. |

The rows describe different, sometimes overlapping checks. They are not one additive count or a promise of flawless operation. JSON reports identify their source hashes and raw logs; supplied file hashes detect later changes and do not replace a publisher signature.

## Running this test build

Extract the entire receiving test ZIP, read its test notes, and run `Install Shared Pilot.cmd`. The installer creates a separate pilot program folder, profile, port, and Desktop/Start Menu shortcuts. An occupied shortcut name receives a suffix instead of replacing the existing shortcut. The installation does not start the app or migrate business records. Open the new `Stock & Sleeve Pilot` shortcut to start its local service and window together. Chrome is required; a separate Python installation is not required.

Use fictional cards and customers. Quit inside this pilot to save and stop its service. Retain the entire extracted program folder. Do not copy an existing store profile, credentials, or pending financial journals into the test profile.

Hosted receiving requires the matching reviewed schema 6 service, securely configured Square OAuth, current staff permissions, fresh enrolled MFA, and explicit receiving enablement. The existing Render service has not received this milestone. Installing the desktop alone does not enable it. Independent-store owner enrollment is being developed in a separate prototype and is not part of this receiving ZIP.

## Work required before commerce

Automatic credit redemption through a tracked Square discount is not production accepted. Shared balances, reservations, settlement, retries, and reversal logic have isolated tests; live Square checkout, order linkage, reporting, refunds, and device behavior still require their own acceptance. Do not use this pilot to redeem production customer credit.

Old-SKU cleanup remains review and dry run. This build does not claim that old Square SKUs have been deleted. New parent-item creation, broader catalog workflows, account lifecycle and support, reconciliation operations, and commercial channel integrations remain engineering work.

The product scope continues to include Pokémon, Magic, Riftbound, One Piece, Union Arena, Gundam, sports, and additional scanner-supported games. Game mapping and software barcode tests do not certify a physical scanner or a pricing-data feed. Commercial pricing access and provider permissions must be settled before a feed is included in a sold product.

Release signing, clean-machine install/update/restore, actual two-computer store use, physical printers/scanners, Square OAuth and checkout acceptance, tax treatment, privacy terms, retention/deletion, and rights review remain release requirements. Idaho treatment of deferred customer credit needs written review before a tracked discount is represented as a qualifying trade allowance. The current design does not collect data for sale.

## Owner decisions to record

- Select the first pilot stores, their computers, scanner models, printer models, label stock, and exact Square checkout devices.
- Confirm Square developer application ownership, OAuth configuration, permission scope, and the target checkout/reporting workflow.
- Approve the credit policy and obtain the Idaho tax treatment decision before production redemption.
- Choose the commercial pricing provider and obtain rights for use across customer stores.
- Set account ownership, staff recovery, retention/deletion, support, and rollout policies.
- Confirm the permanent product name, contributor/asset rights, and signing identity before a commercial release.

Routine engineering work can continue while these decisions are recorded. Live provider actions and commerce enablement remain tied to their specific acceptance steps.
