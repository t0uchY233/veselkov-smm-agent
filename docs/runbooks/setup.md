# Локальная подготовка Windows 11

Этот runbook готовит детерминированный local foundation. Он не публикует
материалы сам по себе и не подтверждает production readiness. Реальные
YouTube, Dzen, Telegram и wake-from-sleep smoke остаются отдельными capability
gates на изолированных non-production ресурсах.

## 1. Выбрать ресурсы на ноутбуке

На ноутбуке Сергея Николаевича Sardor выбирает и создаёт все пути сам. Агент не
сканирует диск и не подставляет `%USERPROFILE%`, `~` или переменные среды.

- `recording_inbox`: одна папка, куда Сергей Николаевич сохраняет исходное видео.
- `portrait_reference_dir`: папка утверждённых референсов Сергея Николаевича.
- `dzen.browser_profile`: выделенный browser profile для ручной Dzen-сессии.
- `data_root`: закрытая runtime-папка SQLite и артефактов.
- `backup_root`: отдельная backup-папка; её отсутствие не мешает обычному Выпуску,
  но блокирует production update.
- `crop_profile`, ASR asset, calibration corpus, FFmpeg и FFprobe: точные,
  уже принятые runtime-файлы.

Не помещайте browser profile, видео, runtime DB или портреты в Git workspace.

## 2. Создать config без секретов

Скопируйте [пример](../../config/smm-agent.example.toml) в
`config/smm-agent.toml`. Реальный файл уже исключён `.gitignore`.

Замените каждый путь с `CONFIGURE` на абсолютный путь. В config запрещены
пароли, Telegram bot token, OAuth JSON, cookies и переменные окружения. Для
YouTube и Telegram указывается только ссылка формата
`windows-credential:<target>`.

В `schedule.run_as_user` укажите Windows setup account явно. Он должен
совпадать с account, под которым запускаются setup и worker. Укажите также
явный `schedule.worker_executable` и `schedule.task_credential_ref` для
Task Scheduler; последнее хранит только `windows-credential:<target>`, не
сам пароль.

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
заблокирован. `localFoundationReady` подтверждает только локальные пути, ссылки
на credential, current/task account и ACL защищённых папок.
`windows.task_scheduler` остаётся `warning` до отдельной регистрации и wake
smoke. Поле `productionReadiness` остаётся `not_assessed` до live smoke площадок
и реальной проверки wake from sleep.

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

## 5. Worker и планировщик

Обычный запуск использует только versioned config; replay никогда не включается
автоматически:

```powershell
smm-worker --config .\config\smm-agent.toml --once
```

Если хотя бы один обязательный local capability или live provider factory
недоступен, запуск завершится до обработки job. `--publication-replay` является
отдельным явно указанным диагностическим режимом и не заменяет live adapters.

`adapters.windows.task_scheduler` формирует связанные с `release_id` XML plans
для T−30 и T с:

- UTC time trigger;
- `WakeToRun=true`;
- `LogonType=Password`, чтобы задача могла стартовать при logged-off user;
- безопасным детерминированным именем задачи на Выпуск;
- точным argv `smm-worker --config … --once --scheduled-task …` без shell.

Регистрация и удаление выполняются только явным вызовом Windows adapter через
`schtasks.exe` с list argv, XML и password из injected Credential Manager port.
Они не выполняются unit-тестами. До успешного non-production registration/wake
smoke планировщик не считается production-ready.

## 6. Non-production capability smoke

Безопасный отчёт без side effect:

```powershell
smmctl capability smoke --config .\config\smm-agent.toml
```

Отдельные harness-команды: `youtube`, `dzen`, `telegram`, `scheduler`. Они
выполняют live probe только с явным `--execute` и только против подготовленных
test channel/private upload/Dzen draft/test task. JSON содержит ограниченные
redacted evidence и всегда оставляет `productionReadiness: "blocked"`: зелёный
smoke является доказательством для Sardor, а не автоматическим разрешением на
публикацию.
