---
title: "Спецификация SMM-агента Сергея Веселкова"
status: draft
version: 0.1.0
design: smm-pipeline/smm-pipeline.7w3.md
created: 2026-09-03
decider: "Sardor"
---

# Спецификация первой production-версии

## 1. Product Scope

SMM-агент превращает тему Автора в согласованный Выпуск и после трёх
человеческих gates публикует его в YouTube, Дзене и Telegram. Сергей Николаевич
работает только через Codex и сохраняет запись в выделенную локальную папку.

Текущий этап — первая production-версия, а не демонстрационный прототип.

В scope:

- один Автор и один комплект его каналов;
- тема, план, исследование, Основной текст, телесуфлёр и платформенные тексты;
- Tone of voice, Humanize, 3–5 визуалов, одна обложка и DOCX;
- локальный ingest записи, alignment, FFmpeg-монтаж и QC;
- три gates Автора и неизменяемая история решений;
- подготовка, отложенная публикация и восстановление без дублей;
- аварийное уведомление Sardor в Telegram;
- GitHub-версионирование, CI, GitHub Releases, установка и rollback;
- production на одном ноутбуке Автора с Windows 11;
- только встроенные инструменты Codex для research, текста и генерации
  изображений; сторонние AI API запрещены.

Вне scope:

- отдельная web-панель, собственный телесуфлёр и управление камерой;
- синтетический ведущий, голос, музыка, b-roll и сложный художественный монтаж;
- несколько авторов, командная редактура и коммерческий SaaS;
- автоматическая оптимизация контента по просмотрам и A/B-тесты;
- обход CAPTCHA, MFA или ограничений площадок;
- копирование кода Текущего агента.

Extension points без реализации: дополнительные авторы и каналы, аналитика
эффективности, официальный Dzen API, облачный
worker. Ядро нельзя строить как одноразовый скрипт: контракты площадок и моделей
обязаны быть заменяемыми адаптерами.

### Scope Rules

- Новая площадка, роль, публичный UI или тип монтажа сначала меняют intent и
  spec, затем plan.
- Исправление внешнего адаптера не расширяет продуктовый scope.
- Обязательный результат нельзя тихо исключить из-за ограничения провайдера.
- Неописанное поведение запрещено до изменения спецификации.

### Golden case cards

| ID | Вход и акторы | Ожидаемый результат |
|---|---|---|
| GC-01 | Автор вводит тему «Как строительный контракт замораживает оборотку», утверждает план и пакет без правок | Пакет содержит 470–820 слов, Реестр источников, 3–5 визуалов, обложку, DOCX и три валидных платформенных представления; система ждёт запись |
| GC-02 | Автор сохраняет один MP4 длительностью 7:30 после редакционного gate | Worker сам принимает файл, сопоставляет речь, создаёт 1080p master: до первого anchor ведущий full-screen, затем непрерывный 50/50 до конца |
| GC-03 | Автор утверждает итог словом «Окей», target — четверг 14:00 Europe/Moscow | Система готовит непубличные ресурсы, вставляет ссылки в Telegram, проходит preflight и публикует три материала с видимым расхождением не более пяти минут |
| GC-04 | YouTube стал публичным, а Дзен вернул временную ошибку | YouTube не удаляется, Telegram не дублируется, повторяется только Дзен; после исчерпания retry Sardor получает одно аварийное сообщение с release_id и действием |
| GC-05 | После утверждения пакета Автор просит изменить число | Создаётся новая версия зависимых файлов, прежнее одобрение инвалидируется, монтаж и публикация старых hashes запрещены |

## 2. Glossary

Канонические определения находятся в `CONTEXT.md`.

| Термин | Определение | Не является |
|---|---|---|
| Автор | Веселков Сергей Николаевич | администратором системы |
| Выпуск | Комплект по одной теме для трёх площадок | отдельной статьёй или роликом |
| Манифест Выпуска | Каноническое состояние, версии, hashes и внешние IDs одного Выпуска | историей чата |
| Основной текст | Общие произносимые слова для телесуфлёра и Дзена | Telegram-подводкой |
| Редакционный пакет | Все материалы второго gate | только DOCX |
| Карта визуалов | 3–5 visual_id с anchors в Основном тексте | случайной галереей |
| Gate | Явное решение Автора над конкретными hashes | ответом AI или наличием файла |
| Publication attempt | Одна идемпотентная операция одной площадки | всем Выпуском |
| Release приложения | Версионный GitHub-артефакт системы | Выпуском контента |

### Glossary Rules

- Не смешивать release приложения и Выпуск.
- Не считать AI-черновик утверждённым артефактом.
- DOCX и preview — производные представления, не источники истины.
- Telegram-подводка не обязана дословно совпадать с Основным текстом.

## 3. Universal Core And Product Templates

V1 не является многопользовательским продуктом. Универсальны state machine,
артефактные версии, jobs, approvals и publisher protocol. Настройками являются
TOV, Humanize, целевые каналы, расписание, crop profile и credentials. Встроен
один шаблон `veselkov-economy-ru`. Следующий Автор должен подключаться новым
профилем без копирования orchestration core, но такой UI и миграция не входят в
v1.

