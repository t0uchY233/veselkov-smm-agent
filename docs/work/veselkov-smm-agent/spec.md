---
title: "Спецификация SMM-агента Сергея Веселкова"
status: accepted
version: 0.2.1
design: smm-pipeline/smm-pipeline.7w3.md
created: 2026-09-03
updated: 2026-09-03
decider: "Sardor"
---

# Спецификация первой production-версии

## 1. Product Scope

SMM-агент проводит один Выпуск от темы до согласованной публикации в YouTube,
Дзене и Telegram. Автор работает только в Codex и сохраняет записанное видео в
одну папку на своём ноутбуке с Windows 11. Sardor выполняет первоначальную
настройку и подключается только при неисправности, которую система не смогла
устранить автоматически.

V1 является первой эксплуатационной версией, а не одноразовым прототипом.

В scope:

- тема, план, исследование и Реестр источников;
- Основной текст, телесуфлёр, Дзен-статья и Telegram-подводка;
- применение `tone-of-voice.md`, затем `humanizer-ru`;
- 3–5 содержательных визуалов и отдельная обложка;
- обнаружение записи, локальное распознавание речи, alignment и FFmpeg-монтаж;
- три решения Автора над конкретными версиями материалов;
- подготовка и согласованная отложенная публикация на трёх площадках;
- восстановление частичного сбоя без удаления успешных публикаций и без дублей;
- аварийное уведомление Sardor в Telegram на ID `276042853`;
- Windows-native runtime на ноутбуке Автора;
- публичный GitHub-репозиторий, CI, версионные GitHub Releases и rollback.

Ограничение AI: исследование, письмо и генерация изображений выполняются только
встроенными возможностями Codex. Приложение не вызывает OpenAI API и сторонние
AI API. Локальное распознавание записи является offline media dependency: оно
не отправляет аудио внешнему провайдеру и не создаёт/редактирует содержание.

Вне scope:

- отдельная web-панель, мобильное приложение и собственный телесуфлёр;
- автоматическое управление камерой;
- синтетический ведущий или голос, музыка, b-roll, субтитры и сложный монтаж;
- несколько Авторов, роли редакции и SaaS;
- A/B-тесты, growth analytics и автоматическое изменение темы ради просмотров;
- обход CAPTCHA, MFA или правил площадок;
- автоматическое удаление публичного контента;
- использование кода Текущего агента.

Архитектура оставляет заменяемые адаптеры площадок, но не реализует абстрактную
мультиплатформенную SaaS-платформу.

### Scope Rules

- Новая площадка, роль или пользовательская поверхность требует изменения
  intent, design и spec до реализации.
- Исправление селектора Дзена или формата ответа YouTube не меняет scope.
- Ограничение внешнего сервиса не разрешает тихо пропустить обязательный
  deliverable.
- Неописанное поведение запрещено; реализация не придумывает его сама.

### Golden case cards

| ID | Вход | Ожидаемый результат |
|---|---|---|
| GC-01 | Автор задаёт тему «Как строительный контракт замораживает оборотку» и утверждает план | Codex формирует проверяемый Редакционный пакет: один Основной текст, Реестр источников, 3–5 визуалов, обложка и платформенные представления |
| GC-02 | Автор утверждает пакет и сохраняет один MP4 длительностью 7:30 в настроенную папку | Worker сам принимает файл и создаёт 1080p master: full-screen до первого anchor, затем 50/50 с непрерывной сменой 3–5 визуалов, последний остаётся до конца |
| GC-03 | Автор утверждает финал; target — четверг 14:00 Europe/Moscow | YouTube и Дзен поставлены в нативную отложку на target, ссылка отложенной статьи Дзена и ссылка YouTube заранее вставлены в Telegram payload, а локальная Windows task отправляет нативное видео и caption до 1000 символов в тот же target |
| GC-04 | После первого public результата одна площадка временно недоступна | Успешное не удаляется, повторяется только отсутствующая публикация с тем же idempotency key; дубль не создаётся |
| GC-05 | После утверждения пакета Автор меняет число в тексте | Создаётся новая версия зависимых материалов, старые approvals становятся invalidated, старый master и публикационный payload использовать нельзя |

## 2. Glossary

`CONTEXT.md` является полным словарём. Здесь приведены термины, необходимые для
чтения spec.

| Термин | Значение | Не является |
|---|---|---|
| Выпуск | Комплект контента по одной теме | отдельным роликом или релизом приложения |
| Манифест Выпуска | Каноническая запись состояния Выпуска в SQLite и её JSON-export | историей чата |
| Основной текст | Одинаковые произносимые слова телесуфлёра и статьи | Telegram-подводкой |
| Редакционный пакет | Материалы второго gate | одним DOCX-файлом |
| Gate | Явное решение Автора над перечисленными hashes | фразой AI о готовности |
| Визуальный материал | Один из 3–5 смысловых assets | обложкой или декором |
| Master | Утверждаемое 1080p видео для YouTube | Telegram-копией с меньшим bitrate |
| Telegram-копия | Тот же монтаж и звук, перекодированные под Bot API | другим содержанием ролика |
| Релиз приложения | Версионная сборка из GitHub Releases | Выпуском контента |

### Glossary Rules

- `release_id` относится к Выпуску; `app_version` — к программе.
- DOCX, preview и `release.json` являются представлениями, не источниками истины.
- AI-ответ не является артефактом до импорта и валидации.
- Private upload, draft, scheduled job и public material — разные состояния.

## 3. Universal Core And Product Templates

V1 создаётся для одного Автора. Универсальны только state machine, версии
артефактов, jobs и publisher port. Профиль `veselkov-economy-ru` задаёт TOV,
приветствие, визуальный стиль, каналы и расписание. Поддержка второго автора вне
scope; ради неё запрещено заранее добавлять multi-tenancy, организации и RBAC.

## 4. Use-Case Catalog

