# Независимое финальное defect-review, попытка 1

Reviewer: `gpt-5.6-terra`, reasoning `xhigh`, новый чистый контекст.

## Findings

1. **HIGH:** media/calibration profile сам декларирует runtime/model hashes,
   crop profile отсутствует, поэтому production preconditions обходятся.
2. **HIGH:** pathname может быть заменён после watcher observation; staging
   проверяет открытый файл, но не его identity против принятого observation.
3. **HIGH:** ambiguity сохраняется, но нет команды выбора candidate_id; после
   reset оставленный файл считается старым.
4. **HIGH:** subprocess не имеют deadline/process cleanup, hung media не
   превращается в actionable issue.
5. **HIGH:** final approval допускает переход в publication_preparing без
   обязательного target time.
6. **MEDIUM:** editorial DTO недостаточно связывает factual spans и platform
   metadata с claims.
7. **MEDIUM:** blank/нечитаемый visual проходит только dimension check.
8. **MEDIUM:** QC не проверяет CFR, audio sample rate, faststart и обе стороны
   каждой границы transition.
9. **MEDIUM:** отсутствует disk admission `3× source + 2 GiB`.

Полные precise locations сохранены в результате agent run
`/root/slice3_final_independent_review`; все high findings приняты к исправлению.