## 4. Use-Case Catalog

| ID | Сценарий | Актор | Предусловия | Успех | Побочные эффекты |
|---|---|---|---|---|---|
| UC-01 | Одноразовая настройка | Sardor | Windows 11 и установлен release | валидны config, secrets, WSL2/Docker и каналы | audit, capability results |
| UC-02 | Начать Выпуск | Автор через Codex | нет конфликтующего активного Выпуска | создан release_id и план | manifest, job |
| UC-03 | Утвердить/вернуть план | Автор | plan_pending, совпала версия | scope заморожен или создан revision | approval/history |
| UC-04 | Собрать пакет | Worker+AI | план утверждён | полный валидный пакет | artifacts, eval records |
| UC-05 | Утвердить/вернуть пакет | Автор | editorial_pending | awaiting_recording или revision | approval/history |
| UC-06 | Принять запись | File watcher | awaiting_recording | один stable source принят | immutable media record |
| UC-07 | Смонтировать и проверить | Worker | source и пакет валидны | master и QC | alignment, timeline, render log |
| UC-08 | Утвердить/вернуть финал | Автор | final_pending | approved_to_schedule или revision | approval/history |
| UC-09 | Подготовить площадки | Publisher worker | final approval | три resources готовы и проверены | remote IDs, links |
| UC-10 | Вооружить расписание | Publisher worker | общий preflight успешен | три операции armed | scheduled jobs |
| UC-11 | Опубликовать | Providers/worker | target time | platform states public | receipts, analytics |
| UC-12 | Восстановить частичный сбой | Worker | хотя бы одна public | повторены только missing | attempts, alert при exhaustion |
| UC-13 | Показать статус | Автор/Codex | release_id известен | одна понятная карточка | нет внешних эффектов |
| UC-14 | Изменить target time | Автор | до final approval/arm | новая дата валидна | history, reschedule if allowed |
| UC-15 | Заменить запись | Автор | до final approval | новый source становится текущим | invalidation и rerender |
| UC-16 | Аварийно оповестить | Worker | terminal technical failure | Sardor получил сообщение | notification attempt |
| UC-17 | Обновить приложение | Sardor | принят GitHub Release | работает новая версия | backup, migration, deploy audit |
| UC-18 | Откатить приложение | Sardor | smoke новой версии провален | восстановлена предыдущая версия | rollback audit |

### Use-Case Rules

Каждая CLI-команда, job, AI-задача и adapter method ссылается минимум на один
UC-ID. Скрытые побочные эффекты запрещены. Новый сценарий сначала добавляется в
каталог.

## 5. Canonical State Machines

### Выпуск

| From | To | Trigger | Актор | Условие |
|---|---|---|---|---|
| — | topic_received | UC-02 | Автор | непустая тема |
| topic_received | plan_pending | plan built | Worker | plan schema valid |
| plan_pending | editorial_building | approve plan | Автор | expected version/hash |
| editorial_building | editorial_pending | UC-04 complete | Worker | package validator passed |
| editorial_pending | awaiting_recording | approve package | Автор | all package hashes |
| awaiting_recording | video_ingesting | UC-06 | Worker | exactly one stable candidate |
| video_ingesting | video_rendering | probe/alignment valid | Worker | duration and anchors valid |
| video_rendering | final_pending | UC-07 complete | Worker | QC passed |
| final_pending | approved_to_schedule | approve final | Автор | video, metadata and posts hashed |
| approved_to_schedule | preparing_publication | UC-09 | Worker | credentials healthy |
| preparing_publication | scheduled | UC-10 | Worker | all platforms validate |
| scheduled | published | UC-11 | Worker/providers | all platform states public |
| любой нетерминальный | revision_requested | Автор requests change | Автор | reason required |
| revision_requested | соответствующая building/pending | rebuild | Worker | dependency graph decides target |
| preparing_publication/scheduled | delayed | preflight/compensation | Worker | no intentional partial start |
| любой рабочий | needs_attention | terminal automation failure | Worker | attempts exhausted |

`published` терминален для v1. Прямое редактирование status запрещено. Каждый
переход пишет from/to, actor, reason, correlation_id, version и timestamp.
Переход через gate без Approval запрещён.

### Job

| From | To | Trigger | Актор | Условие |
|---|---|---|---|---|
| queued | running | lease acquired | Worker | lease not held |
| running | succeeded | handler complete | Worker | outcome persisted |
| running | retry_wait | retryable error | Worker | attempts remain |
| retry_wait | queued | due time | Scheduler | same idempotency key |
| running/retry_wait | failed | terminal/exhausted | Worker | error recorded |
| queued/retry_wait | cancelled | dependency invalidated | Orchestrator | reason required |

### Platform publication

`absent → preparing → prepared → validated → armed → public`; дополнительно
`retry_wait`, `failed`, `cancelled`. Из `public` автоматический переход назад
запрещён. `armed → cancelled` допустим только до public.

### Approval

`pending → approved|rejected|invalidated`. Approved может стать только
`invalidated`, если изменился зависимый hash; повторно approved требует новой
записи. Физическое удаление Approval запрещено.

