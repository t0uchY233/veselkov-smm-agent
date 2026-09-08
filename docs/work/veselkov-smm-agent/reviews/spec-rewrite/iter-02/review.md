# Self-review после первого rewrite

Независимый reviewer недоступен; это собственный defect pass и не заменяет
cross-family review.

## Findings

1. **High · spec 0.2.0, Channel/Deploy.** Формулировка «no user data» неверна
   для публичного repository: там намеренно находятся имя Автора, публичные
   каналы и TOV. Нужно запретить именно runtime, credentials и unpublished
   release content, а публичные продуктовые документы назвать сознательным
   исключением.
2. **High · Publication order.** Telegram может появиться раньше, чем ссылки
   стали публично доступны. Telegram должен всегда быть последним: сначала
   подтвердить public YouTube и Dzen, затем отправлять video+caption.
3. **High · Deploy.** Не определено, где на ноутбуке находится Codex workspace и
   как сверяется его skill с установленным worker. Нужен stable workspace path и
   doctor check `workspace commit == app commit`.
4. **Medium · Error contract.** Есть только один пример ошибки, но нет каталога
   кодов, достаточного для UI и тестов.
5. **Medium · Schedule.** Thursday 14:00 существует только в design и golden
   case, но не закреплён как алгоритм default target в spec.
6. **Medium · Final gate.** Не перечислена Telegram-копия, хотя Автор должен
   увидеть итоговый platform encode до разрешения publication.

## Gate

Исправить все шесть findings до следующего compliance pass.
