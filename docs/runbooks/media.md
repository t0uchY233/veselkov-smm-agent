# Локальный media worker

Slice 3 предоставляет `smm-worker`: он наблюдает только настроенную inbox-папку,
принимает один стабильный MP4/MOV/MKV длительностью 5–10 минут, запускает
локальный ASR, строит timeline и создаёт master и Telegram-копию.

До Windows setup нужно подготовить принятый calibration profile. Произвольный
threshold не принимается. Профиль фиксирует runtime, SHA-256 исполняемых файлов
ASR/FFmpeg/ffprobe, SHA-256 ASR-модели и calibration corpus, принятую точку
кадрирования лица, порог, оператора и время принятия. Worker пересчитывает все
хеши до запуска. Принятый argv template хранится внутри самого профиля: он должен
использовать именно проверенные executable и model, работать локально, содержать
литерал `{source}` и вернуть в stdout JSON
контракта `video-transcript.v1.json`. FFmpeg и ASR имеют 30-минутный deadline;
при превышении worker завершает дерево процесса и создаёт `video_issue`.

Сначала оператор один раз принимает точный профиль в trusted setup state:

```powershell
smmctl setup accept-media-profile `
  --data-root "$env:LOCALAPPDATA\VeselkovSmm" `
  --profile ".\alignment-profile.json" `
  --confirmation "ПРИНИМАЮ ПРОФИЛЬ МОНТАЖА"
```

Произвольный файл из аргумента worker не считается принятым и запуск блокируется.
После принятия доступен диагностический одиночный poll:

```powershell
smm-worker --once `
  --data-root "$env:LOCALAPPDATA\VeselkovSmm" `
  --inbox "D:\VeselkovRecordingInbox" `
  --alignment-profile ".\alignment-profile.json" `
  --asr-executable ".\tools\whisper-wrapper.exe" `
  --asr-model ".\models\whisper.bin" `
  --calibration-corpus ".\calibration\veselkov.json" `
  --ffmpeg ".\tools\ffmpeg.exe" `
  --ffprobe ".\tools\ffprobe.exe"
```

Нельзя указывать весь диск или личную папку как inbox. Файлы, существовавшие до
открытия recording window, не принимаются. Несколько новых записей, невалидная
длительность, низкая confidence или сбой QC создают `video_issue` и переводят
Выпуск в `needs_attention`; случайный выбор и скрытое снижение качества запрещены.

Полный Windows service/Task Scheduler setup, закреплённые FFmpeg/ASR binaries и
benchmark на ноутбуке входят в Slice 6.

При двух стабильных файлах worker показывает безопасные имена и `candidate_id`.
После выбора Автором Codex вызывает `smmctl release select-recording`; случайный
файл не выбирается. Перед финальным approve Codex обязан вызвать
`smmctl release set-target --target-at <ISO-8601>`: без будущего времени gate
остаётся закрытым.
