# Публикация Выпуска — 7w3

**Parent:** [SMM-пайплайн](smm-pipeline.7w3.md)

## Wish

После финального «Окей» утверждённые материалы без участия Sardor становятся
публичными в YouTube, Дзене и Telegram в один target window. До первой public
операции любой общий сбой задерживает весь Выпуск; после частичного внешнего
сбоя система сохраняет успешное и повторяет только отсутствующее без дублей.

## Why

### Causes

Площадки не дают общей транзакции. YouTube поддерживает private upload, Telegram
Bot API не имеет отложенной отправки, а публичный официальный API публикации
статей Дзена не подтверждён. Ноутбук может спать или быть выключен.

### Intentions

Разделить непубличную подготовку и локально инициируемый public transition.
Сделать площадочные операции идемпотентными и не создавать native schedule,
который может сработать, пока локальный coordinator недоступен.

## What

Publication coordinator и три адаптера используют контракт:

- `prepare` создаёт private upload/draft/local payload;
- `validate` проверяет правильную версию, права, remote state и известные URL;
- `publish` переводит один подготовленный объект в public;
- `status` читает фактическое состояние;
- `cancel_prepare` отменяет ещё не public ресурс, когда площадка это позволяет;
- `retry_missing` повторяет только отсутствующий side effect с прежним
  idempotency key.

`schedule` в v1 означает durable job в SQLite и Windows Task Scheduler, а не
таймер внутри YouTube или Дзена.

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
3. определить, известен ли будущий Dzen material URL;
4. собрать Telegram caption с YouTube/Dzen links и проверить 1000 символов;
5. проверить Telegram-копию и channel rights;
6. выполнить общий prepare validation;
7. создать T−30 preflight task и T publish task;
8. в T−30 повторно проверить worker, сеть, credentials и prepared resources;
9. в T выполнить publication order, зависящий только от доступности Dzen URL;
10. наблюдать до `published`, `recovering`, `delayed` или `needs_attention`.

Если T−30 или T пропущены и ещё ничего не public, при следующем запуске весь
Выпуск становится delayed и не публикуется поздно самовольно.

## Method

YouTube:

- resumable upload с `privacyStatus=private`;
- дождаться processing complete, затем установить metadata и thumbnail;
- не задавать `status.publishAt` в v1;
- в target вызвать idempotent public transition и подтвердить API state.

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
- adapter создаёт draft, вставляет тот же Основной текст, оформление, cover и
  визуалы, затем сохраняет screenshot и DOM assertions;
- CAPTCHA/MFA не обходятся;
- capability tracer bullet обязан доказать draft identity, URL timing и
  проверку public state до production readiness.

Publication order:

- если Dzen URL известен до target, YouTube и Дзен переводятся в public с
  bounded concurrency, после подтверждения обоих отправляется Telegram;
- если URL появляется только после public, сначала публикуется Дзен, затем после
  получения URL публикуется YouTube и только после подтверждения обоих
  отправляется Telegram;
- если первая операция не стала public, общий сбой оставляет остальные private;
- после первого public результата любая ошибка переводит Выпуск в recovering;
- public content автоматически не удаляется.

Retry policy по умолчанию для временных provider errors: 30 секунд, 2 минуты,
5 минут и 15 минут. Permanent auth/permission/schema errors не повторяются.
После исчерпания создаётся incident и alert Sardor с ID 276042853.

## Boundaries

- Модуль не меняет утверждённый контент ради прохождения площадки.
- Публикация невозможна без совпадения hashes final approval.
- Native platform scheduling в v1 запрещён.
- Public material не удаляется автоматически.
- Adapter не обходит CAPTCHA, MFA или правила площадки.
- Sardor не является штатным публикационным оператором.

## Limitations

- Физическая атомарность трёх площадок невозможна; целевой visible drift — не
  более пяти минут, затем открывается incident.
- Выключенный Windows laptop не публикует и не отправляет alert до следующего
  запуска. Это безопасная задержка, а не гарантия высокой доступности.
- Dzen browser automation зависит от UI и является главным integration risk.
- Telegram-копия может не пройти одновременно size и readability gates; тогда
  release блокируется до отдельного design decision о Local Bot API Server.
- YouTube API project может требовать audit, прежде чем uploads смогут стать
  public.

## What's-next

Spec фиксирует state machine, URL-dependent publication order, Windows wake,
retry taxonomy и adapters. Первый implementation plan обязан начинаться с Dzen,
Telegram-size и Windows-wake tracer bullets до строительства всего пайплайна.

## Проверенные ограничения

- [YouTube Data API](https://developers.google.com/youtube/v3/docs/videos):
  `publishAt` применим только к private video, которое ещё не публиковалось; v1
  сознательно не использует его.
- [Telegram Bot Features](https://core.telegram.org/bots/features): cloud Bot API
  указывает upload limit 50 MB, local server — 2000 MB.
- В открытой официальной документации Дзена не найден подтверждённый article
  publishing API. Это результат поиска, а не утверждение об отсутствии
  партнёрского или закрытого интерфейса.
