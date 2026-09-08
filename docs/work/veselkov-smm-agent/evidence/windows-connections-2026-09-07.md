# Windows connection setup continuation

Date: 2026-09-07
Status: local connections verified; production readiness remains incomplete.

## Authority and scope

The owner asked to continue the previous Windows setup step by step, supplied
an OAuth client and the separate Telegram test channel. This work continues the
accepted Slice 6 setup plan. It does not accept a publication gate.

## Completed operations

- Replaced only the Telegram placeholder in the ignored local
  `config/smm-agent.toml` with the supplied test channel, `@testall122`.
- Parsed the resulting TOML successfully and checked `git check-ignore`.
- Stored the supplied client secret in the current Windows account's Credential
  Manager, target `VeselkovSmmAgent/YouTubeClientSecret`. An exact read-back matched;
  neither the secret nor its hash was written to the report or config.
- Inspected only availability for `YouTubeOAuth`, `TelegramBot`, and `TaskAccount`
  targets under `VeselkovSmmAgent/`: all were absent before this OAuth attempt.
- Prepared an operational bootstrap outside the repository at
  `D:\VeselkovSmm\runtime\youtube_oauth_setup.py` and parsed its Python syntax.
- Started that script using the project's `pythonw.exe`; its status confirmed
  `waiting_for_browser_consent` at 08:47:47 UTC with a 900-second consent window.

## OAuth behavior and review

The script opens the system browser, binds its callback only to `127.0.0.1` on
an ephemeral port, checks a random state, and uses PKCE S256. It requests the
YouTube `youtube.force-ssl` scope for the planned video-management API operations.
No video is uploaded, changed, scheduled, or deleted by this bootstrap.

The callback does not log request URLs or authorization codes. Exchange and
refresh use Google's HTTPS token endpoint. The refresh token is stored as the
raw credential expected by `RefreshingOAuthCredential`, at
`VeselkovSmmAgent/YouTubeOAuth`; the client secret remains a separate credential.
An existing refresh credential is preserved and reused on subsequent runs.

After consent, the script verifies the existing application's token refresher,
queries `channels.list(mine=true)`, and replaces the YouTube config placeholder
only if exactly one valid channel is returned. A missing/multiple channel or a
conflicting configured channel requires further resolution. Status output
contains only stages, bounded error codes, and non-secret channel metadata.

A separate source inspection covered callback validation, credential write/read,
external destinations, config replacement, and failure reporting. This is an
operational helper, not a reviewed production OAuth command. No automated tests
were run in this continuation, and no full-suite or live-smoke success is claimed.
The prior agent's modifications to `config.py` and `test_config.py` were preserved.

## Resume in the same Windows account

Read `D:\VeselkovSmm\runtime\youtube-oauth-status.json` first. The status is
sanitized and may be read; never print credential values or browser callback URLs.
If it is still waiting, let the user finish consent before starting another session.
If it completed, read the exact channel ID/title and confirm the config update.
If it timed out, rerun the bootstrap after confirming its old process exited.

```powershell
Get-Content -Encoding UTF8 D:\VeselkovSmm\runtime\youtube-oauth-status.json
Start-Process -FilePath D:\smm_agent_v4\.venv\Scripts\pythonw.exe `
  -ArgumentList D:\VeselkovSmm\runtime\youtube_oauth_setup.py `
  -WorkingDirectory D:\smm_agent_v4 -WindowStyle Hidden
```

A lock prevents overlapping bootstrap sessions. Do not remove the lock while its
recorded process is alive. Browser consent, refresh verification and channel
read-back must actually succeed before reporting YouTube connected.

## Remaining steps

1. Finish Google browser consent and inspect the resulting channel identity.
2. Save the Telegram bot token through local secure input to Credential Manager;
   verify that the bot can post in the supplied test channel. Do not request the
   token in chat. No Telegram message was sent by this continuation.
3. Complete manual Dzen login and resolve the exact author identity.
4. Resolve remaining local configuration/asset/account requirements and rerun
   setup validation.
5. Wire and review actual provider/smoke composition before live capability runs:
   the current CLI uses `UnavailableCapabilitySmokeProbes` and the default worker
   uses `UnavailableProviderFactory`. OAuth credentials alone do not connect them.
6. Complete isolated capability checks and obtain human acceptance of Slice 6.

Production readiness remains blocked.