| ID | Сценарий | Актор | Предусловия | Успех | Побочный эффект |
|---|---|---|---|---|---|
| UC-01 | Установить и настроить | Sardor | Windows 11 | doctor подтверждает среду и доступы | config/audit |
| UC-02 | Начать Выпуск | Автор через Codex | нет другого активного Выпуска | создан Выпуск и показан план | DB rows, artifacts |
| UC-03 | Решить gate плана | Автор | plan_pending | approve или новая версия плана | approval/transition |
| UC-04 | Собрать Редакционный пакет | Codex | план approved | пакет проходит validators | artifacts/claims |
| UC-05 | Решить редакционный gate | Автор | editorial_pending | awaiting_recording или revision | approval/transition |
| UC-06 | Принять запись | Worker | awaiting_recording | один стабильный файл импортирован | media artifact/job |
| UC-07 | Смонтировать | Worker | запись и пакет валидны | master, Telegram-копия, QC | artifacts/job history |
| UC-08 | Решить финальный gate | Автор | final_pending | разрешена подготовка публикации или revision | approval/transition |
| UC-09 | Подготовить площадки | Worker | final approved | private/draft/payload готовы | remote IDs/URLs |
| UC-10 | Поставить выпуск в отложку | Worker | общий preflight успешен | YouTube и Дзен нативно запланированы, Telegram task зарегистрирована в DB и Task Scheduler | remote schedules, scheduled task |
| UC-11 | Выпустить | Worker/площадки | target наступил, повторный preflight успешен | все платформы public | receipts/events |
| UC-12 | Восстановить частичный выпуск | Worker | одна или две платформы public | опубликованы только отсутствующие | retry attempts |
| UC-13 | Показать статус | Codex | есть active release либо release_id | показано одно следующее действие | отсутствует |
| UC-14 | Изменить target | Автор | публикация ещё не началась | старая task отменена, новая создана | history |
| UC-15 | Заменить запись | Автор/Worker | final ещё не approved | новый source принят, downstream invalidated | rerender job |
| UC-16 | Сообщить об аварии | Worker | recovery исчерпан | Telegram alert и local incident созданы | notification attempts |
| UC-17 | Обновить приложение | Sardor | есть GitHub Release | установлена выбранная версия | backup/deployment record |
| UC-18 | Откатить приложение | Sardor | update smoke failed | предыдущая версия восстановлена | rollback record |
| UC-19 | Экспортировать диагностику | Sardor | incident существует | создан архив без секретов и исходного видео | audit bundle |

### Use-Case Rules

Каждая CLI-команда, Codex-процедура, background job и adapter operation указывает
UC-ID. Скрытые side effects запрещены. Новый сценарий сначала меняет этот каталог.

## 5. Canonical State Machines

### Выпуск

| From | To | Trigger | Actor | Guard |
|---|---|---|---|---|
| — | topic_received | UC-02 | Автор | непустая тема |
| topic_received | plan_pending | Codex импортировал план | Codex | plan schema valid |
| plan_pending | editorial_building | approve plan | Автор | expected_revision и plan hash совпали |
| editorial_building | editorial_pending | UC-04 | Codex | пакет полностью валиден |
| editorial_pending | awaiting_recording | approve package | Автор | утверждены hashes всего пакета |
| awaiting_recording | video_processing | UC-06 | Worker | ровно один стабильный source, 5:00–10:00 |
| video_processing | final_pending | UC-07 | Worker | render и QC успешны |
| final_pending | publication_preparing | approve final | Автор | утверждены master, Telegram-копия, cover, metadata, posts и local Dzen preview; target задан и показан, но не входит в content hash |
| publication_preparing | scheduled | UC-09/10 | Worker | YouTube и Дзен armed на target, Telegram task создана, три prepare checks успешны |
| scheduled | publishing | target/provider transition | Worker/площадки | повторный общий preflight успешен либо нативная отложка наступила |
| publishing | published | UC-11 | Worker | три publication state = public |
| publishing | recovering | первый public + ошибка другой площадки | Worker | missing set непуст |
| recovering | published | UC-12 | Worker | missing set пуст |
| publication_preparing/scheduled | delayed | общий preflight failed или target пропущен до первого public | Worker | ноль площадок public |
| publishing/recovering | needs_attention | recovery исчерпан | Worker | минимум одна площадка public или terminal failure |
| любой до publishing | revision_requested | Автор просит правку | Автор | причина и target artifact обязательны |
| revision_requested | соответствующий pending/building | rebuild/import | Codex/Worker | dependency invalidation завершён |

`published` терминален. `delayed` возвращается в `publication_preparing` только
после устранения причины и нового target; content approval сохраняется, если
hashes не изменились. `needs_attention` требует решения Sardor. Прямой `UPDATE
status` вне domain method запрещён. Каждый переход атомарно пишет transition и
outbox event.

### Approval

`pending → approved|rejected`; `approved → invalidated`. Decision immutable.
Новая версия создаёт новую pending approval, а не изменяет старую. «Окей»
принимается только если существует ровно один pending gate, показанный Автору в
текущей карточке с тем же revision.

### Recording candidate

`observed → stabilizing → accepted|rejected|ambiguous`. Accepted candidate
immutable. Новый accepted source создаётся отдельной версией. Несколько новых
кандидатов переводятся в `ambiguous` и требуют выбора, случайный выбор запрещён.

### Job

`queued → running → succeeded`; при retryable error:
`running → retry_wait → queued`; при terminal/exhausted: `→ failed`; при
invalidation до side effect: `→ cancelled`. Lease и outcome записываются в DB.

### Platform publication

`absent → preparing → prepared → armed → publishing → public`; дополнительно
`retry_wait`, `failed`, `cancelled`. `public` необратим автоматически. Для
YouTube и Дзена `armed` означает подтверждённую нативную отложку на target, для
Telegram — durable job в SQLite и Windows Task Scheduler. После возобновления
worker сверяет удалённое состояние: нативные площадки могли стать public, даже
если ноутбук был выключен.

### Notification

`queued → sending → delivered|retry_wait|failed`. Terminal notification failure
не скрывает исходный incident.

Сессия Codex не является state machine. После нового входа Codex читает status
из приложения.

## 6. Canonical Event Catalog

Events — неизменяемый audit внутри SQLite и создаются в одной транзакции с
business state. Они не маршрутизируют workflow и не имеют consumers в v1;
фоновые действия запускает только таблица `jobs`. Поэтому redelivery событий
не требуется, а redelivery jobs защищена их idempotency keys. Events — факты,
не команды и не замена основной базе.

Envelope: UUIDv7 `event_id`, `name`, `schema_version`, UTC `occurred_at`,
`aggregate_type`, `aggregate_id`, `actor`, `correlation_id`, `causation_id`,
validated `payload`.

| Event | Когда | Payload | Consumers |
|---|---|---|---|
| ReleaseStarted.v1 | создан Выпуск | topic, release_revision | audit/read model |
| ApprovalRecorded.v1 | Автор решил gate | gate, decision, artifact hashes | audit/read model |
| EditorialPackageValidated.v1 | пакет прошёл проверки | package_id, hashes | audit/read model |
| RecordingAccepted.v1 | source импортирован | artifact_id, duration | audit/read model |
| VideoValidated.v1 | QC успешен | master_id, telegram_copy_id, qc_id | audit/read model |
| PublicationPrepared.v1 | площадка готова | platform, remote_id, known_url | audit/read model |
| PublicationStarted.v1 | наступил target или началась reconciliation | target_at, trigger, armed platforms | audit/read model |
| PlatformPublished.v1 | площадка public | platform, remote_id, public_url, public_at | audit/read model |
| RecoveryExhausted.v1 | retry исчерпан | platform, error_code | audit/read model |
| DeploymentCompleted.v1 | обновление завершено | app_version, commit_sha | audit/read model |

