# Slice 5: аварийные уведомления

## Граница

Этот узел создаёт `incident`, `notification` и durable job в одной SQLite
транзакции. Доставка всегда сначала ищет receipt по `notification_id`, затем
делает отправку. Поэтому аварийное завершение после успешного Bot API вызова не
создаёт повторный alert.

## Инварианты

- recipient технического alert неизменно `276042853`;
- body содержит выпуск, площадку, closed-contract error code, UTC-время и одно
  безопасное действие; provider detail и секреты в него не попадают;
- интервалы retry: 1, 5, 15 минут; затем notification `failed`, incident остаётся
  `open`;
- все записи attempt и fenced job transition выполняются в одной транзакции.

## Проверка

До интеграции worker: `tests/integration/test_notifications.py` проверяет
атомарное создание, suppression и lookup-before-send/recovery после crash point.