### Notification

`queued → sending → delivered|retry_wait|failed`. Одинаковый suppression_key не
создаёт второе активное уведомление.

Сессия Codex не является бизнес-состоянием. Возобновление диалога всегда читает
Манифест и не может менять state само по себе.

## 6. Canonical Event Catalog

Events записываются после атомарного изменения состояния в append-only
`events.jsonl`; event и manifest commit используют один file lock и журнал
намерения. Consumer хранит `(consumer,event_id)`, поэтому redelivery безопасна.
Event schema имеет версию; события не заменяют Манифест и jobs DB.

Envelope: `event_id` UUIDv7, `name`, `version`, `occurred_at` UTC, `aggregate_type`,
`aggregate_id`, `actor`, `correlation_id`, `causation_id`, `payload`.

| Event | Publisher | Когда | Обязательные payload | Consumers |
|---|---|---|---|---|
| ReleaseStarted.v1 | Orchestrator | создан Выпуск | topic, profile_id | editorial |
| ApprovalRecorded.v1 | Orchestrator | решение gate | gate, decision, hashes | state machine |
| ArtifactSetValidated.v1 | Validator | пакет прошёл | kind, version, hashes | Codex view |
| RecordingAccepted.v1 | Media | source принят | asset_id, probe | video worker |
| VideoRendered.v1 | Video | QC прошёл | master_id, qc_id | final gate |
| PublicationPrepared.v1 | Adapter | remote draft создан | platform, remote_id, url | preflight |
| PublicationArmed.v1 | Publisher | schedule armed | platform, target_at | monitor |
| PublicationBecamePublic.v1 | Monitor | public подтверждён | platform, public_url | completion |
| RecoveryExhausted.v1 | Worker | retry исчерпан | platform, error_code | notifier |
| DeploymentCompleted.v1 | Deploy tooling | production обновлён | app_version, commit | diagnostics |

## 7. Data Model Level 2

Файлы Выпуска являются каноническими; SQLite обслуживает транзакционные jobs и
индексы. Все JSON schemas находятся в `src/smm_agent/contracts/schemas/`.

| Таблица/коллекция | Назначение | Обязательные поля | Ограничения |
|---|---|---|---|
| release.json | состояние Выпуска | release_id, schema_version, profile_id, topic, status, revision, created_at, updated_at, target_at | atomic replace, optimistic revision |
| artifact_versions.json | версии артефактов | artifact_id, kind, version, sha256, path, created_at, dependencies | append-only; path внутри release |
| approvals.jsonl | решения Автора | approval_id, gate, decision, actor_id, hashes, reason, at | immutable |
| events.jsonl | доменные события | полный envelope | append-only, unique event_id |
| sources.json | Реестр источников | claim_id, claim, source_url, publisher, checked_at, evidence, status | существенный claim имеет evidence |
| visuals/manifest.json | Карта визуалов | 3–5 visual_id, type, anchor_text, claim_ids, asset hash | порядок уникален |
| alignment.json | результат речи | segments, words, anchors, confidence, model | derived, versioned |
| timeline.json | монтажная шкала | first_visual_at, ordered visual spans | last ends at duration |
| jobs | durable queue | job_id, kind, status, due_at, lease_until, attempts, max_attempts, idempotency_key, payload_ref | unique idempotency_key |
| job_attempts | история запусков | attempt_id, job_id, started_at, ended_at, outcome, error_code | immutable |
| provider_resources | внешние объекты | release_id, platform, remote_id, state, public_url, idempotency_key, payload_hash | unique release+platform |
| notifications | аварийные доставки | notification_id, type, recipient, status, suppression_key, attempts | unique active suppression_key |
| deployments | локальная история версий | version, commit_sha, installed_at, result, previous_version | immutable |

Время хранится UTC RFC3339; target дополнительно содержит IANA timezone.
Физически удалять approvals, events, attempts, provider IDs и deployment history
нельзя. Тема и публичный текст не считаются чувствительными; OAuth, bot token и
browser session не попадают в эти структуры. Произвольный JSON разрешён только
в versioned payload со schema validation.

## 8. API Contract Baseline

Публичного HTTP API в v1 нет. Локальный API — CLI `smmctl`, stdin/stdout UTF-8,
stdout строго JSON, человекочитаемый текст строит Codex skill. Source of truth —
Pydantic schemas; JSON Schema генерируется в `docs/generated/contracts/`.
Business logic запрещена в CLI handlers.

| Команда | Доступ | UC | Idempotency | Назначение |
|---|---|---|---|---|
| `smmctl setup validate` | Sardor | UC-01 | нет | config/capabilities |
| `smmctl release start` | Автор | UC-02 | да | topic → release_id |
| `smmctl release status` | Автор/Sardor | UC-13 | нет | read model |
| `smmctl release decide` | Автор | UC-03/05/08 | да | gate decision + expected revision |
| `smmctl release revise` | Автор | UC-03/05/15 | да | reason + target |
| `smmctl release schedule` | Автор | UC-14 | да | target_at до arm |
| `smmctl worker run` | service | UC-04/06–12/16 | job key | background loop |
| `smmctl deploy doctor` | Sardor | UC-17/18 | нет | version/health report |

