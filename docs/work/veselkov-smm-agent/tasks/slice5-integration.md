# Slice 5: интеграция worker, recovery и alert outbox

## Реализованный путь

- Worker берёт каждую publication job только через fenced `JobClaim` и держит
  heartbeat каждые 20 секунд, пока выполняется внешний вызов. Потерянная lease
  не может завершить или переназначить job старого владельца.
- Target-time Telegram job сначала сверяет все три receipts. Если хотя бы одна
  площадка не подтверждена или Telegram временно недоступен, он в одной SQLite
  транзакции сохраняет известные receipts, ставит Выпуск в `recovering`, создаёт
  ровно один `publication_recovery` с исходными task/remote/idempotency/operation
  identities, после чего fenced-завершает исходный target job.
- Recovery вызывается только для активного `recovering` Выпуска. Его terminal
  callback открывает incident и notification job в той же транзакции, что
  `RecoveryExhausted.v1` и `needs_attention`; recipient неизменно `276042853`.
- В replay mode worker использует persistent `ReplayAlertTransport`, поэтому
  restart сохраняет lookup-before-send receipt технического уведомления.

## Осознанная граница

`publication_cancel_reconcile` пока не исполняется worker-ом. Он остаётся
durable записью для человеческого решения в `needs_attention`: автоматическое
продолжение здесь опасно, потому что перед cancellation нужно status-first
сверить все receipts и запретить любое удаление после первого `public`. Этот
узкий policy будет добавлен вместе с production capability checks, где можно
проверить реальные provider semantics. Текущий worker никогда не запускает
автоматическую cancellation из этого job.

## Подготовленная верификация

`tests/integration/test_recovery_pipeline.py` покрывает:

- GC-04: public YouTube/Дзен, временно недоступный Telegram, затем recovery
  только Telegram без prepare/arm и без смены remote identities;
- terminal recovery: один incident и один alert job для `276042853`, а сбой
  alert delivery сохраняет локальный incident;
- crash после удалённого Telegram side effect: следующий fenced attempt видит
  public receipt и не вызывает второй `execute`.

Тесты, Ruff и Mypy намеренно ещё не запускались: ожидается выделенный
единственный guarded test slot от координатора.
