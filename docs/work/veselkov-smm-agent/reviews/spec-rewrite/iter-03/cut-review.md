# Parsimony cut pass

## Drift verdict

Spec всё ещё несёт следы enterprise-шаблона: event delivery и универсальный
artifact graph добавлены поверх уже существующей SQLite job queue. Для одного
локального процесса это два механизма одного назначения.

## Solid/fragile map

- Solid: один SQLite state, immutable artifact bytes, CLI boundary, три platform
  adapters, Windows Task Scheduler wake.
- Fragile: outbox dispatcher/consumer dedupe поверх jobs; generic dependency DAG
  для фиксированного набора artifact kinds.

## Reframes

1. События — только immutable audit facts, не внутренний bus. Удаляются
   dispatcher и consumed-events bookkeeping. Jobs остаются единственным
   механизмом фонового исполнения.
2. Dependency invalidation — фиксированная таблица правил domain layer. Общая
   графовая подсистема не нужна в v1.

## Calibrate

Не сокращаются: три человеческих gates, durable jobs, platform idempotency,
immutable approvals/artifacts и partial-failure recovery. Это сложность самой
задачи, а не архитектурный налёт.

