-- Notification delivery validates its full durable request, including the
-- release identity. Backfill Slice 5 rows so an in-flight alert survives a
-- code upgrade without being mistaken for an invalid request.
UPDATE jobs
SET payload_json = json_set(payload_json, '$.release_id', release_id)
WHERE kind = 'notification_deliver'
  AND json_type(payload_json, '$.release_id') IS NULL;
