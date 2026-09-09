CREATE TABLE telegram_alert_receipts (
    notification_id TEXT PRIMARY KEY,
    recipient TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    receipt_json TEXT
);
