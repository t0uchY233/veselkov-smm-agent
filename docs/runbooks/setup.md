# Локальная подготовка Windows 11

Этот runbook готовит детерминированный local foundation. Он не публикует
материалы, не регистрирует Task Scheduler tasks и не подтверждает production
readiness. Реальные YouTube, Dzen, Telegram и wake-from-sleep smoke остаются
отдельными capability gates.

## 1. Выбрать ресурсы на ноутбуке

На ноутбуке Сергея Николаевича Sardor выбирает и создаёт все пути сам. Агент не
сканирует диск и не подставляет `%USERPROFILE%`, `~` или переменные среды.

- `recording_inbox`: одна папка, куда Сергей Николаевич сохраняет исходное видео.
- `portrait_reference_dir`: папка утверждённых референсов Сергея Николаевича.
- `dzen.browser_profile`: выделенный browser profile для ручной Dzen-сессии.
- `data_root`: закрытая runtime-папка SQLite и артефактов.
- `backup_root`: отдельная backup-папка; её отсутствие не мешает обычному Выпуску,
  но блокирует production update.
- `crop_profile`, ASR asset и FFmpeg: точные, уже принятые runtime-файлы.

Не помещайте browser profile, видео, runtime DB или портреты в Git workspace.

## 2. Создать config без секретов

Скопируйте [пример](../../config/smm-agent.example.toml) в
`config/smm-agent.toml`. Реальный файл уже исключён `.gitignore`.

Замените каждый путь с `CONFIGURE` на абсолютный путь. В config запрещены
пароли, Telegram bot token, OAuth JSON, cookies и переменные окружения. Для
YouTube и Telegram указывается только ссылка формата
`windows-credential:<target>`.

В `schedule.run_as_user` укажите Windows setup account явно. Его используют
будущие Task Scheduler registration steps для запуска при logged-off user; его
нельзя угадывать по имени профиля или `%USERNAME%`.

Секреты добавляются в **Windows Credential Manager** текущего setup account с
точными target-именами из config. Валидатор читает только факт наличия записи:
значение secret не попадает в config, JSON-отчёт или лог.

## 3. Проверить local foundation

На ноутбуке выполните:

```powershell
smmctl setup validate --config .\config\smm-agent.toml
```

Команда возвращает versioned JSON. Исправьте каждый `unavailable`; `warning` по
`backup.backup_root` означает, что обычный Выпуск возможен, но update production
заблокирован. `localFoundationReady` подтверждает только локальные пути,
ссылки на credential и возможность сформировать Windows task XML. Поле
`productionReadiness` остаётся `not_assessed` до live smoke площадок и реальной
проверки wake from sleep.

На non-Windows машине отчёт намеренно показывает Windows Credential Manager и
Task Scheduler как `unavailable`; это не заменяет проверку на целевом ноутбуке.

## 4. Передать Dzen вход человеку

```powershell
smmctl setup login dzen --config .\config\smm-agent.toml
```

Эта команда никогда не читает пароль, не обходит MFA и не делает headless
login. На Windows она выдаёт hand-off: Sardor открывает **видимый** browser
profile из config и лично выполняет вход в Dzen. Затем отдельный Slice 6 live
smoke подтвердит selectors и стабильную ссылку отложенной статьи.

## 5. Планировщик: пока только спецификация

`adapters.windows.task_scheduler` формирует XML для Task Scheduler с:

- UTC time trigger;
- `WakeToRun=true`;
- `LogonType=Password`, чтобы задача могла стартовать при logged-off user;
- безопасным детерминированным именем задачи на Выпуск;
- корректным quoting argv без shell.

На этом срезе модуль сознательно не вызывает `schtasks.exe`, не регистрирует
задачу и не утверждает, что ноутбук проснётся. Installer и реальный sleep/wake
smoke будут отдельным проверяемым шагом.