References: [Google installed-app OAuth](https://developers.google.com/identity/protocols/oauth2/native-app),
[YouTube channel discovery](https://developers.google.com/youtube/v3/docs/channels/list).
## Follow-up: manual link and redirect mismatch

The first browser session timed out without storing a refresh credential. At the
owner's request, the bootstrap was changed to write its Google authorization URL
to `D:\VeselkovSmm\runtime\youtube-oauth-login-url.txt` instead of opening the
browser automatically. The URL has state and a PKCE challenge, but no secret,
verifier, access/refresh token, or authorization code. Only use it while the
corresponding callback listener is active.

The owner's screenshot of the next attempt shows Google's HTTP 400
`redirect_uri_mismatch`. The actual OAuth client type and allowed redirect URI
have not yet been verified in Google Cloud. Do not infer Desktop type solely from
the provided client ID or report this error as caused by the selected account.

For future attempts, `prompt` is now `select_account consent` to show account
selection explicitly. The updated script parses successfully. This change alone
does not resolve the redirect mismatch; inspect the client's Application type
and allowed callback configuration before retrying. No OAuth success is claimed.
## Confirmed Web application client

The owner confirmed that the supplied client is of type Web application.
The operational bootstrap now binds only `127.0.0.1:8765`, retaining PKCE,
state validation, manual link delivery and explicit account selection.
The exact redirect URI to register in the existing Google Cloud client is
`http://127.0.0.1:8765/oauth2callback` (no trailing slash).
Python syntax parsed successfully; a local socket could bind port 8765.
The obsolete random-port consent session was closed after checking its process
identity. A new OAuth session is deferred until the owner saves this authorized
redirect URI. Cloud configuration and OAuth success are not yet verified.
This supersedes the earlier ephemeral-port description for this Web client.
## Owner-authorized client replacement

The owner explicitly replaced the original client with
`637062293419-1oube7dv9q9sl2fir3j58isbsgr2dre0.apps.googleusercontent.com`.
This is now the authoritative client ID for the local bootstrap. The earlier
instructions to find/use the 514793154006 client are superseded.

The corresponding newly supplied secret replaced the previous client secret in
Windows Credential Manager at the existing YouTubeClientSecret target; an exact
read-back matched without outputting its value. There was no refresh credential
to migrate and no active OAuth session lock. The bootstrap syntax parsed after
the client change. The registered callback remains
`http://127.0.0.1:8765/oauth2callback`, with explicit account selection and PKCE.
The owner's screenshot showed this callback on the replacement client's page.
Actual authorization, refresh verification and channel discovery remain pending.
## Consent callback still pending after client replacement

After the owner reported completing login, local status showed
`browser_consent_timeout` at 2026-09-07T12:35:26 UTC. YouTubeOAuth was absent and
the channel ID remained the unresolved placeholder. This does not establish
whether the user completed Google's consent screens; it establishes that the
local callback did not complete during the session. No connection success is
claimed. The next session's browser-consent window was extended from 900 to 3600
seconds to allow enough time for the manual process. The same replacement client,
fixed loopback callback, state validation and PKCE remain in use.
## YouTube OAuth completed and verified

At 2026-09-07T13:27:05 UTC the active replacement-client session completed.
A subsequent read confirmed:

- channel ID: `UCPL3MascatiZkkBwruDgPYA`;
- channel title: Сергей Николаевич;
- refresh credential saved in Windows Credential Manager;
- existing application token refresher successfully verified;
- `channels.list(mine=true)` returned the channel above;
- the ignored local config contains that channel ID.

The displayed channel title was re-read with JSON Unicode escapes to avoid the
Windows console encoding replacing Cyrillic characters. No credential values
were printed. This closes the OAuth/bootstrap step, not the YouTube publication
capability smoke or Slice 6 acceptance. Production readiness remains blocked.

Telegram is configured for `@testall122`, but its `VeselkovSmmAgent/TelegramBot`
credential is still absent. The next manual setup step is to add that generic
credential in Windows Credential Manager under the same Windows account. Its
password field should contain the bot token. Do not send that token in chat.
## Telegram credential and identity verified

Read-only Bot API checks at 2026-09-07T13:32:19 UTC confirmed the generic
`VeselkovSmmAgent/TelegramBot` credential is present and accepted by `getMe`.
The existing credential-aware Telegram transport was used; no token value was
printed or written to a file.

- Bot: `@Veselkov_smm_bot`, ID `8506294274`.
- Configured test channel: `@testall122`.
- `getChat` resolved channel `Test_all`, ID `-1003530344045`, type `channel`.
- `getChatMember` for this bot returned HTTP 400. A follow-up diagnostic confirmed
  the allowlisted error `Bad Request: member list is inaccessible`.

The bot's posting rights are not verified. The next owner action is to add this
bot as an administrator of the test channel with permission to post messages,
then repeat the read-only membership check. No Telegram message was sent;
`telegram.test_send` capability remains unexecuted and production remains blocked.
Sanitized machine-readable status: `D:\VeselkovSmm\runtime\telegram-setup-status.json`.
## Telegram posting permissions verified

At 2026-09-07T13:35:31 UTC, fresh read-only `getMe`, `getChat` and
`getChatMember` requests succeeded for the configured bot and test channel.
`@Veselkov_smm_bot` (8506294274) is an administrator of `@testall122`
(-1003530344045), with `can_post_messages=true`. The API also reported edit and
delete permissions. This supersedes the earlier inaccessible-membership finding.
No message or video was sent; a real test-send capability is still pending.
The sanitized status is saved in `D:\VeselkovSmm\runtime\telegram-setup-status.json`.

Next setup activity is the manual Dzen login in the already configured dedicated
profile. The existing adapter uses Playwright's bundled Chromium, so preparation
uses that browser rather than an unrelated personal Chrome profile.
## Dzen channel binding confirmed

The owner completed a manual login in the dedicated profile and supplied a
visible Studio screenshot. The profile exposes two channels:

- `Экономика не для всех!` — the only target for SMM-agent articles, with
  view/edit access and configured URL `https://dzen.ru/ekonomikadliavseh`;
- `Админ Экономика не для всех` — a separate empty channel marked `Основной` in
  the profile selector; all SMM-agent mutations in this channel are forbidden.

The owner explicitly resolved this distinction. The ignored local config now
uses `Экономика не для всех!` as `dzen.author_identity`, and the rule is recorded
in `CONTEXT.md` and the accepted publication 7w3. A provider smoke must still
prove the selected channel identity immediately before creating a draft.

Playwright 1.62.0 and its Chromium 151 bundle were installed for the accepted
Dzen adapter. Direct launch of the bundled executable failed on this Windows
host with WinError 14001 (side-by-side configuration). The manual session was
therefore opened through installed Chrome using the same dedicated profile.
Production adapter compatibility with that browser remains an unresolved local
capability item; no Dzen draft, schedule, or public mutation was performed.
## Windows ACL validation corrected

The folders already granted `DESKTOP-R00H9C9\Ассистент` inherited Modify access,
but `setup validate` reported both ACL checks unavailable. A byte-level diagnostic
showed `whoami.exe` emits UTF-8 on this host while `icacls.exe` emits the active
Windows OEM code page. The shared runner decoded both as UTF-8, corrupting the
Cyrillic account name before comparison.

The runner now captures bytes and decodes only `icacls.exe` as OEM on Windows;
other commands retain UTF-8. A regression case covers a Cyrillic account encoded
as CP866. Fresh `smmctl setup validate` changed both data-root and Dzen-profile
ACL capabilities from `unavailable` to `available`; current-account matching also
remained available. No ACL was changed.

The repository-mandated `/root/.local/bin/codex-test-guard` is absent on this
Windows host and WSL is not installed. Therefore no test command was run; the
new regression test remains pending execution on a host with the guard. A source
diff check reported no whitespace errors.
## Scheduler registration and Telegram text connectivity

The `VeselkovSmmAgent/TaskAccount` credential is present. Three registration-only
test tasks were created with a trigger seven days in the future and deleted in a
`finally` block before they could run. `schtasks /Create` accepted the credential.
The queried task XML normalized `DESKTOP-R00H9C9\Ассистент` to SID
`S-1-5-21-3208673037-314243514-781052425-1001`; `whoami /user` returned the same
SID. Each temporary task was deleted, and the first deletion was followed by an
absence check. No worker or publication command ran. Wake-from-sleep remains
untested.

At 2026-09-07T14:47:42 UTC the configured bot sent one silent text-only setup
message to `@testall122`; Telegram returned channel ID `-1003530344045` and
message ID `3`. The text explicitly said that no release publication was being
performed. This verifies Bot API write connectivity only. Native-video upload,
caption readability and ambiguous-send recovery remain pending, so the formal
`telegram.test_send` capability is not yet complete.

Fresh setup validation confirms all credentials, Windows account matching, both
ACL checks, Dzen identity/profile, YouTube, Telegram and backup are available.
Local foundation remains false only because the calibration corpus and accepted
media/crop profile files do not exist. The recording inbox and portrait reference
folders are currently empty. These artifacts cannot be invented: they require a
real Author recording, reference images, measured ASR/alignment results and human
acceptance.