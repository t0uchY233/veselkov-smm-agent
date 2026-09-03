# Compliance pass по требованиям владельца

## Findings

1. **High · lifecycle/final gate.** Target time ошибочно включён в content
   approval. Изменение времени не меняет видео или текст и не должно вынуждать
   повторно утверждать контент. Final gate должен требовать наличие target, но
   hash approval охватывает только content artifacts.
2. **Medium · Telegram count.** «Unicode code points» недостаточно надёжно для
   emoji/entities. Validator должен ограничить и Unicode scalar count, и UTF-16
   code units значением 1000.
3. **Medium · visuals.** В rewrite потерян явный master-size 1080×1080 для
   повторного использования одного asset в Дзене и правой половине видео.
4. **Medium · success.** Основная бизнес-метрика Sardor = 0 routine minutes есть
   только косвенно в analytics, но не оформлена acceptance rule.

## Gate

Все четыре findings подлежат исправлению.