Ошибка:

```json
{"error":{"code":"STATE_CONFLICT","message":"Выпуск уже перешёл на другой этап.","hint":"Обновите статус и повторите решение для показанной версии.","requestId":"...","details":{}}}
```

Mutation требует `command_id`, `release_id`, `expected_revision`; повтор одного
`command_id` возвращает прежний outcome. Списочные read-команды используют
`limit<=100` и opaque cursor. Breaking contract создаёт новую CLI/schema major
version. DTO не строятся напрямую из SQLite rows. Provider callbacks, если
появятся, принимаются только после signature verification.

## 9. Canonical Architecture

Monorepo, Python 3.12, modular monolith и два процесса: short-lived CLI и
long-running worker. Production runtime — контейнеры Linux под Docker Desktop
WSL2 на Windows 11. Выбранные технологии: Pydantic 2, Typer, SQLite WAL,
filelock/OS locks, HTTPX, FFmpeg/ffprobe, локальное Whisper-compatible
распознавание без внешнего API, Playwright Chromium, прямые Telegram и Google
API clients, OpenTelemetry-compatible structured logging, pytest и Ruff/mypy.

Domain/application layers не импортируют integrations. AI, filesystem,
clock, subprocess и network доступны только через ports. Запрещены: business
logic в prompts, глобальный mutable singleton, Celery/Redis в v1, generic
`utils.py`, shell-строки с непроверенными аргументами и прямые provider calls из
Codex skill.

## 10. Source-Of-Truth Rules

- Продуктовые правила: этот `spec.md` и принятые ADR.
- Термины: `CONTEXT.md`.
- Business state: Манифест + immutable logs.
- Schemas: `contracts/schemas`.
- Jobs/provider index: SQLite schema и migrations.
- Runtime config: validated TOML + secret store.
- UI: Codex skill, который только отображает structured output.
- Deployment: Git tag, GitHub Release manifest и local deployment record.

Критическая логика запрещена только в prompt, DOCX, CI YAML, SQL read model,
platform UI, chat history или ручной SOP.

## 11. Repo Layout

```text
.
├── .agents/skills/veselkov-smm/
├── .github/workflows/{ci,release}.yml
├── config/smm-agent.example.toml
├── docs/{adr,work,generated,runbooks}/
├── migrations/
├── src/smm_agent/
│   ├── contracts/{commands,events,schemas}/
│   ├── domain/{release,editorial,video,publication}/
│   ├── application/<use_case>/
│   ├── adapters/{ai,files,media,publishing,secrets}/
│   ├── platform/{config,jobs,logging,clock}/
│   ├── cli/
│   └── worker/
├── tests/{unit,contract,integration,e2e,evals,fixtures}/
├── tools/architecture/
├── Dockerfile
└── compose.yaml
```

Application зависит от domain/contracts; adapters реализуют ports; domain не
зависит от adapters/platform. Public boundary модуля — `api.py` или `ports.py`.
Тесты повторяют bounded context. Папки `common`, `helpers`, `misc`, `shared`
запрещены без отдельного архитектурного решения. Import graph, циклы, размеры
файлов и forbidden calls проверяются CI.

## 12. AI-Agent Delivery Rules

Агент читает AGENTS, CONTEXT, принятый spec и локальные контракты; указывает
UC-ID и bounded context. Изменение поведения начинается с теста/eval. Изменение
минимально и не вводит скрытые defaults. Внешний эффект проходит через adapter
и job. Production-кодовый файл целится до 400 строк; превышение 600 требует
обоснования review. Неочевидный invariant комментируется причиной. Architecture
checks обязательны. Код агента не принимает собственный review и release gate.

## 13. Bounded Contexts

| Context | Владеет | Не владеет |
|---|---|---|
| Release | lifecycle, revisions, approvals | содержанием текста |
| Editorial | claims, text, visuals, metadata | записью и публикацией |
| Video | source, alignment, timeline, master/QC | редакционными правками |
| Publication | remote resources, schedule, recovery | изменением контента |
| Interaction | Codex cards и intent mapping | business state |
| Platform | jobs, config, logging, secrets ports | доменными решениями |
| Delivery | app versions и deploy history | контентными Выпусками |

## 14. Canonical Domain Model

| Aggregate/entity | Назначение | Инварианты |
|---|---|---|
| Release | корень Выпуска | один status/revision, все действия version-guarded |
| ArtifactVersion | неизменяемая версия файла | sha256 совпадает с bytes |
| Approval | решение gate | actor=Автор, hashes полны, immutable |
| Claim | доказуемое утверждение | существенный факт имеет evidence либо маркировку |
| Visual | смысловой материал | 3–5, один anchor, claims согласованы |
| MediaAsset | исходник/master | оригинал не перезаписывается |
| Publication | состояние площадки | один idempotency key на release+platform |
| Job | повторяемая работа | один lease, bounded retry |
| Notification | аварийное сообщение | dedupe по suppression key |

AI output не является entity до schema и deterministic validation. Analytics —
производная проекция. Article, teleprompter и main text не взаимозаменяемы.