## 7. Data Model Level 2

Единственный transactional source of truth — SQLite
`%LOCALAPPDATA%\VeselkovSmm\state\smm.sqlite3` в WAL mode. Файлы содержат bytes;
их metadata, version, dependencies и hashes находятся в DB. `release.json` —
воспроизводимый export Манифеста для диагностики и не принимается обратно как
команда.

| Table | Назначение | Обязательные поля и ограничения |
|---|---|---|
| releases | корень Выпуска | release_id UUIDv7 PK, topic, state, revision, target_at_utc, target_timezone, active, timestamps; максимум один active |
| transitions | immutable history | transition_id, release_id FK, from/to, actor, reason, correlation_id, at |
| artifact_versions | все версии файлов | artifact_id, release_id, kind, version, path, sha256, size, media_type, created_at; unique release+kind+version |
| approvals | immutable gates | approval_id, release_id, gate, decision, release_revision, artifact_set_hash, actor, reason, at |
| claims | проверяемые утверждения | claim_id, release_id, exact_text, materiality, status |
| sources | найденные источники | source_id, URL, title, publisher, checked_at, evidence_excerpt_hash |
| claim_sources | связь evidence | claim_id, source_id, relation; material claim требует supporting row |
| visual_specs | Карта визуалов | visual_id, release_id, order 1..5, type, anchor_text, artifact_id, claim_ids JSON validated |
| recording_candidates | обнаруженные файлы | candidate_id, observed path, size, mtime, fingerprint, state, reason |
| jobs | durable queue | job_id, kind, state, due_at, lease_until, attempts, retry_policy_id, idempotency_key unique, payload JSON schema-versioned |
| job_attempts | immutable executions | attempt_id, job_id, start/end, outcome, error_code, sanitized detail |
| publications | состояние площадок | release_id+platform unique, state, payload_hash, remote_id nullable, known_url nullable, public_at nullable |
| notifications | аварийные сообщения | notification_id, incident_id, recipient, state, suppression_key unique, attempts |
| domain_events | audit events | envelope fields, payload JSON; append-only |
| command_results | idempotency CLI | command_id unique, actor, command, request_hash, outcome JSON, at |
| deployments | версии приложения | deployment_id, app_version, commit_sha, installed_at, previous_version, outcome |

SQLite foreign keys включены. JSON разрешён только с `schema_version` и
Pydantic validation. Approvals, transitions, attempts, events и deployments не
удаляются физически. OAuth tokens, cookies и browser profile в DB не хранятся.
Каждая успешная mutation увеличивает `releases.revision` ровно на один.
`artifact_set_hash` — SHA-256 от canonical JSON массива пар
`[{"artifact_id":...,"sha256":...}]`, отсортированного по `artifact_id`.

## 8. API Contract Baseline

V1 не открывает HTTP API. Локальный контракт — `smmctl.exe`; stdout всегда один
JSON-document UTF-8, diagnostics идут в stderr. Pydantic models являются source
of truth и экспортируются как JSON Schema в `docs/generated/contracts/`.
Business rules живут в application/domain, не в CLI parser и не в Codex skill.

| Command | Actor | UC | Idempotency | Результат |
|---|---|---|---|---|
| `smmctl setup validate` | Sardor | UC-01 | нет | capability report |
| `smmctl setup login dzen` | Sardor | UC-01 | нет | headful login + profile check |
| `smmctl release start --command-id --topic-file` | Автор/Codex | UC-02 | обязательно | release_id, revision, next_action |
| `smmctl release status [--release-id]` | Автор/Sardor | UC-13 | нет | status card DTO |
| `smmctl release import-plan --command-id --expected-revision --file` | Codex | UC-02/03 | обязательно | artifact IDs, pending gate |
| `smmctl release import-editorial --command-id --expected-revision --bundle` | Codex | UC-04 | обязательно | validation report, pending gate |
| `smmctl release decide --command-id --expected-revision --gate --decision` | Автор/Codex | UC-03/05/08 | обязательно | transition, next_action |
| `smmctl release revise --command-id --expected-revision --target --reason` | Автор/Codex | UC-03/05/15 | обязательно | invalidated artifacts |
| `smmctl release set-target --command-id --expected-revision --at` | Автор/Codex | UC-14 | обязательно | UTC target, next_action |
| `smmctl worker install|start|stop|status` | Sardor | UC-01/17 | по operation | Windows task/service status |
| `smmctl diagnostics export --incident-id` | Sardor | UC-19 | нет | sanitized archive path |

Mutation требует `command_id`, `expected_revision` и actor context. Повтор того
же command_id с тем же request hash возвращает прежний result; с другим hash —
`IDEMPOTENCY_CONFLICT`. Списки используют `limit` 1–100 и opaque cursor.

Единая ошибка:

```json
{
  "error": {
    "code": "STATE_CONFLICT",
    "message": "Выпуск уже перешёл на другой этап.",
    "hint": "Обновите статус и повторите действие для показанной версии.",
    "requestId": "019...",
    "details": {"expectedRevision": 7, "actualRevision": 8}
  }
}
```

Breaking DTO создаёт новую major CLI contract version. Database rows никогда не
выдаются наружу напрямую.

Editorial bundle import содержит явные link slots `youtube_url` и `dzen_url`.
Автор утверждает текст CTA и назначение этих slots на финальном gate. Замена
slot на URL, полученный тем же Выпуском при prepare, является механической
операцией и не инвалидирует approval; любое другое изменение текста инвалидирует.

| Error code | Когда используется |
|---|---|
| `STATE_CONFLICT` | state/revision уже изменились |
| `AMBIGUOUS_GATE` | короткое решение не указывает единственный gate |
| `VALIDATION_FAILED` | schema или deterministic invariant нарушен |
| `ARTIFACT_HASH_MISMATCH` | bytes не совпали с утверждённой версией |
| `AMBIGUOUS_RECORDING` | найдено более одного кандидата |
| `DURATION_OUT_OF_RANGE` | запись короче 5:00 или длиннее 10:00 |
| `ALIGNMENT_LOW_CONFIDENCE` | anchors нельзя надёжно расположить |
| `TELEGRAM_VIDEO_TOO_LARGE` | readable encode не помещается в 49 MB |
| `PROVIDER_AUTH_REQUIRED` | OAuth/session/token требует Sardor |
| `PROVIDER_CONTRACT_CHANGED` | API/DOM не соответствует adapter contract |
| `PROVIDER_TEMPORARY_FAILURE` | network, 429 или 5xx допускает retry |
| `MISSED_PUBLICATION_TARGET` | target прошёл до первого public side effect |
| `IDEMPOTENCY_CONFLICT` | command_id повторён с другим payload |
| `DEPLOYMENT_NOT_READY` | installation/wake/backup smoke не пройден |

