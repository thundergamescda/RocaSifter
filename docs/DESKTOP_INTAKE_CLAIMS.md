# Desktop central physical intake — phase 1

This isolated development slice binds new shared trades to one tenant-wide
physical intake. It does not enable hosted Square receiving. Centrally claimed
and hosted-purchase batches cannot fall back to the local Square token, local
cash/store-credit acceptance or the local wallet. Ordinary unclaimed legacy
receiving retains its original provider, request keys and algorithm.

## Operator flow

1. Import or enter the original cards, then review their exact identity, quantity
   and price. The original source is captured before subsequent edits or price
   transformations; reviewed corrections retain their original source-line key.
2. Open **Shared customer credit**. An owner or manager creates or opens the
   central physical intake tag/reference. Use the same physical tag on every
   computer handling those actual cards. A file hash, local receipt ID or random
   manual request ID is not proof of a different physical stack.
3. If an equivalent reordered source already has a reference, explicitly select
   that existing central ID after checking the actual cards. The desktop checks
   the original request and authoritative source evidence; it does not invent a
   replacement claim or map reordered rows by position.
4. Select the shared customer and review the saved hosted offer. Confirm the
   central reference and selected/all/excluded counts, customer agreement and
   physical receipt. Every new acceptance requires server-checked owner/manager
   authority and enrolled MFA verified within 15 minutes. Browser flags are not
   authorization. The server applies this requirement to cash agreements too;
   this desktop credit flow does not execute a cash payment.
5. If verification is required, use **Open hosted security setup**, enroll there
   if needed, then sign out and sign in **here** for fresh proof. This fixed-origin
   link carries no credentials or query tokens. There is no automatic sign-out;
   the saved batch and pending journals remain available.

One accepted operation consumes the intake reference. Excluded copies remain
**NOT RECEIVED** and cannot be reused in a continuation or called a new physical
delivery. Quantity-budgeted continuation is future work. The UI shows selected
versus original copies and the excluded count before acceptance.

A genuinely separate delivery of identical cards requires a different physical
tag, a linked original intake, a 10–200 character manager reason and explicit
physical attestation. The server requires the same fresh enrolled MFA. A failed
or uncertain additional-delivery request retains its exact key and source; it
cannot be changed into the base intake as a retry shortcut.

## Original-source limits and older receipts

`source_manifest` stores complete ordered original identity/quantity rows using
the shared pure `intake_manifest.py` contract. It excludes prices, review flags,
notes and local UUIDs. The CSV reference uses the existing BOM/newline-normalized
source-digest convention. Semantic deduplication separately groups exact original
identities and quantities; source row positions are provenance, not join keys.

The manifest is bounded to 1,000 rows, 100,000 copies per row, one million copies
total and 512 KiB. Missing game or incomplete slab facts also prevent a complete
manifest. The ordinary local intake remains intact with actionable hosted-intake
guidance; it is never truncated or silently replaced with selected rows.

Older receipts without an original manifest are not backfilled from edited
cards. Reimport the verified original CSV or use owner review of a new central
manual intake. Later manual entries preserve earlier original manual rows;
adding separate manual cards to a scanner batch does not rewrite its CSV source.
Once a central reference exists, adding different physical cards requires a new
batch. Canceling a hosted draft leaves its original central reference intact.

## Recovery and durable boundaries

The local original-source intent is saved before the dedicated encrypted
`cloud.intake` journal and before cloud I/O. The original `cloud.pending` v1 and
SQLite receiving journal are not converted or rekeyed. A confirmed response
stays journaled until the local mirror is durably saved. Restart/lost-response
recovery uses the original request key and authoritative server reference.
Malformed or unreadable journals fail closed with owner-recovery guidance;
retain the original action and use the same Windows account.

New shared intents carry a versioned claim binding and retain exact original
source-line keys through corrections and splits. Legacy trade fingerprints and
pending request bodies remain unchanged. Already accepted outcomes recover their
original receipt without a new MFA-gated issuance. Explicit versioned draft
cancellation can resolve an uncommitted legacy acceptance: accept and void race
under the same server row lock, and journals are cleared only after a durable
terminal result. A copied batch can mirror an already accepted trade only when
its source, customer, rows, amount and claim evidence match exactly.

Keep new profiles separate from older binaries: those versions do not know the
new claim and routing guards. Never point a pre-claim binary at a migrated profile.

## Evidence and limits

The current focused desktop slice covers original capture, journal encryption,
pre-I/O disk failures, restart/lost-response recovery, source/store/session guards,
legacy request preservation, cancellation and the existing intake/MFA/updater/
runtime paths. The shared-trade Chrome report is saved under
`Testing/oauth-receiving/shared-trade-user-acceptance.json` and its adjacent log.
It includes two separate loopback desktop origins, server-authoritative MFA
denial, manual tags and linked additional-delivery recovery. Historical evidence
is retained under that folder's `history` directory.

These are fictional browser/transport and disposable-database checks. They do not
prove physical identity by hash, live Square/hosted deployment, scanner/printer
hardware, tax classification, checkout redemption or commercial acceptance.
No merchant data, live provider calls or actual MFA enrollment was used here.
