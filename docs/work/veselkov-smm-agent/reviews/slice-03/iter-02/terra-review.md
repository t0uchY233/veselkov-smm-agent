# Независимое повторное defect-review Slice 3

Reviewer: `gpt-5.6-terra`, reasoning `xhigh`, новый чистый контекст. Артефакт
во время review был заморожен.

## Findings

1. **HIGH:** повторная запись под тем же именем повторно использует immutable
   candidate_id и нарушает unique constraint recordings.candidate_id.
2. **HIGH:** ingest failures получают remediation target `video`, хотя требуют
   новой `recording`, после чего разрешённый recovery невозможен.
3. **HIGH:** full-bundle import для узкой правки cover/metadata/Telegram может
   незаметно заменить main text, visuals и claims без editorial gate.
4. **HIGH:** bytes проверяются только на final gate; plan/editorial approval
   можно провести после изменения утверждавшегося файла.
5. **HIGH:** inbox baseline фиксируется на первом poll, а не в момент открытия
   recording window; корректная запись, сохранённая при остановленном worker,
   может быть ошибочно признана старой.
6. **HIGH:** два worker-процесса могут одновременно рендерить одну revision в
   одинаковые work paths; DB serialization защищает commit, но не FFmpeg bytes.
7. **MEDIUM:** длинный 7:30 media smoke opt-in и не входит в обычный media gate.

Точные исходные locations и полный текст reviewer доступны в истории agent
run `/root/slice3_independent_rereview`; этот файл фиксирует неизменённый смысл
всех findings для review-and-fix loop.
