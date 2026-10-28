# Vendored dependencies

## stitch — snapshot

- **What:** `vendor/stitch/` is a verbatim snapshot of the `stitch`
  package (the substrate library: append-only hash-chained ledgers,
  BIND bookings, `agree()` minting, opt-in Ed25519 signed bookings).
- **Source:** SuperInstance/stitch, package dir `stitch-python/stitch`.
- **Version:** stitch **v0.4.1**, commit **00f77b8**
  (`v0.4.1 release assembly: version bump, SPEC changelog, README
  status, VERIFICATION record`).
- **Copied:** 2026-10-27, by plain directory copy; `__pycache__`
  removed. No files modified — if it differs from upstream v0.4.1,
  that's a bug in this repo, not a patch.
- **Why vendored, not pip-installed:** stitch-substrate is not on PyPI
  (publication is the owner's call and still pending), and the fleet
  code must run on boxes with nothing but Python 3. A snapshot in the
  tree is the dependency story: zero installs, zero network.
- **Usage rule:** fleet code imports it through the `sys.path` shim in
  `fleet/common.py` and uses only the public API (`book_send`,
  `book_receive`, `agree`, `audit_ledger`, `Tenant`, `Ledger`,
  `stitch.crypto.ed25519.Key`). Never edit the snapshot in place;
  re-vendor a newer release instead.

API notes learned from the v0.4.1 source (agreement.py), because the
original build brief guessed two of them wrong:

- The booking signing parameter is **`secret_key=`** (a 32-byte Ed25519
  secret), not `key=`. `agree(..., booking_keys={tenant_identity:
  pubkey_bytes_or_hex})` enforces signatures for keyed parties:
  unsigned bookings refuse as `unsigned_booking`, bad ones as
  `bad_signature`.
- Payloads containing non-finite floats (NaN/±Infinity) are refused by
  `book_send`/`book_receive` with `ValueError` **before** anything is
  written — the SPEC numeric policy, enforced writer-side in v0.4.1.
  Fleet payloads therefore carry strings/ints/bools only.
- `stitch.crypto.ed25519.Key`: `public_bytes`, `secret_bytes`, and
  `identity` are **properties**, not methods (learned by test failure,
  2026-10-27: `'bytes' object is not callable`). `sign`/`verify` are
  methods; `Key.generate()` / `Key.from_secret()` are classmethods.