## 9. Canonical Architecture

Архитектура — Windows-native modular monolith:

- Codex project skill: единственный разговорный интерфейс и исполнитель
  интеллектуальной редакционной стадии;
- `smmctl.exe`: узкий JSON CLI;
- `smm-worker.exe`: file watcher, media pipeline, durable jobs и publishing;
- SQLite: business state, queue и audit events;
- release directory: immutable artifact bytes и previews;
- adapters: filesystem, FFmpeg/ffprobe, offline ASR, Playwright Dzen, YouTube
  Data API, Telegram Bot API, Windows Task Scheduler и Credential Manager.

Development stack: Python 3.12, Pydantic 2, Typer, SQLite, watchdog, HTTPX,
Playwright, FFmpeg/ffprobe, локальный Whisper-compatible runtime, Google API
client, pytest, Ruff, mypy и PyInstaller one-folder distribution.

Запрещены в baseline: Docker/WSL2 как обязательная production-зависимость,
микросервисы, Redis/Celery, HTTP server, generic `utils/common/helpers`, прямой
provider call из Codex skill и business state в prompt/chat history.

## 10. Source-Of-Truth Rules

| Предмет | Source of truth |
|---|---|
| цель и границы | accepted intent |
| поведение | accepted spec и ADR |
| термины | `CONTEXT.md` |
| runtime business state | SQLite migrations + rows |
| artifact bytes | versioned release files, связанные DB hash |
| schemas | `src/smm_agent/contracts/` |
| runtime settings | validated TOML; secrets отдельно |
| Codex interaction | project skill + CLI DTO |
| delivery | Git commit, tag, GitHub Release manifest |

Критическая логика не может существовать только в prompt, Markdown-preview,
DOCX, UI площадки, CI YAML, SQL read query или ручном SOP.

## 11. Repo Layout

```text
.
├── .agents/skills/{veselkov-smm,humanizer-ru}/
├── .github/workflows/{ci,release}.yml
├── config/smm-agent.example.toml
├── docs/{adr,research,work,generated,runbooks}/
├── migrations/
├── src/smm_agent/
│   ├── contracts/
│   ├── domain/{release,editorial,video,publication}/
│   ├── application/<use_case>/
│   ├── adapters/{codex_import,files,media,publishing,secrets,windows}/
│   ├── platform/{config,db,jobs,logging}/
│   ├── cli/
│   └── worker/
├── tests/{unit,contract,integration,e2e,evals,fixtures}/
├── tools/architecture/
├── pyproject.toml
└── uv.lock
```

Domain импортирует только standard library и contracts. Application импортирует
domain/ports. Adapters импортируют ports, но domain не импортирует adapters.
Public boundary контекста — `api.py` и `ports.py`. Production file target — до
400 строк; более 600 требует зафиксированного обоснования. Import graph, cycles,
forbidden folders и direct network/subprocess calls проверяются CI.

## 12. AI-Agent Delivery Rules

Перед изменением агент читает `AGENTS.md`, `CONTEXT.md`, accepted spec и нужный
contract; указывает UC-ID. Изменение поведения начинается с failing test/eval.
Изменение минимально. Mandatory setting не получает скрытый default. Внешний
side effect идёт через port, job и audit. Agent-written code проходит отдельный
review; окончательный риск принимает человек. Test commands запускаются только
по правилам `AGENTS.md` через `codex-test-guard` и никогда параллельно.

## 13. Bounded Contexts

| Context | Владеет | Не владеет |
|---|---|---|
| Release | lifecycle, revisions, approvals, dependencies | текстом и media bytes |
| Editorial | claims, text, visuals, metadata schemas | записью и внешней публикацией |
| Video | source, ASR, alignment, timeline, encodes, QC | смысловой редактурой |
| Publication | prepare/publish/recovery per platform | изменением утверждённого контента |
| Interaction | Codex cards и command mapping | business state |
| Platform | DB, jobs, config, logging, Windows integration | доменными решениями |
| Delivery | app build, version, install, rollback | Выпусками контента |

## 14. Canonical Domain Model

| Entity | Назначение | Invariant |
|---|---|---|
| Release | корень Выпуска | state меняется только domain transition |
| ArtifactVersion | неизменяемая версия | hash соответствует bytes |
| Approval | решение Автора | exact revision + artifact set hash |
| Claim | существенный факт | supported, conflict или явно opinion |
| VisualSpec | visual + anchor | ровно 3–5, порядок и anchor уникальны |
| Recording | принятый source | оригинал не изменяется |
| VideoEncode | master/Telegram-копия | одинаковое содержание и timeline |
| Publication | состояние площадки | unique release+platform, public необратим |
| Job | повторяемая операция | lease + idempotency key |
| Incident | неисправность | причина и безопасное следующее действие |

AI output становится ArtifactVersion только после schema/deterministic checks.
History физически не удаляется. Analytics — derived read model.

Правила invalidation фиксированы в domain layer и покрыты table-driven tests:

- новая версия плана инвалидирует весь Редакционный пакет, связь с записью,
  encodes и publication payloads;
- новая версия Основного текста или визуалов инвалидирует teleprompter, Dzen,
  DOCX, связь с записью, encodes и publication payloads;
- новая cover/metadata/Telegram-caption версия инвалидирует только затронутые
  previews и publication payloads;
- новая запись инвалидирует alignment, encodes и publication payloads;
- новый master/Telegram encode инвалидирует final approval и публикацию.

## 15. Multitenancy Or Ownership Model

Multi-tenancy отсутствует. Владельцем данных является Windows-профиль Сергея
Николаевича; Sardor получает локальный setup/diagnostic доступ с его согласия.
Actor labels `author`, `operator`, `worker`, `codex` нужны для audit, но не
являются сетевой RBAC. Любой, кто получил доступ к Windows account и Codex
project, фактически может действовать как Автор; UI не скрывает это ограничение.

## 16. Data Rules And Import Rules

ID — UUIDv7; hashes — SHA-256; DB time — UTC RFC3339; target хранит UTC и
`Europe/Moscow`. Table names — plural snake_case. Foreign keys и busy timeout
включены. Migration имеет monotonic version и проверяется на копии предыдущей DB.