## 15. Multitenancy Or Ownership Model

Система single-owner. `profile_id=veselkov` обязателен во всех Выпусках, jobs и
file paths; глобальны только app version и schema registry. Author commands
разрешены лишь текущему Codex project identity; Sardor имеет setup/diagnostic
scope. Worker переносит profile_id из job и не принимает его из AI output.
Ключи имеют форму `<profile_id>/<release_id>/<entity_id>`.

## 16. Data Rules And Import Rules

IDs — UUIDv7; hashes — SHA-256 lowercase; время — UTC, расписание — UTC плюс
`Europe/Moscow`. Имена SQLite таблиц plural snake_case. Foreign keys включены.
Архивация Выпуска только логическая после published; implicit deletion
запрещён.

Ingest видео — двухфазный import: snapshot/validation → atomic accept. Preview
содержит path label, size, mtime, duration, streams и errors. Несколько файлов
блокируют execute. Merge записей не выполняется. Коды: `NO_STABLE_RECORDING`,
`AMBIGUOUS_RECORDING`, `UNSUPPORTED_MEDIA`, `DURATION_OUT_OF_RANGE`.

## 17. Auth, Sessions And CSRF

V1 не имеет browser UI приложения. Локальный OS account и права socket/files
являются входной границей; CLI actor передаётся подписанным локальным context,
который Codex skill получает из защищённой конфигурации. Provider OAuth проходит
одноразово при setup, refresh tokens хранятся в secret store. Playwright profile
доступен только worker OS account. Telegram bot token аналогично.

Нет HTTP session и CSRF surface. CLI mutations rate-limited до 10/мин на actor;
worker concurrency задаётся по типу job. Credential recovery требует Sardor и
пишется в audit. UI никогда не является security boundary.

## 18. RBAC Matrix

| Role | Scope | Права |
|---|---|---|
| author | один profile/release | start, view, revise, approve, change schedule |
| operator | installation | setup, diagnostics, credentials, deploy/rollback, read all |
| worker | assigned jobs | internal transitions и adapters, без approvals |

| Permission | Значение | Roles | Ограничение |
|---|---|---|---|
| release.read | читать статус/artifacts | author, operator, worker | same profile |
| release.decide | решать gate | author | expected hashes |
| release.execute | внутренний переход | worker | leased job |
| integration.manage | менять credentials | operator | audit required |
| deployment.manage | deploy/rollback | operator | released version only |

Deny by default; permission проверяет application layer. Неописанное право
запрещено. Privileged actions логируются.

## 19. Channel And Integration Model

| Канал | Направление/identity | Operation ID | Вложения | Подтверждение и устойчивость |
|---|---|---|---|---|
| Codex | inbound; local project actor | command_id | ссылки на local previews | manifest response; повтор безопасен |
| Recording inbox | inbound; configured directory | asset fingerprint | MP4/MOV/MKV | stable-window + ffprobe |
| YouTube | outbound; channel OAuth | video_id | MP4, JPEG | API status polling, resumable upload |
| Dzen | outbound; authenticated browser profile | draft/public URL | article, JPEG/PNG | DOM assertions, screenshots, polling |
| Telegram channel | outbound; bot token/admin rights | message_id | native MP4 + caption | API response + getChat checks |
| Telegram alert | outbound; same bot | notification_id | text only | retry/dedupe to 276042853 |
| GitHub | delivery; repo credentials | commit/tag/run ID | public source/release bundle | protected branch, Actions attestations |

External input никогда напрямую не меняет status. Provider payload сохраняется
редактированно: IDs/status/error без tokens и лишних персональных данных.

## 20. Frontend Runtime And UI Flow Specs

Единственная поверхность Автора — Codex skill.

| Flow | Entry | Актор | Happy path | Обязательные состояния |
|---|---|---|---|---|
| UI-01 | тема | Автор | карточка плана + одно действие | building, error, conflict |
| UI-02 | «Окей» | Автор | утверждён единственный pending gate | ambiguous, stale, rejected |
| UI-03 | замечание | Автор | новая версия и обновлённый preview | invalid target, processing |
| UI-04 | ожидание записи | Автор | путь inbox и автоматическое продолжение | no file, many files, invalid duration |
| UI-05 | финальный preview | Автор | video/package/target в одной карточке | QC failure, stale hashes |
| UI-06 | status | Автор | Готово/Проверьте/Сейчас/Дальше | delayed, needs_attention |

UI не вычисляет permissions, не перезаписывает stale revision и показывает
причину каждого блокирования. Destructive action в v1 отсутствует.

## 21. Main Business Lifecycle

Внутренние статусы определены в разделе 5. Пользователь видит укрупнённо:
«готовим план», «нужно утвердить план», «готовим материалы», «нужно утвердить
материалы», «ждём запись», «монтируем», «нужно утвердить финал», «готовим
публикацию», «запланировано», «опубликовано», «нужна помощь».

Revision всегда сохраняет историю и инвалидирует downstream hashes. Reopen
published Выпуска запрещён; создаётся новый. Cancellation до public требует
причину и отдельное будущее решение, поэтому в v1 вместо неё используется
`needs_attention`. Duplicate topic не считается дублем автоматически.

