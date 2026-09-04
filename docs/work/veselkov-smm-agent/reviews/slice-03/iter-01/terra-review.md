# Независимое defect-review Slice 3

Reviewer: `gpt-5.6-terra`, reasoning `xhigh`, fresh context. Артефакт во время
review был заморожен. Ниже сохранены полученные findings; решение об исправлении
принимает builder и затем проверяет повторным review.

## Findings

1. **HIGH:** baseline-файлы со статусом `rejected` на следующем poll снова
   переводятся в `stabilizing`, а затем могут быть приняты.
   Locations: `worker/video_worker.py:70`, `platform/video_store.py:149,188`,
   `adapters/files/recording_watcher.py:38`.
2. **HIGH:** ambiguity остаётся только в `WorkerTick`, low-confidence завершает
   daemon исключением; Codex не получает устойчивого actionable state.
   Locations: `worker/video_worker.py:59,91,113`, `worker/main.py:59`,
   `domain/video/service.py:98`.
3. **HIGH:** final approval hash включает только новые media artifacts, но не
   весь финальный пакет; перед решением bytes повторно не сверяются.
   Locations: `application/video_service.py:255,311,319`,
   `application/editorial_service.py:376`.
4. **HIGH:** между probe/hash исходника и повторным чтением для импорта возможна
   замена inbox-файла (TOCTOU).
   Locations: `application/video_service.py:100-129`,
   `application/editorial_artifacts.py:69`.
5. **HIGH:** детерминированный temp с режимом `xb` после crash блокирует retry.
   Locations: `application/editorial_artifacts.py:67-70`,
   `application/video_service.py:124,250`.
6. **HIGH:** production QC проверяет контейнер и decode, но не проверяет
   timeline, непрерывность и правильный visual в правой панели.
   Locations: `adapters/media/ffmpeg.py:191-200`, media tests.
7. **HIGH:** после rejected final gate или замены записи нет полного recovery
   path, а unique valid recording препятствует новой записи.
   Locations: `application/editorial_service.py:395,492`,
   `application/video_service.py:128`, `migrations/0003_video.sql:32`.
8. **HIGH:** alignment выбирает глобально лучший поздний повтор, а не первое
   уверенное совпадение.
   Locations: `domain/video/service.py:69-80`.
9. **HIGH:** произвольное значение threshold позволяет обойти обязательный
   calibration gate; профиль модели/corpus не фиксируется.
   Locations: `worker/main.py:18`, `worker/video_worker.py:34`,
   `domain/video/service.py:52`.
10. **MEDIUM:** отсутствует `RecordingAccepted.v1`, а render event не совпадает
    с каноническим `VideoValidated.v1` и не перечисляет media/QC IDs.
    Locations: `application/video_service.py:139,338`, `spec.md:218`.
11. **MEDIUM:** regression test generated schemas не охватывает новые video
    contracts.
    Locations: `tools/generate_contracts.py:15`,
    `tests/contract/test_generated_schemas.py:10`.