Video ingest двухфазный: observe/stabilize/probe → atomic accept. Stable означает
неизменные size и mtime в течение 30 секунд и успешное открытие file handle без
sharing violation. Preview возвращает имя, размер, duration, streams и errors.
Оригинал копируется в release directory, не перемещается и не удаляется.

## 17. Auth, Sessions And CSRF

HTTP/browser session приложения отсутствует, поэтому CSRF неприменим. Локальная
граница доступа — Windows account и NTFS ACL. Codex approvals являются
семантическими решениями Автора, а не криптографической подписью.

YouTube OAuth проходит один раз при setup; refresh token хранится через Windows
Credential Manager. Dzen login выполняется Sardor в headful Playwright Chromium,
включая MFA/CAPTCHA вручную; session profile хранится локально под ACL. Telegram
bot token хранится в Credential Manager. При истечении внешней сессии система
не обходит login, а создаёт incident.

## 18. RBAC Matrix

Полноценный RBAC вне scope. Применяются локальные command guards:

| Actor label | Допустимые команды | Ограничение |
|---|---|---|
| author | start, status, decide, revise, set-target | только через текущий Codex project |
| codex | import plan/editorial, вызвать author command после явной фразы | не может сам approve |
| worker | leased jobs и внутренние transitions | не может создавать Approval |
| operator | setup, credentials, diagnostics, deploy/rollback | не утверждает содержание |

Deny by default: не указанная пара actor+command запрещена и auditится.

## 19. Channel And Integration Model

| Channel | Binding | Operation ID | Confirmation | Retry/special rule |
|---|---|---|---|---|
| Codex | project directory + skill | command_id | CLI JSON result | chat history не доверяется |
| Recording inbox | подтверждённый Windows path | file fingerprint | accepted artifact | только новые stable files после gate |
| YouTube | channel OAuth | video_id | API state/public URL | private prepare, native `publishAt` |
| Dzen | local browser profile | scheduled material URL/DOM identity | preview screenshot + schedule/public state | Playwright page objects, no CAPTCHA bypass |
| Telegram channel | bot admin rights | message_id | Bot API response | native video + caption one message |
| Telegram alert | bot + user ID 276042853 | notification_id | Bot API response | local incident remains if bot unavailable |
| GitHub | public repo `t0uchY233/veselkov-smm-agent` | commit/tag/run | checks + release manifest | no secrets, runtime или unpublished release content |

YouTube private video получает стабильный URL при prepare. При создании
отложенной публикации Дзен уже выдаёт рабочую ссылку на будущий материал. Dzen
adapter сохраняет её, подтверждает соответствие нужному draft и target, после
чего formatter подставляет ссылки Дзена и YouTube в заранее утверждённые места
Telegram caption. Отсутствующая или изменившаяся ссылка блокирует постановку
Telegram в расписание. Ждать public-состояния Дзена для сборки caption не нужно.