## 22. Assignment, Routing, SLA Or Workload Rules

Jobs маршрутизируются по `kind`, не AI-решением. Один worker может исполнять
один FFmpeg job, до двух network jobs и один browser job одновременно. Lease
renewal каждые 30 секунд; истёкший lease допускает redelivery. Scheduled publish
получает приоритет за 30 минут до target. Queue lag свыше 60 секунд для publish
и 5 минут для прочих jobs нарушает SLO. Race решает optimistic revision и unique
idempotency key.

## 23. Dedupe, Merge And Conflict Rules

Command duplicate: тот же command_id. Recording duplicate: SHA-256+size.
Publication duplicate: release_id+platform. Notification duplicate:
suppression_key. AI может подсказать похожесть, но не объединяет объекты.
Конфликт revision, несколько recordings, изменившийся approved hash и иной
remote payload блокируют операцию. Public resources не merge и не delete.

## 24. AI Operating Model

| Сценарий | Input | Output | Low confidence | Запрещено менять | Проверки |
|---|---|---|---|---|---|
| План | topic, profile; Codex built-in | plan schema | вернуть review item | topic | schema, scope |
| Research | plan, web evidence; Codex web tool | claims/sources | пометить conflict/unsupported | source content | URL/date/claim coverage |
| Draft/TOV | claims, TOV; Codex | main text + claim refs | human review | facts/numbers | claim diff, word budget |
| Humanize | text, protected spans; Codex skill | edited text + findings | оставить span и report | claims, quotes, greeting | skill lint, protected diff |
| Visual planning | text/claims; Codex | 3–5 anchors/specs | manual package review | claim values | count/order/coverage |
| Image generation | visual spec/reference; Codex image tool | raster candidate | regenerate/block | face identity acceptance | dimensions, text overlay, human gate |
| Alignment | ASR+text+anchors | timestamps/confidence | block render | approved text/order | monotonicity, threshold |
| Intent mapping | phrase+state | command proposal | ask one clarification | business state | allowed-command schema |

Сторонние AI API и прямые вызовы моделей из worker запрещены. Research, письмо,
Humanize и генерация изображений происходят внутри активной Codex-сессии до
соответствующего gate. После редакционного gate worker выполняет только локальные
медиа-операции; speech-to-text использует локальную модель без сетевой отправки
записи.

Порог alignment задаётся config и стартует с 0.85 aggregate и 0.75 на anchor;
менять его можно только через versioned config. Prompt/model/version, input
hashes, output, latency и validation result записываются без секретов. Evals
покрывают TOV, facts, duration budget, 3–5 visuals, greeting и command mapping.
Неприемлемы invented facts, изменённые protected spans, пропущенный gate и
несоответствующий схеме output.

## 25. Files And Media

Bytes хранятся под `var/releases/<release_id>/`, metadata — в artifact index.
Путь нормализуется и обязан оставаться внутри release root. Allowed:
MP4/MOV/MKV source, MP4 master, PNG/JPEG visuals, JSON/MD/TXT/DOCX artifacts.
Максимальный source 20 GiB, отдельный visual 25 MiB, DOCX 100 MiB.

Source проходит ffprobe и полное decode-check; антивирусный scan выполняется,
если настроен локальный scanner, иначе production setup не проходит readiness.
Original read-only, generated temp пишется в release temp и атомарно rename.
Blocked file не удаляется, не исполняется и получает quarantine metadata.
Codex показывает локальные paths только Автору/оператору.

## 26. Notifications And Campaigns

Есть только аварийное уведомление `operator_attention_required`; это не
контентный Telegram-пост. Получатель фиксирован config и обязан равняться
276042853 в production. Quiet hours отсутствуют, потому что уведомление
аварийное. Retry: 1, 2, 5, 15 минут, максимум четыре попытки; после failure
ошибка остаётся в diagnostics. Dedupe по release+platform+error class до
изменения состояния. Массовых рассылок нет.

## 27. Analytics And Read Models

V1 хранит operational read model: время стадий, ручные минуты Sardor,
длительность видео, число визуалов, publication drift, retry count и outcomes.
Источник — events/jobs/provider receipts. `release-summary.json` обновляется
после события, допустимая свежесть 60 секунд. Формулы: cycle time = published -
created; drift = max(public_at)-min(public_at); routine operator minutes вводятся
только явным diagnostic event, default отсутствует. Growth analytics вне scope.

## 28. Observability

Каждый log: timestamp, level, app_version, commit_sha, request_id, trace_id,
profile_id, actor_id, module, use_case, entity_type/id, provider, event/job ID и
error_code. Логируются transitions, commands, AI validations, subprocess exit,
provider calls без payload secrets, retries, config schema version, deploy и
rollback.

Alerts: worker down >2 минут; publish queue lag >60 секунд; provider prepare/
publish failures; AI schema/fact failure spike; disk free <20 GiB; SQLite errors;
recording ambiguity; notification final failure. Health report доступен через
`smmctl deploy doctor --json`.

