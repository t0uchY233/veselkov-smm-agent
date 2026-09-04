---
title: "План реализации SMM-агента Сергея Веселкова"
status: active
spec: spec.md
spec_version: 0.2.1
created: 2026-09-03
updated: 2026-09-03
owner: Sardor
---

# План реализации

План разбивает production v1 на вертикальные срезы. Каждый срез заканчивается
наблюдаемым поведением, тестами и отдельным evidence-файлом. Реальные публикации
запрещены до финального human gate; внешние адаптеры до capability smoke работают
только с записанными fixtures или test/draft resources.

## Неподвижные контракты

- Python 3.12, package `smm_agent`, JSON CLI `smmctl`, SQLite source of truth.
- Domain не импортирует adapters; сеть и subprocess доступны только adapters.
- Каждая mutation принимает `command_id`, `expected_revision` и actor context.
- Один активный Выпуск; approvals привязаны к hashes конкретных версий.
- YouTube и Дзен используют нативную отложку. Отложенный Дзен возвращает ссылку
  до target; обе ссылки механически подставляются в утверждённый Telegram caption.
- Telegram отправляется Windows Task Scheduler и восстанавливается без дубля.
- Все тесты запускаются последовательно только через `codex-test-guard`.

## Golden cases

- GC-01: тема создаёт Выпуск, статус показывает один следующий шаг.
- GC-02: утверждённый пакет + запись 7:30 дают master 1080p и 3–5 визуалов.
- GC-03: отложенные YouTube/Дзен и Telegram task имеют общий target и обе ссылки.
- GC-04: после частичного сбоя повторяется только отсутствующая площадка.
- GC-05: изменение утверждённого артефакта инвалидирует зависимые approvals.

## Срезы

### Slice 1: Выпуск помнит тему и ведёт к следующему действию

- **Delivers:** `smmctl release start` создаёт единственный активный Выпуск в
  SQLite, а `release status` после перезапуска процесса возвращает его revision,
  state и русское `next_action`.
- **Files:** `pyproject.toml`, `src/smm_agent/contracts/cli.py`,
  `src/smm_agent/domain/release/{model,service}.py`,
  `src/smm_agent/platform/{config,db}.py`, `src/smm_agent/cli/main.py`,
  `migrations/0001_initial.sql`, `tests/unit/test_release_service.py`,
  `tests/integration/test_release_cli.py`, `docs/generated/contracts/*.json`,
  `docs/work/veselkov-smm-agent/evidence/slice-01.md`.
- **Acceptance:** stdout содержит один валидный JSON; повтор `command_id` с тем
  же payload возвращает прежний результат; иной payload даёт
  `IDEMPOTENCY_CONFLICT`; второй active release запрещён; stale revision даёт
  `STATE_CONFLICT`; состояние переживает новый CLI process.
- **Out of scope:** AI-контент, approvals, media, providers и Windows installer.
- **Blocked by:** none.

### Slice 2: План и редакционный пакет проходят три человеческих gate

- **Delivers:** импорт версионированных plan/editorial artifacts, решения
  plan/editorial/final gate и детерминированная invalidation зависимостей.
- **Files:** `src/smm_agent/contracts/editorial.py`,
  `src/smm_agent/domain/{release,editorial}/`,
  `src/smm_agent/application/{import_plan,import_editorial,decide,revise}/`,
  `.agents/skills/veselkov-smm/`, `tests/{unit,integration,evals}/`, evidence 02.
- **Acceptance:** GC-01 и GC-05; 3–5 визуалов; основной текст един для суфлёра и
  статьи; TOV → humanizer lint → formatter проверяется отчётом.
- **Out of scope:** обработка записи и внешние площадки.
- **Blocked by:** Slice 1.

### Slice 3: Запись автоматически превращается в проверяемый монтаж — реализован

- **Delivers:** watcher принимает один стабильный MP4 5–10 минут, offline ASR
  строит anchors, FFmpeg создаёт master и Telegram-копию с непрерывными визуалами.
- **Files:** `src/smm_agent/domain/video/`,
  `src/smm_agent/adapters/{files,media}/`, `src/smm_agent/worker/`, media fixtures,
  `tests/{unit,integration,e2e}/`, evidence 03.