Telegram cloud Bot API принимает video upload до 50 MB, local Bot API — до
2000 MB. V1 сначала создаёт `telegram-video.mp4` размером не более 49,000,000
bytes с тем же монтажом; local Bot API Server не входит в baseline. Если
readability gate невозможно пройти в 49 MB, Выпуск блокируется и требует
отдельного design change, а не скрытой потери качества. Ограничения сверяются с
[Telegram Bot Features](https://core.telegram.org/bots/features) при setup.

YouTube scheduling и private/public правила сверяются с
[YouTube Data API](https://developers.google.com/youtube/v3/docs/videos).

Temporary provider errors: network timeout, HTTP 429 и 5xx; retry через 30
секунд, 2, 5 и 15 минут. Auth/permission errors, invalid payload и Dzen DOM
contract mismatch являются permanent и сразу создают incident. Retry всегда
сначала вызывает `status`, затем действует только при отсутствии public result.

## 20. Frontend Runtime And UI Flow Specs

Единственная поверхность Автора — Codex. Каждая карточка содержит четыре блока:
«Готово», «Проверьте», «Сейчас нужно», «Что дальше».

| Flow | Entry | Happy path | Обязательные альтернативы |
|---|---|---|---|
| UI-01 | Автор пишет тему | показан план и одна просьба approve/revise | processing, conflict, failure |
| UI-02 | Автор пишет «Окей» | утверждён один показанный pending gate | ambiguous gate, stale revision |
| UI-03 | Автор даёт замечание | показана новая версия | invalid target, unsupported change |
| UI-04 | пакет approved | показаны точный inbox path и ожидание | watcher down, ambiguous recording |
| UI-05 | master готов | ссылки на video/package/metadata и один final gate | QC failure, duration violation |
| UI-06 | final approved | показаны target и prepare status | platform not ready, delayed |
| UI-07 | status request | текущий state и одно действие | нет active release, needs_attention |

Codex не утверждает от имени Автора, не перезаписывает stale state и не обещает
успех до CLI result.

## 21. Main Business Lifecycle

Внутренние состояния определены только в разделе 5. Пользовательские labels:
«готовим план», «утвердите план», «готовим материалы», «утвердите материалы»,
«сохраните запись», «монтируем», «утвердите финал», «готовим площадки»,
«запланировано», «публикуем», «опубликовано», «выпуск задержан», «нужна помощь».

Revision сохраняет предыдущие artifacts и invalidates downstream. Published
Выпуск не переоткрывается; продолжение темы — новый Выпуск. Новый Выпуск при
активном предыдущем требует явного решения, смешивать их запрещено. Отмена
публичного контента вне scope.

При создании Выпуска default target — ближайший будущий четверг 14:00 в
`Europe/Moscow`. Если тема введена после этого момента в четверг, выбирается
следующий четверг. Автор видит target в каждой gate-card и может изменить его до
начала `publishing`. Изменение только target не инвалидирует content approval,
но отменяет старые Windows tasks и создаёт новые с audit transition.

## 22. Assignment, Routing, SLA Or Workload Rules

Один worker выполняет максимум один CPU-heavy media job и до трёх network jobs.
Job выбирается по due_at и priority; AI не выбирает очередь. Lease — 60 секунд,
heartbeat — 20 секунд. Publication preflight job создаётся на T−30 минут, а
Telegram send/reconciliation job — на T. Windows Task Scheduler обе задачи
получает с `WakeToRun=true`.

Если T−30 preflight неуспешен до наступления target, worker отменяет нативные
отложки YouTube и Дзена, отменяет Telegram task и переводит Выпуск в delayed.
Если ноутбук был выключен и нативная отложка уже сработала, при следующем
запуске reconciliation обнаруживает частичный выпуск и отправляет только
отсутствующий Telegram-пост без дубля.

## 23. Dedupe, Merge And Conflict Rules

- command duplicate: одинаковый command_id и request hash;
- recording duplicate: SHA-256 + size;
- publication duplicate: release_id + platform;
- notification duplicate: incident_id + channel;
- artifact duplicate: kind + sha256 в одном Выпуске.

Несколько recordings, stale revision, изменённый approved hash и несовпадающий
remote payload блокируют действие. AI может сообщить о похожести, но не merge
объекты. Public publication автоматически не удаляется и не заменяется.

## 24. AI Operating Model

Codex — интерактивный исполнитель, не скрытый background service. Официальная
документация описывает pattern «создать CLI, который может использовать Codex»;
приложение следует именно этому направлению, а не пытается выдать UI-tools за
стабильный программный API: [Codex use cases](https://developers.openai.com/codex/use-cases).

| Сценарий | Input | Validated output | Low confidence/failure | Защищённое |
|---|---|---|---|---|
| plan | topic, TOV context | plan JSON | показать неопределённость, не открыть gate | topic |
| research | approved plan, web sources | claims + sources JSON | conflict/unsupported marker | source evidence |
| main text | claims, TOV | text + claim map | validation report | факты, числа, цитаты |
| Humanize | protected text spans | edited text + lint report | исправить только findings | факты, ссылки, greeting |
| visuals | text anchors + claims | 3–5 specs/assets | regenerate/block | claim values, identity approval |
| cover | topic + local portrait refs | 1280×720 asset | regenerate/block | узнаваемость решает Автор |
| intent mapping | phrase + status DTO | proposed CLI command | задать одно уточнение | business state |

Порядок текста: evidence draft → TOV → Humanize audit → точечная правка → lint
→ platform formatting. Formatter добавляет ссылки, стрелки и emoji после lint.
Skill version, объявленные типы использованных Codex tools и input/output hashes
входят в eval record; model/session metadata записываются только если Codex
возвращает их явно. Секреты и полные browser sessions не логируются.

Редакционные инварианты:

- `teleprompter.txt` равен строке `Здравствуйте, друзья.`, пустой строке и
  точному Основному тексту; иных непроизносимых вставок нет;
- произносимые слова `dzen.md` равны Основному тексту; приветствие в Дзен не
  входит, оформление, captions, links и visual slots являются metadata;
- Telegram caption самостоятельный, содержит title, полезное содержание, CTA с
  двумя link slots и ровно три reaction rows; после подстановки entities ≤1000
  видимых Unicode scalar values и одновременно ≤1000 UTF-16 code units;
- комплект содержит ровно 3–5 visual specs, каждый имеет уникальный anchor в
  порядке Основного текста и минимум одну содержательную функцию;
- каждый visual master имеет canvas 1080×1080 и safe layout для правой половины
  Full HD video и размещения в Дзене; в article и video используется один и тот
  же asset hash;
- числа и подписи визуалов рендерятся кодом поверх изображения, а не доверяются
  генеративной модели; каждый факт ссылается на claim_id;
- cover 1280×720 не входит в число визуалов; генеративно изменённый портрет
  допускается только при финальном approval Автора;
- YouTube metadata включает title, description, chapters, tags и cover;
- Dzen metadata включает title, cover, visual captions и source links;
- DOCX — производный preview, который пересобирается из утверждаемых artifacts.

Word budget не является жёстким числом. Provisional speaking rate 86 слов/мин
получен из одного опубликованного ролика и записан в research; draft целится в
7,5 минуты. Estimated duration = число произносимых слов / текущую медианную
скорость Автора. Estimate вне 5–10 минут блокирует editorial gate; фактическая
длительность записи остаётся окончательным hard gate.

Offline ASR принимает только local audio и выдаёт words/timestamps/confidence.
Это output кандидата: timeline становится state только после monotonic anchor
validation. Порог не фиксируется «из головы»: implementation plan включает
calibration dataset; до принятого порога auto-render запрещён.

## 25. Files And Media

Data root выбирается при setup; предлагаемый путь
`%LOCALAPPDATA%\VeselkovSmm`. Recording inbox выбирает Sardor вместе с Автором;
весь диск не сканируется.

Allowed input: MP4, MOV, MKV с одним читаемым video и минимум одним audio stream.
После удаления только технической тишины duration обязана быть от 300.000 до
600.000 секунд включительно. Голос не ускоряется. Source копируется и получает
read-only attribute; оригинал остаётся на месте.

Master: MP4, H.264, AAC, 1920×1080, constant frame rate, web-compatible pixel
format. До первого anchor ведущий full-screen. Далее ведущий занимает левую
половину 960×1080, visual — правую; visual N действует до N+1, последний — до
последнего frame.

Telegram-копия создаётся из master без изменения cuts, timeline и audio content;
допустимо только масштабирование и перекодирование. Требования: H.264/AAC,
faststart, размер ≤49,000,000 bytes. QC проверяет decode, duration, audio,
resolution, transition frames, непрерывность правой панели и читаемость
контрольных надписей на каждом визуале.

Временный файл пишется рядом, проверяется и атомарно переименовывается. Path
traversal и symlink/reparse-point escape запрещены. Ни один исходник или
публичный material автоматически не удаляется в v1.

## 26. Notifications And Campaigns

Контентный Telegram-post является deliverable Выпуска. Аварийное уведомление —
отдельный технический объект. Alert отправляется Sardor без quiet hours после
terminal recovery failure и содержит release_id, platform, error code, время и
одно безопасное действие.

Retry notification: 1, 5 и 15 минут. Если Telegram недоступен, создаются local
incident JSON и Windows toast при следующем interactive login. Это не независимый
сетевой канал: при выключенном ноутбуке alert физически невозможен до запуска.
Массовых campaigns в v1 нет.

## 27. Analytics And Read Models

V1 содержит только operational `release_summary` view из SQLite: время стадий,
duration, visual count, retry count, public_at per platform и Sardor intervention
flag. Формулы: cycle time = published_at − created_at; publication drift =
max(public_at) − min(public_at); routine Sardor time считается нулём только для
Выпуска без operator action между start и published.

Read model обновляется транзакционно или пересобирается из основной DB. Если он
не обновился, status показывает stale marker и читает canonical rows. Growth
metrics и platform analytics вне scope.

Business acceptance одного штатного Выпуска: `operator_intervention=false`,
3–5 визуалов, master duration 5–10 минут, Telegram caption ≤1000 по обоим
счётчикам, три public URLs и отсутствие duplicate remote IDs. Целевая, но не
блокирующая метрика: время Автора на управление до 30 минут без учёта записи.

## 28. Observability

Structured log fields: timestamp UTC, level, app_version, commit_sha,
request_id, correlation_id, actor, module, UC-ID, release_id, job_id, platform,
error_code. Логируются transitions, approvals metadata без текста, job leases,
subprocess argv без secrets, exit codes, provider method/status, retries,
deployment и rollback.

Health signals: worker heartbeat старше 2 минут, overdue preflight/target job,
provider auth failure, recording ambiguity, FFmpeg/ASR failure, DB integrity,
disk admission failure и notification failure. `smmctl diagnostics export`
удаляет tokens, cookies, полный article/video и абсолютное имя Windows user.

## 29. Security, Privacy And Compliance

Secrets хранятся в Windows Credential Manager через DPAPI. Dzen browser profile
и data root защищаются NTFS ACL текущего Windows account и Sardor setup account.
Tokens, cookies, source/master video, generated portraits, runtime DB и real
config запрещены в Git, GitHub Actions artifacts и diagnostics.

Public repo допускает только явно предназначенные для публикации документы,
включая TOV и ссылки на каналы, examples без credentials и synthetic fixtures.
Secret scanning выполняется до
commit и в CI. OAuth scopes минимальны. Logs редактируют Authorization headers,
query tokens, cookies и file contents.

V1 ничего не удаляет по retention автоматически. Installer сообщает статус
Windows Device Encryption/BitLocker, но не утверждает, что шифрование включено.
Если оно выключено, production readiness показывает предупреждение и требует
явного решения Sardor; скрывать риск запрещено.

## 30. Runtime, Staging And Deploy

Production host — Windows 11 laptop Сергея Николаевича. Baseline не требует
Docker или WSL2. GitHub Release содержит Windows x64 one-folder bundle:
`smmctl.exe`, `smm-worker.exe`, migration files, PowerShell installer/updater,
SBOM, `release-manifest.json` и `SHA256SUMS`. Manifest фиксирует версии,
официальные download URLs и SHA-256 для FFmpeg, Playwright Chromium и offline
ASR assets; installer скачивает только эти версии и прекращает установку при
несовпадении checksum. Их лицензии должны разрешать выбранный способ установки
до release.

Windows Task Scheduler:

- worker task стартует at logon и перезапускается после failure;
- T−30 preflight и T Telegram/reconciliation tasks создаются для каждого Выпуска;
- tasks разрешены при logged-off user и используют `WakeToRun=true`;
- deployment acceptance обязана реально проверить wake из sleep на ноутбуке;
- powered-off laptop не может быть разбужен: YouTube и Дзен всё равно могут
  опубликоваться по нативному расписанию, а Telegram будет восстановлен после
  запуска ноутбука; этот частичный сценарий создаёт incident.

Microsoft подтверждает time triggers и `WakeToRun` в
[Task Scheduler documentation](https://learn.microsoft.com/en-us/windows/win32/taskschd/task-triggers)
и [WakeToRun property](https://learn.microsoft.com/en-us/windows/win32/taskschd/tasksettings-waketorun).

GitHub delivery contract:

1. Repository: public `t0uchY233/veselkov-smm-agent`.
2. Секреты, runtime DB, browser profile, diagnostics, recordings и unpublished
   artifacts Выпуска никогда не коммитятся. Имя Автора, ссылки на публичные
   каналы, TOV и принятые продуктовые документы публичны по решению владельца.
3. Каждое изменение идёт через branch и PR.
4. До появления второго collaborator GitHub не требует approval count, потому
   что автор PR не может одобрить его сам; вместо этого PR содержит независимый
   review artifact и финальное решение Sardor. После появления reviewer account
   включается один required approval. GitHub прямо указывает, что автор PR не
   может одобрить собственный PR: [Approving a pull request](https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/approving-a-pull-request-with-required-reviews).
5. После реализации CI обязательны checks: lint, type, architecture, tests,
   migrations, secret scan и Windows package smoke.
6. SemVer tag `vX.Y.Z` создаётся только из protected `main`; GitHub Actions
   формирует immutable Release assets, SBOM и checksums.
7. Production обновляет Sardor явной командой на конкретную версию; push не
   обновляет ноутбук автоматически.
8. Deploy: stop worker → backup DB/config/profile metadata → verify checksums →
   install side-by-side → migrate DB copy → start → doctor → smoke → commit
   deployment record.
9. Rollback: stop new version → restore previous binaries and pre-migration DB
   backup → start → doctor. Content files не удаляются.
10. Manual hotfix установленной копии запрещён; исправление получает новый tag.

Codex открывает stable workspace `%USERPROFILE%\VeselkovSmmAgent`, установленный
из того же Git tag, что и binaries. Updater меняет workspace и app side-by-side,
а затем атомарно переключает current version. `setup validate` блокирует
mutation, если workspace commit, skill version и app commit расходятся; status и
rollback при этом остаются доступны.

## 31. Environment And Config Contract

`config.toml` versioned schema, но real file вне Git.

| Group | Mandatory examples | При отсутствии | Degraded mode |
|---|---|---|---|
| runtime | data_root, timezone | startup fail | нет |
| files | recording_inbox, portrait_reference_dir | media capability unavailable | status/editorial доступны |
| media | ffmpeg_path, asr_asset, crop_profile | render blocked | status/editorial доступны |
| schedule | wake_task identity, preflight offset | scheduling blocked | до final gate допустимо |
| YouTube | channel ID, credential ref | publication blocked | editorial/video доступны |
| Dzen | channel URL, browser_profile path | publication blocked | editorial/video доступны |
| Telegram | channel ID, bot credential ref, alert ID | publication/alert blocked | editorial/video доступны |
| Codex | required skill versions | editorial import blocked | status/worker доступны |
| observability | log path, retention | startup fail | нет |
| delivery | app_version, commit_sha | startup fail | нет |
| backup | backup_root | production update blocked | обычный Выпуск допустим с warning |

Installer предлагает пути, но требует явного подтверждения. Missing mandatory
credential/path не получает guessed default. `setup validate` и каждый process
startup выполняют schema validation.

## 32. Backup And Disaster Recovery

Перед каждым update через SQLite Online Backup API создаются консистентный DB
backup, config snapshot без secret values и
browser-profile metadata. Ежедневно при изменениях копируются DB и artifacts
активного Выпуска. Backup root выбирает Sardor; production readiness предупреждает,
если он находится на том же физическом диске.

Минимальное хранение: три deployment backups и последние четыре weekly snapshots.
Restore order: app version → config → DB → artifact hash verification → credential
rebinding → Dzen session check → publication reconciliation → doctor. Restore
неполон, если потеряны approvals, artifact hashes или remote IDs. Restore drill
обязателен перед первым production и затем после изменения migration/installer.

## 33. Testing Strategy

Все тесты строго последовательно запускаются через
`/root/.local/bin/codex-test-guard`; targeted timeout 10m, full suite 20m, если
репозиторий не задаст иной timeout.

| Risk | Tests |
|---|---|
| пропуск gate/stale approval | state-machine unit + property tests |
| рассинхронизация DB/jobs/events | SQLite transaction, crash/restart, migration tests |
| duplicate public post | adapter contract + idempotent redelivery tests |
| 5–10 минут и layout | ffprobe fixtures, frame snapshots на anchors, full decode |
| плохой Telegram encode | size, codec, decode, visual readability fixtures |
| искажение фактов/TOV | claim-protection tests + Humanize evals |
| неправильное «Окей» | Codex interaction evals с ambiguous/stale states |
| Dzen UI change | staging page-object smoke with screenshots |
| Windows sleep/missed target | Task Scheduler integration on Windows runner + real laptop acceptance |
| unsafe public repo | secret scan and forbidden-path tests |
| broken update | clean install, previous-version migration, rollback smoke |

Critical E2E соответствуют GC-01…GC-05. Provider adapters имеют fake contract
tests; real-channel smoke никогда не публикует без test account/draft mode.

Change gate:

- domain: unit + affected state/E2E;
- schema/migration: upgrade from previous release + rollback restore;
- Codex prompt/skill: frozen eval corpus;
- provider adapter: contract + staging capability smoke;
- media: golden video + transition screenshots + full decode;
- deployment: Windows clean install/update/rollback.

Release требует lint, type, architecture, tests, evals, secret scan, Windows
package smoke, независимый review evidence и человеческое принятие риска.

## 34. Non-Functional Requirements

- `release status` p95 ≤2 seconds on the production laptop.
- Recording detection ≤90 seconds after file close.
- A 10-minute source renders in ≤30 minutes on the accepted laptop; измеряется
  deployment benchmark, иначе production not ready.
- Worker starts ≤60 seconds after Windows logon or scheduled wake.
- T−30 preflight starts within ±60 seconds when laptop is in supported sleep.
- Telegram target job starts within ±30 seconds when the laptop is available.
- Visible publication drift target ≤5 minutes; большее значение создаёт incident.
- Disk admission requires free space ≥3× source size + 2 GiB; это вычисляемое
  правило, не фиксированный размер диска.
- External outage не повреждает approved artifacts и не создаёт duplicate.
- Масштабирование ограничено одним Автором, одним laptop и одним media job.

## 35. Feature Delivery Gates

Definition of Ready: accepted spec; UC-ID и bounded context; actor, trigger,
happy/error paths; schemas/data changes; side effects/idempotency; tests;
file-specific `plan.md`.

Definition of Done: guarded tests и architecture checks проходят; observability
есть; errors имеют code/message/hint; migration, Windows packaging, rollout и
rollback проверены; spec/ADR обновлены при изменении решения; review evidence
сохранён; Sardor принимает gate.

## 36. Canonical Implementation Decisions

| Выбрано | Отклонено | Почему и следствие |
|---|---|---|
| Codex UI + deterministic local app | prompt-only orchestration | Codex создаёт content, код охраняет state/gates |
| Один SQLite source of truth | JSON+JSONL+SQLite state | atomic state/jobs/outbox и проще recovery |
| Windows-native worker | обязательные Docker/WSL2 | меньше startup/file/browser failures на ноутбуке |
| Native YouTube/Dzen schedules + local Telegram task | полностью локальный publish coordinator | ссылки известны заранее, а две площадки не зависят от бодрствования ноутбука; Telegram остаётся локальным риском |
| YouTube private upload + `publishAt` | локальный public transition | URL известен, content остаётся непубличным до target |
| Telegram ≤49 MB derivative | обязательный Local Bot API Server | меньше deployment complexity при том же монтаже |
| Playwright Dzen adapter | выдуманный официальный API | v1 возможен только после real capability smoke |
| Codex built-ins for content | сторонние AI API | соблюдает выбранный пользователем AI-контур |
| Offline local ASR | внешняя transcription API | запись не отправляется AI provider |
| Public GitHub + Release bundles | production checkout/hotfix | воспроизводимые версии и rollback |
| PR+CI без required approval до второго reviewer | один approval в single-user repo | merge не оказывается технически невозможным |

## 37. Deferred Alignment Items

Это не скрытые решения. Каждый пункт блокирует указанный rollout gate.

| Item | Решить | Фиксированная граница | До решения запрещено |
|---|---|---|---|
| Windows recording inbox path и crop profile | setup на ноутбуке | одна выделенная папка, Автор слева | scan диска и production render |
| Offline ASR model/threshold | media tracer bullet + calibration fixtures | local-only, monotonic anchors | auto-render при low confidence |
| Dzen selectors и scheduled-link contract | первый Dzen tracer bullet на real account | отложка обязана вернуть стабильную ссылку до target; no bypass, adapter isolated | обещать end-to-end publication readiness |
| Telegram readability при 49 MB | media benchmark на 10-minute fixture | тот же edit/audio content | скрыто ухудшать визуалы или превышать cloud limit |
| Wake-from-sleep capability | deployment smoke на ноутбуке | Telegram остаётся локальной scheduled task | production scheduling |
| Backup root | setup Sardor | update backup обязателен | production update |
| Второй GitHub reviewer account | когда будет добавлен collaborator | PR, CI и review evidence уже обязательны | включать required approval, блокирующий owner PR |
| Реальные credentials | staging/production setup | Credential Manager, least privilege | хранить их в Git или config.toml |

### Assumptions

- Windows 11 laptop имеет x64 CPU и может запускать Task Scheduler jobs.
- Автор сохраняет запись в выбранную папку и не выключает ноутбук намеренно до
  завершения подготовки; при выключении около target возможен частичный выпуск
  YouTube/Дзен без Telegram, который затем восстанавливается без дублей.
- Bot имеет право публиковать в Telegram channel, YouTube OAuth связан с нужным
  каналом, Sardor может один раз войти в Dzen.

### Open Questions

Критических продуктовых вопросов для начала plan нет. Spec 0.2.1 принят Sardor
3 сентября 2026 года с уточнением контракта отложенной ссылки Дзена. Deferred items выше
решаются отдельными tracer bullets до объявления production-ready.

## 38. Final Rule

Если быстрая реализация нарушает эту спецификацию, исправляется реализация, а не
спецификация размывается ради удобства.

## Spec acceptance

- Decision: accepted
- Date: 2026-09-03
- Decider: Sardor
- Notes: версия 0.2.1 принята после hardening-review и доменного уточнения:
  отложенная публикация Дзена заранее предоставляет рабочую ссылку, которая
  подставляется в Telegram caption до постановки Telegram в расписание.