## 29. Security, Privacy And Compliance

Secrets только в защищённых ACL-файлах Docker secrets под `%PROGRAMDATA%` и
Windows Credential Manager для bootstrap, никогда в Git, manifests, logs,
screenshots или GitHub artifacts. TLS обязателен наружу. Filesystem права
ограничиваются Windows ACL пользователем Автора, SYSTEM и оператором. BitLocker
или Device Encryption обязателен. OAuth scopes минимальны. Logs и backups
шифруются на диске средствами хоста; backup archive дополнительно шифруется.

Browser screenshots перед сохранением маскируют cookies/tokens. Retention:
исходники и рабочие artifacts 180 дней после published, финальные artifacts и
audit 3 года, operational logs 90 дней, secrets до revocation. Удаление по
retention — отдельная будущая подтверждаемая job; v1 только сообщает кандидатов
и ничего физически не удаляет. Поддержка Sardor действует локально и auditится.

## 30. Runtime, Staging And Deploy

Production host — ноутбук Сергея Николаевича с Windows 11. Composition:
Docker Desktop WSL2, `smm-worker`, локальный `telegram-bot-api`, Chromium runtime
и bind-mounted config/releases/inbox. CLI запускается в том же versioned image.
Environments: local, staging с тестовыми каналами, production. Windows Task
Scheduler при входе запускает Docker Desktop и worker, а отдельная задача с
правом wake запускается до ближайшего target time. Preflight запрещает arm,
если питание, сон или сеть не позволяют гарантировать запуск.

GitHub contract:

1. Код и SDLC-артефакты хранятся в публичном GitHub repository. Runtime data,
   реальные конфиги, browser profile, tokens, исходники и готовые Выпуски туда
   не попадают.
2. `main` защищён: pull request, один независимый review, успешные `ci` checks,
   запрет force-push.
3. Версии SemVer. Tag `vX.Y.Z` создаётся только из `main` после принятого plan и
   verification evidence.
4. GitHub Actions строит Linux image и Windows bootstrap bundle, создаёт SBOM,
   checksums и GitHub Release. Release artifact неизменяем.
5. Production обновляется только явной командой Sardor на конкретную версию;
   автоматический deploy из push запрещён в v1.
6. Порядок: backup → pull release by checksum → migrate jobs DB → start services
   → readiness → smoke golden subset → записать deployment.
7. Startup проверяет config/schema/migration; liveness — жив event loop;
   readiness — writable state, FFmpeg/Chromium и required local services.
8. При smoke failure сервис останавливается, восстанавливаются previous image,
   DB backup и config snapshot; content files не понижаются и не удаляются.
9. Manual hotfix внутри container/production checkout запрещён; исправление идёт
   через PR и новый patch release.

GitHub outage не останавливает установленный worker. Repository принадлежит
GitHub-пользователю Sardor и создаётся публичным. GitHub Environment и
credentials обязательны перед первой публикацией release, но не хранятся в
spec.

## 31. Environment And Config Contract

TOML schema versioned. Mandatory production groups:

| Group | Примеры | При отсутствии | Degraded start |
|---|---|---|---|
| runtime | profile_id, state_root, timezone | startup error | нет |
| jobs | sqlite_path, concurrency, lease | startup error | нет |
| files | recording_inbox, limits, stability_window | watcher not ready | worker без media запрещён |
| media | ffmpeg/ffprobe paths, crop profile, codecs | readiness fail | нет |
| Codex | required skills/tool capabilities, thresholds | editorial unavailable | только status допустим |
| YouTube | channel binding, secret refs | platform not ready | до final gate допустим |
| Dzen | browser profile path, channel URL | platform not ready | до final gate допустим |
| Telegram | API URL, channel ID, alert ID | platform not ready | до final gate допустим |
| observability | log path/level | startup error | нет |
| delivery | app version, commit SHA | startup error | нет |

Default разрешён только для безопасных tuning values, явно записанных в example
config. Mandatory identity/path/credential никогда не получает invented default.
Validation выполняется при любой CLI-команде и worker startup.

## 32. Backup And Disaster Recovery

Перед deploy и ежедневно копируются releases manifests/artifacts, jobs SQLite,
config без secret values, encrypted browser profile и deployment history.
Secrets экспортируются только штатным защищённым механизмом отдельно. Retention:
7 daily, 4 weekly, 6 monthly. Ежемесячно выполняется restore drill в staging.

Порядок restore: app version → config → jobs DB → release files → browser
profile/secret binding → doctor → smoke. Восстановление неполно, если расходятся
hashes, потеряны approvals/provider IDs или scheduled jobs не сверены с
площадками. RPO 24 часа для опубликованного архива и 5 минут для активного
manifest/jobs через локальный incremental journal; RTO 2 часа.

## 33. Testing Strategy

Все test commands выполняются последовательно через
`/root/.local/bin/codex-test-guard`; targeted timeout 10m, full suite 20m, если
репозиторий не задаст строже.

Типы: unit domain, property/state-machine, schema/contract, SQLite/job
integration, filesystem/media, provider sandbox/fake, Playwright staging smoke,
AI evals, security, migration, deploy/rollback и end-to-end.

