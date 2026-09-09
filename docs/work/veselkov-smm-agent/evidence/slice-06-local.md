# Slice 6 local verification evidence

Date: 2026-09-04
Status: local foundation verified; Windows/live capability gate pending

## Observable result

- A versioned TOML configuration requires explicit Windows paths, task account,
  credential references, target channels and Dzen author identity. No secret value
  is accepted in the configuration.
- `smm-worker --config ... --once` is the production composition seam. It fails
  closed until the non-production live smoke is accepted; replay mode remains
  explicit and cannot be selected implicitly.
- YouTube has a durable resumable-upload receipt, status-query recovery for
  ambiguous 308/timeouts, bounded processing checks, post-processing thumbnail,
  native `publishAt` and explicit read-back.
- Telegram stores task state in SQLite with CAS/fencing, streams multipart video,
  keeps the bot token inside the wire transport, and never automatically repeats
  an ambiguous send.
- Dzen binds the persistent browser session and durable receipt to the configured
  channel, author, article ID/URL, title and approved content hashes. Its optional
  Playwright session is visible/headful and does not automate login, MFA or CAPTCHA.
- Windows adapters generate and register exact T-30/T Task Scheduler plans, check
  task-account consistency and expose NTFS ACL checks. Wake capability is not
  inferred from XML generation or host detection.
- `smmctl capability smoke` emits a redacted machine-readable non-production
  report and remains `productionReadiness=blocked` even when injected fake probes
  pass.

## Reproducible local checks

All test commands ran sequentially through the mandated guard.

```text
/root/.local/bin/codex-test-guard --timeout 20m -- .venv/bin/pytest
146 passed in 379.96s (0:06:19)

/root/.local/bin/codex-test-guard --timeout 10m -- .venv/bin/ruff check .
All checks passed!

/root/.local/bin/codex-test-guard --timeout 10m -- .venv/bin/mypy --strict src
Success: no issues found in 79 source files
```

Dry-run capability commands also remained fail-closed:

```text
.venv/bin/smmctl capability smoke --config config/smm-agent.example.toml
executionRequested=false; allRequiredSmokesPassed=false; productionReadiness=blocked

.venv/bin/smmctl setup validate --config config/smm-agent.example.toml
schemaValid=true; localFoundationReady=false; productionReadiness=not_assessed
```

The latter result is expected on Linux with placeholder Windows paths.

## Remaining live gate

This evidence does not satisfy Slice 6 acceptance. Completion requires the target
Windows 11 laptop, explicit machine paths/task account, Dzen interactive login,
YouTube OAuth for the intended channel, a Telegram bot plus non-production channel,
and permission to register a task and perform a sleep/wake test. The resulting
sanitized smoke report will become `evidence/slice-06.md`.

## 2026-09-09: manual CAPTCHA continuation

The owner confirmed manual CAPTCHA completion in the dedicated Dzen profile.
The waiting diagnostic process resumed and reported both HTTP 400 and a final
article route `/a/aqEI8EFtvlQaguyb`. This is ambiguous publication evidence and
must not be counted as a successful schedule/readback smoke.

The process confirmed its exact-title deletion flow. A separate authenticated
read of that article URL returned HTTP 404, confirming cleanup. The browser
context and waiting process have exited. No new smoke article was created during
this follow-up.

Dzen capability acceptance remains pending: handle `captcha-required-error` as
an interactive-auth requirement, never log the CAPTCHA link or response body,
and verify scheduling/readback before reporting success. The Windows wake gate
also remains pending; production readiness is blocked.

## 2026-09-09: baseline and guarded Windows verification

Owner accepted the full-v1 completion plan. A repository guard now serializes
Windows/CI tests and delegates timeouts to the existing process-tree runner.
64-bit Windows Job Object handle signatures were corrected. Regression checks
exercise a spawned descendant and reject a second guarded command.

Verification: targeted Windows/config/CLI suite: 25 passed; process-tree/guard
regressions: 3 passed. Ruff passed repository-wide; mypy passed all 81 source
files before removal of the duplicate diagnostic Dzen smoke driver.

Prior live observations (2026-09-08), not a new combined acceptance run:
- Telegram native-video send to -1003530344045: passed, 29224 bytes,
  public_at 2026-09-08T11:17:05+00:00.
- YouTube private upload and publishAt readback: passed for the configured
  channel, target 2026-09-10T11:29:23+00:00; test video cleanup confirmed.
- Dzen latest continuation remains failed/ambiguous; HTTP 404 cleanup confirmed.
- Task registration was verified previously, but real wake remains pending.

The duplicate Dzen smoke driver was removed. Its incomplete selectors and raw
response diagnostics must not become a second publishing implementation.
Dzen smoke now explicitly reports pending until production DzenPage uses the
verified live operations. No browser/provider mutation was run during this block.
A single selected passing smoke no longer reports allRequiredSmokesPassed=true.

Remaining external prerequisites: replacement OAuth client secret via protected
setup, Author recording/text/references, media-profile acceptance and sleep window.
No production acceptance, OAuth rotation or full-v1 completion is claimed here.

### Windows integration defects discovered by full suite

Full guarded Windows run: 138 passed, 15 failed, 2 skipped. It was not retried.
Diagnosis used targeted tests only after confirming test processes exited.

Fixed actual host incompatibilities:
- Wide unsigned volume/file identities exceeded SQLite signed INTEGER. Values
  outside signed 64-bit range now use exact hex text at the SQL boundary; old
  integer rows remain readable and no lossy REAL conversion is permitted.
- Python 3.12 Windows path stat and handle fstat disagreed on legacy ctime;
  both now use birthtime on Windows. Device/inode/size/mtime checks remain.
- Re-saving a recording in place preserves Windows birthtime. The recording
  window now considers last write time too; rejected observations compare size
  and mtime so a changed recording is reconsidered after the stability interval.
- Cleanup restores the write bit only on an uncommitted staging artifact.

Targeted result after fixes: 20 passed, 1 skipped (Windows symlink privilege is
not granted; that case remains covered on Linux). A wide-ID roundtrip regression
and the Windows video integration tests are included in CI. The earlier full
suite failure is not relabelled as a pass.

CI follow-up: Linux full suite passed on 31ae5f2. Windows CI initially selected
base Python instead of the uv environment (pytest absent); guard now resolves
plain python/python.exe to its own sys.executable. Local verification using the
same plain-python invocation passed all 3 process/guard tests. Fresh CI pending.
