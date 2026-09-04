# Slice 5 review fixes

Independent review and an additional restart-path audit found six recovery
risks. This change closes them with executable coverage:

- notification request payloads now bind notification, incident, recipient,
  request hash and release; legacy durable jobs are backfilled by migration
  `0006`;
- every local notification/attempt change is gated by the live `JobClaim`, so
  an expired worker cannot commit alert state after a replacement is claimed;
- terminal recovery records the actual failed platform, including YouTube and
  Dzen status-only failures;
- provider errors have a typed path, including terminal `DZEN_DOM_MISMATCH`;
- cancellation reconciliation observes status only and never cancels a remote
  publication after a public receipt may exist;
- target-time work wins over an overdue preflight after restart; the obsolete
  preflight is closed without provider cancellation.

Provider-derived detail is redacted at every SQLite persistence boundary.
Verification is captured by the Slice 5 integration, contract and unit tests.