Критические E2E соответствуют GC-01…GC-05. Fixture video проверяет каждый
visual boundary и last-to-end. Provider fakes проверяют prepare/arm/partial
failure/redelivery.

| Модуль | Обязательный минимум |
|---|---|
| Release | все переходы, stale revision, invalidation |
| Editorial | claims, 470–820, TOV/Humanize protected diff, 3–5 |
| Video | ingest ambiguity, 5–10 min, alignment, pixel/frame QC |
| Publication | idempotency, compensation, partial recovery, caption 1000 |
| Interaction | одно действие, ambiguous «Окей», readable errors |
| Delivery | migration both directions or restore rollback, checksums |

| Change | Required tests |
|---|---|
| domain rule | unit + state property + affected E2E |
| schema/migration | contract + upgrade from previous release + backup restore |
| AI prompt/model | frozen eval set + fact/protected-span regression |
| provider adapter | fake contract + staging smoke |
| FFmpeg graph | golden media + boundary screenshots + decode |
| deploy workflow | CI dry run + staging install/rollback |

Release gate: lint/type/architecture pass; unit/integration/evals pass; no high
security findings; migrations verified; staging golden cases pass; independent
review recorded; human accepts residual risk.

## 34. Non-Functional Requirements

- CLI status p95 <1 с на production host.
- Mutation acknowledgement p95 <2 с без тяжёлой работы; тяжёлая работа job.
- Worker startup <60 с; recording detection <2 минут после stable window.
- Editorial package target <30 минут при здоровых providers.
- Video render target ≤2x source duration on reference CPU, подтверждается load
  test; одновременно только один render.
- Availability worker 99% в недельном окне от final approval до публикации.
- Публикационные команды отправляются ±30 с от target; visible drift SLO ≤5 мин.
- Provider outage переводит только зависимый процесс в retry/delayed; approved
  artifacts остаются доступны.
- Disk admission требует свободно max(20 GiB, 3× source size).

## 35. Feature Delivery Gates

Definition of Ready: указан context и UC; actor/trigger/happy/errors; inputs,
outputs и data changes; permissions/audit; tests и acceptance; spec принят и
file-specific plan готов.

Definition of Done: guarded tests, types/lint/architecture зелёные;
observability и понятные errors добавлены; spec/ADR обновлены при изменении
границ; нет hidden fallback; security/data boundaries проверены; staging
evidence, rollout и rollback приложены; независимый review завершён.

## 36. Canonical Implementation Decisions

| Выбрано | Отклонено | Причина/следствие |
|---|---|---|
| Codex facade + deterministic local orchestrator | prompt-only agent | gates и расписание проверяются кодом |
| Manifest files + SQLite jobs | облачная распределённая система | один host, inspectable artifacts |
| Python modular monolith | микросервисы | меньше operational complexity, строгие ports |
| Configured recording inbox | scan всего устройства/обязательный Drive | безопасность и автоматический trigger |
| FFmpeg deterministic render | ручной editor | воспроизводимость и QC |
| Local Telegram Bot API Server | cloud-only 50 MB transport | нативное видео 5–10 минут |
| Isolated Playwright Dzen adapter | неподтверждённый API | реалистичный v1 с явным риском |
| GitHub protected main + Releases | ручные production copies | traceable version и rollback |
| Manual production promotion | deploy каждого push | снижает риск публикационного простоя |
| Публичный GitHub repository без runtime data | приватный repo или публикация пользовательских данных | открытая история разработки без утечки секретов и контента |
| Windows 11 laptop + Docker Desktop WSL2 | отдельный сервер | всё работает на устройстве Автора |
| Codex built-ins + локальный speech alignment | сторонние AI API | единый согласованный AI-контур без передачи записи внешнему AI provider |

## 37. Deferred Alignment Items

Не блокируют принятие spec и разработку, но блокируют соответствующий rollout:

| Не решено | Когда решить | Уже фиксировано | До решения запрещено |
|---|---|---|---|
| Абсолютный Windows-путь recording inbox | до deployment plan | Windows 11 laptop, Docker Desktop WSL2, выделенная папка | сканировать весь диск |
| Реальные OAuth/bot/channel credentials | setup staging/production | secret store, least privilege | класть tokens в Git/config |
| Dzen DOM selectors и наличие native schedule | первый adapter smoke | Playwright contract и local fallback | обещать readiness без smoke |
| Reference CPU render ratio | performance test | функциональный hard gate 5–10 мин | ослаблять content limit |

Рабочие предположения: ноутбук x86_64, Windows 11 допускает WSL2/Docker Desktop,
BIOS/Windows разрешают wake timers и достаточно места для двух одновременных
копий source и render temp. Если аппаратный wake невозможен, preflight задержит
Выпуск, а не поставит ненадёжную отложку.

## 38. Final Rule

Если быстрая реализация нарушает эту спецификацию, исправляется реализация, а не
спецификация размывается ради удобства.

## Spec acceptance

- Decision: pending
- Date: pending
- Notes: после принятия запускаются architecture guardrails и file-specific
  `plan.md`; реализация до этого запрещена.
