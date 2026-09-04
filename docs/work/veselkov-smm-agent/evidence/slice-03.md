# Evidence Slice 3

Дата: 2026-09-04. UC: UC-06, UC-07, UC-15. Статус: реализован, ожидает
человеческого принятия риска.

## Наблюдаемое поведение

- SQLite хранит recording window, immutable candidates/recordings, approval
  membership и media lease.
- Worker после рестарта продолжает 30-секундную stabilization; старый файл не
  принимается, файл после approval принимается даже до первого poll.
- Ingest открывает source без follow, сверяет identity со стабильным observation,
  копирует и проверяет его вне writer transaction; SQLite регистрирует уже
  fsync-нутый immutable artifact короткой транзакцией.
- Только явно принятый оператором и сохранённый в trusted setup state профиль
  открывает offline ASR alignment. Worker сверяет SHA-256 ASR/model/corpus и
  FFmpeg/ffprobe, использует принятые argv/crop и блокирует произвольный profile.
  Первое
  уверенное совпадение каждого anchor образует монотонный timeline.
- FFmpeg выдаёт H.264/AAC Full HD: до первого anchor ведущий fullscreen, затем
  ведущий слева и visual справа; visual действует до следующего, последний до
  последнего кадра.
- Telegram-копия повторяет монтаж, проходит frame-fidelity QC и ограничена
  49 000 000 bytes.
- Ambiguity, invalid source, low confidence и media failure сохраняют
  `video_issue` и actionable `needs_attention` вместо падения daemon.
- Все внешние media процессы имеют deadline; POSIX process group и Windows
  kill-on-close Job Object завершают дерево. Render attempts имеют уникальные
  work paths, renewable lease и проверку срока/владельца перед commit.
- Final approve невозможен без будущего target; точное время хранится в UTC с
  `Europe/Moscow`, а назначение времени перевыпускает hash-bound final gate.
- Final manifest и approval включают весь текущий комплект. Plan, editorial и
  final approvals повторно сверяют точные member IDs, hashes и bytes.

## Верификация

Все команды запускались строго последовательно через `codex-test-guard`.

- Финальный полный gate:
  `/root/.local/bin/codex-test-guard --timeout 20m -- .venv/bin/pytest` —
  **48 passed in 366.97 s (0:06:06)**.
- Финальный lint gate:
  `/root/.local/bin/codex-test-guard --timeout 10m -- .venv/bin/ruff check src tests tools`
  — **All checks passed**.
- Финальный type gate:
  `/root/.local/bin/codex-test-guard --timeout 10m -- .venv/bin/mypy src` —
  **Success, 42 source files**.
- Последний быстрый preflight до full gate: **45 passed in 21.44 s**.
- Реальный FFmpeg 3-second transition/decode/QC smoke проходит.
- Реальный 450-second GC-02 smoke входит в обязательный full suite: master
  1920×1080, CFR 30, AAC 48 kHz, faststart, boundary frames, full decode,
  Telegram encode и предел 49 MB; тест не opt-in.

Среда evidence: Python 3.12.3; FFmpeg/ffprobe 6.1.1-3ubuntu5; pytest 8.4.2;
Ruff 0.16.5; mypy 1.20.2. Базовый commit до Slice 3: `7eea85c`.

## Независимое review

- Все независимые проходы выполнены `gpt-5.6-terra`, reasoning `xhigh`, без
  правки файлов и без запуска тестов reviewer-агентом.
- Несколько свежих контекстов последовательно обнаружили дефекты ingest,
  approvals, recovery, process cleanup, lease и migration. Подтверждённые
  critical/high исправлены и покрыты регрессиями.
- Финальная точечная перепроверка двух последних HIGH дала **PASS**: legacy
  pending gates безопасно переоткрываются, а Автор может отозвать запись во
  время монтажа; stale renderer не может commit.
- Отчёты и финальный verdict сохранены в `reviews/slice-03/`.

## Отложенная проверка среды

Calibration corpus/model, Windows Job Object и crop/readability нельзя честно
принять на Linux fixture. Их capability smoke и benchmark выполняются на
Windows 11 ноутбуке в Slice 6; без принятого profile production worker не
запускается.
