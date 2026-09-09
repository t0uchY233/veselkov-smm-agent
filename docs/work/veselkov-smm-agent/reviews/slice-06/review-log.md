# Slice 6 local foundation review log

Reviewer: `gpt-5.6-terra`, reasoning `xhigh`, fresh read-only context.

The first local foundation review found two critical and seven high gaps. The
review correctly separated locally fixable composition/recovery/security defects
from checks that physically require the Windows laptop and test accounts.

## Fixed locally

- Added a config-driven production worker, exact Task Scheduler lifecycle and a
  machine-readable, fail-closed live-smoke harness.
- Added concrete redacting HTTPS/OAuth and secret-read boundaries. Telegram token
  substitution happens only immediately before socket I/O.
- Hardened YouTube resumable restart, 308 without Range, processing status,
  thumbnail ordering, schedule read-back and remote cancellation.
- Replaced Telegram JSON task state with SQLite CAS/fencing, streamed multipart
  bodies and an explicit ambiguous-send policy that forbids automatic resend.
- Bound Dzen receipts to configured channel/author identity and immutable content
  hashes; added a lazy headful persistent Playwright session boundary.
- Added account and NTFS ACL checks; Windows host detection alone no longer reports
  Task Scheduler as live-ready.

## Still externally blocked

- Real Dzen selectors, session and scheduled URL can be verified only after manual
  login/MFA on the target laptop.
- YouTube OAuth, channel ownership, private upload and `publishAt` read-back require
  the intended Google account and test asset.
- Telegram native-video receipt requires a bot with rights in a non-production
  channel.
- Task registration, logged-off execution, NTFS DACL and wake-from-sleep require
  the actual Windows 11 account and power state.

No live capability is claimed by the local test doubles.

## 2026-09-09 baseline review pass

Separate read-only review pass by the implementing agent; this is not a second
reviewer identity, GitHub approval, or final owner risk acceptance.

Findings addressed: selected-only smoke reported all-required success; FFmpeg
smoke lacked a deadline; Windows Job Object handles lacked 64-bit argtypes;
TOML test paths were not portable; Windows volume IDs overflowed SQLite;
stat/fstat ctime differed; read-only cleanup masked an ingest failure.
The incomplete standalone Dzen driver was removed from the smoke path.

Review decision: ready for GitHub CI, production blocked. Ruff and strict mypy
passed. Targeted Windows tests passed; prior full Windows failure stays recorded.
Fresh Linux full-suite and Windows integration CI must pass before merge.

Remaining: Dzen production operations, readiness wiring, OAuth rotation,
real media calibration, scheduler wake and packaging are not accepted.
