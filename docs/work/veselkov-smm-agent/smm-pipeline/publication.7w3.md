# Публикация Выпуска — 7w3

**Parent:** [SMM-пайплайн](smm-pipeline.7w3.md)

## Wish

После финального «Окей» утверждённые материалы без участия Sardor становятся
публичными в YouTube, Дзене и Telegram в один target window. До первой public
операции любой общий сбой задерживает весь Выпуск; после частичного внешнего
сбоя система сохраняет успешное и повторяет только отсутствующее без дублей.

## Why

### Causes

Площадки не дают общей транзакции. YouTube поддерживает нативную отложку,
отложенная публикация Дзена заранее выдаёт рабочую ссылку, а Telegram Bot API
не имеет отложенной отправки. Ноутбук может спать или быть выключен.

### Intentions

Разделить непубличную подготовку и отложенный public transition. Использовать
нативное расписание YouTube и Дзена, а для Telegram создать локальную Windows
task после получения обеих ссылок. Сделать операции идемпотентными и явно
восстанавливать неизбежный частичный сбой при выключенном ноутбуке.

## What

Publication coordinator и три адаптера используют контракт:

- `prepare` создаёт private upload/draft/local payload;
- `validate` проверяет правильную версию, права, remote state и известные URL;
- `arm` ставит YouTube/Дзен в нативную отложку либо Telegram в локальную;
- `publish` отправляет Telegram в target или восстанавливает пропущенную отправку;
- `status` читает фактическое состояние;
- `cancel_schedule` отменяет ещё не public отложку, когда площадка это позволяет;
- `retry_missing` повторяет только отсутствующий side effect с прежним
  idempotency key.

`schedule` означает подтверждённую нативную отложку для YouTube и Дзена и
durable job в SQLite и Windows Task Scheduler для Telegram.

## Who

- Автор утверждает master, Telegram-копию, обложку, metadata, posts и target.
- Worker готовит resources и исполняет target job.
- YouTube adapter использует OAuth канала Автора.
- Dzen adapter использует отдельный локальный Playwright profile.
- Telegram adapter публикует channel message и отправляет аварийный alert.
- Sardor один раз выдаёт доступы и реагирует только на terminal incident.

## Where

- Контракты: `src/smm_agent/domain/publication/ports.py`.
- Реализации: `src/smm_agent/adapters/publishing/`.
- Jobs, attempts, remote IDs, URLs и states: единая SQLite DB.
- Payload bytes: release artifact directory.
- Credentials: Windows Credential Manager.
- Dzen session: локальный browser profile под NTFS ACL.
- Target timezone: `Europe/Moscow`.

## When

Default target — ближайший четверг 14:00 Europe/Moscow, который ещё допускает
полный prepare. Автор видит и может изменить target до финального gate.

После final approval:

1. загрузить YouTube video как private и получить `video_id`;
2. создать Dzen draft с обложкой и 3–5 визуалами;
3. поставить статью Дзена в отложку и сохранить выданный material URL;
4. собрать Telegram caption с YouTube/Dzen links и проверить 1000 символов;
5. проверить Telegram-копию и channel rights;
6. выполнить общий prepare validation;
7. поставить YouTube в нативную отложку, создать T−30 preflight task и T Telegram task;
8. в T−30 проверить нативные schedules, worker, сеть, credentials и payload;
9. в T площадки публикуют YouTube/Дзен, а Windows task отправляет Telegram;
10. наблюдать до `published`, `recovering`, `delayed` или `needs_attention`.

Если T−30 preflight не проходит, worker до target отменяет все три schedules и
переводит Выпуск в delayed. Если ноутбук выключен в target, YouTube и Дзен могут
стать public без Telegram; reconciliation отправляет только отсутствующий пост
после запуска ноутбука и создаёт incident о нарушении target window.

## Method

YouTube:

- resumable upload с `privacyStatus=private`;
- дождаться processing complete, затем установить metadata и thumbnail;
- задать `status.publishAt` для private video и подтвердить schedule через API;
- после target сверить фактический public state.

Telegram:

- отправить `telegram-video.mp4` как native streaming video вместе с caption;
- caption — Unicode text + entities, не более 1000 видимых символов;
- слова «блоге» и «YouTube» ссылаются на материалы этого Выпуска;
- CTA и три reaction rows входят в предел;
- cloud Bot API используется только если файл ≤49 000 000 bytes;
- `release_id + telegram` связывается с одним `message_id`; перед retry adapter
  сверяет сохранённый result и recent channel messages.

Дзен:

- Playwright запускается на Windows с persistent profile;
- setup открывает headful browser для ручного login/MFA Sardor;
- после входа adapter обязан выбрать и подтвердить канал «Экономика не для
  всех!» (`https://dzen.ru/ekonomikadliavseh`) до любой mutation; находящийся в
  том же профиле пустой канал «Админ Экономика не для всех» с пометкой
  «Основной» запрещён для создания, редактирования и публикации материалов;
- adapter создаёт draft, вставляет тот же Основной текст, оформление, cover и
  визуалы, задаёт target и сохраняет выданную рабочую ссылку, screenshot и DOM assertions;
- CAPTCHA/MFA не обходятся;
- capability tracer bullet обязан доказать draft identity, стабильность ссылки
  отложенного материала и проверку schedule/public state до production readiness.

Publication coordination:

- ссылка YouTube известна после private upload, ссылка Дзена — после создания
  отложенной публикации; только затем собирается финальный Telegram payload;
- до target сбой общей проверки отменяет доступные нативные schedules;
- после первого public результата любая ошибка переводит Выпуск в recovering;
- public content автоматически не удаляется.

Retry policy по умолчанию для временных provider errors: 30 секунд, 2 минуты,
5 минут и 15 минут. Permanent auth/permission/schema errors не повторяются.
После исчерпания создаётся incident и alert Sardor с ID 276042853.

## Boundaries

- Модуль не меняет утверждённый контент ради прохождения площадки.
- Публикация невозможна без совпадения hashes final approval.
- YouTube и Дзен должны использовать нативное расписание; Telegram — Windows task.
- Public material не удаляется автоматически.
- Adapter не обходит CAPTCHA, MFA или правила площадки.
- Sardor не является штатным публикационным оператором.

## Limitations

- Физическая атомарность трёх площадок невозможна; целевой visible drift — не
  более пяти минут, затем открывается incident.
- Выключенный Windows laptop не отправляет Telegram и alert до следующего
  запуска. Нативные публикации YouTube и Дзена могут выйти вовремя, поэтому
  физическая атомарность и полное отсутствие частичного выпуска не гарантируются.
- Dzen browser automation зависит от UI и является главным integration risk.
- Telegram-копия может не пройти одновременно size и readability gates; тогда
  release блокируется до отдельного design decision о Local Bot API Server.
- YouTube API project может требовать audit, прежде чем uploads смогут стать
  public.

## What's-next

Spec фиксирует state machine, scheduled-link contract, Windows wake,
retry taxonomy и adapters. Первый implementation plan обязан начинаться с Dzen,
Telegram-size и Windows-wake tracer bullets до строительства всего пайплайна.

## Проверенные ограничения

- [YouTube Data API](https://developers.google.com/youtube/v3/docs/videos):
  `publishAt` применим только к private video, которое ещё не публиковалось; v1
  использует его для нативной отложки.
- [Telegram Bot Features](https://core.telegram.org/bots/features): cloud Bot API
  указывает upload limit 50 MB, local server — 2000 MB.
- В открытой официальной документации Дзена не найден подтверждённый article
  publishing API. Это результат поиска, а не утверждение об отсутствии
  партнёрского или закрытого интерфейса.