- **Acceptance:** GC-02; Автор слева, текущий визуал справа от первого anchor до
  следующего, последний до конца; Full HD; Telegram ≤49 MB; low-confidence блокирует render.
- **Out of scope:** реальные публикации.
- **Blocked by:** Slice 2.

### Slice 4: Площадки готовят один согласованный отложенный Выпуск

- **Delivers:** ports и replay adapters доказывают prepare/arm/status/cancel;
  Telegram payload создаётся только после получения ссылок YouTube и Дзена.
- **Files:** `src/smm_agent/domain/publication/`,
  `src/smm_agent/adapters/publishing/{youtube,dzen,telegram,replay}.py`,
  `src/smm_agent/platform/jobs.py`, contract/integration tests, evidence 04.
- **Acceptance:** GC-03 на replay; ссылка scheduled Dzen присутствует до target;
  caption ≤1000; preflight отменяет все доступные schedules до первого public.
- **Out of scope:** реальные credentials и production channels.
- **Blocked by:** Slices 1–3.

### Slice 5: Частичный выпуск восстанавливается без дублей

- **Delivers:** durable jobs, leases, retry taxonomy, reconciliation и alert
  Sardor `276042853` после исчерпания recovery.
- **Files:** `src/smm_agent/platform/{jobs,logging}.py`,
  `src/smm_agent/application/{publish,reconcile,notify}/`, failure fixtures,
  integration/e2e tests, evidence 05.
- **Acceptance:** GC-04; crash после каждого side effect; один remote ID на
  платформу; повтор отправляет только missing; public автоматически не удаляется.
- **Out of scope:** Windows packaging.
- **Blocked by:** Slice 4.

### Slice 6: Реальные capability checks площадок и Windows

- **Delivers:** draft/test smoke для Playwright Dzen, YouTube OAuth,
  Telegram size/readability и Task Scheduler wake; секреты в Credential Manager.
- **Files:** production adapters, `src/smm_agent/adapters/{secrets,windows}/`,
  `config/smm-agent.example.toml`, `docs/runbooks/setup.md`, live-smoke harness,
  evidence 06.
- **Acceptance:** scheduled Dzen URL стабилен и принадлежит нужной статье;
  YouTube `publishAt` читается обратно; Telegram test send подтверждён; wake smoke
  исполнен на ноутбуке. Любой непройденный check блокирует production readiness.
- **Out of scope:** публикация на боевых каналах без отдельного финального gate.
- **Blocked by:** Slices 4–5 и реальные credentials/setup.

### Slice 7: Один Codex-диалог проводит Автора от темы до финального «Окей»

- **Delivers:** project skill всегда читает CLI status, показывает один gate и
  запускает только допустимую следующую команду; недельная память чата не нужна.
- **Files:** `.agents/skills/veselkov-smm/`, interaction fixtures/evals,
  `docs/runbooks/author.md`, evidence 07.
- **Acceptance:** GC-01–GC-05 выполняются в одной demo-сессии; пропуск стадии и
  неоднозначное «Окей» невозможны; Автор не использует terminal/provider UI.
- **Out of scope:** второй пользовательский интерфейс.
- **Blocked by:** Slices 1–6.

### Slice 8: Установка, обновление и откат одной командой

- **Delivers:** Windows x64 bundle, installer/updater, backup, diagnostics,
  GitHub Actions и версионный GitHub Release.
- **Files:** `.github/workflows/{ci,release}.yml`, `packaging/windows/`,
  `tools/architecture/`, `docs/runbooks/{deploy,rollback,diagnostics}.md`,
  `tests/packaging/`, evidence 08 и финальный HTML-отчёт.
- **Acceptance:** clean Windows install; one-command boot; guarded full suite;
  secret scan; upgrade и rollback с восстановлением DB; GC-01–GC-05 повторены.
- **Out of scope:** auto-update без Sardor.
- **Blocked by:** Slices 1–7.

## Текущий build loop

Slices 1–3 реализованы и проверены; evidence сохранён в `evidence/`.
Следующий срез: Slice 4. Перед его завершением выполняются последовательный
guarded test run, свежий review и исправление critical/high findings.
