"""Narrow JSON-only CLI used by Codex and operators."""

import hashlib
import json
import os
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from pydantic import BaseModel, ValidationError
from typer._click.exceptions import ClickException

from smm_agent.adapters.editorial.docx_builder import PythonDocxBuilder
from smm_agent.adapters.editorial.image_inspector import PillowImageInspector
from smm_agent.adapters.media.ffmpeg import FFmpegMediaTool
from smm_agent.application.capability_service import validate_capabilities
from smm_agent.application.editorial_service import (
    decide_gate,
    import_editorial,
    import_plan,
    request_revision,
    set_publication_target,
)
from smm_agent.application.release_service import (
    IdempotencyConflict,
    ReleaseNotFound,
    StateConflict,
    create_release,
    get_release_status,
    utc_request_id,
)
from smm_agent.application.setup_service import accept_media_profile
from smm_agent.application.video_service import select_recording_candidate
from smm_agent.contracts.cli import ErrorDetail, ErrorResponse
from smm_agent.contracts.editorial import EditorialBundle, PlanDocument
from smm_agent.contracts.setup import DzenLoginHandoff
from smm_agent.contracts.video import AlignmentProfile
from smm_agent.domain.release import ReleaseConflict
from smm_agent.platform.config import ConfigLoadError, default_data_root, load_config
from smm_agent.platform.db import Database

app = typer.Typer(add_completion=False, no_args_is_help=True)
release_app = typer.Typer(add_completion=False, no_args_is_help=True)
setup_app = typer.Typer(add_completion=False, no_args_is_help=True)
setup_login_app = typer.Typer(add_completion=False, no_args_is_help=True)
app.add_typer(release_app, name="release")
app.add_typer(setup_app, name="setup")
setup_app.add_typer(setup_login_app, name="login")


class GateChoice(StrEnum):
    plan = "plan"
    editorial = "editorial"
    final = "final"


class DecisionChoice(StrEnum):
    approved = "approved"
    rejected = "rejected"


def _emit(model: BaseModel) -> None:
    typer.echo(model.model_dump_json(by_alias=True))


def _fail(code: str, message: str, hint: str, **details: object) -> NoReturn:
    _emit(
        ErrorResponse(
            error=ErrorDetail(
                code=code,
                message=message,
                hint=hint,
                requestId=utc_request_id(),
                details=details,
            )
        )
    )
    raise SystemExit(2)


def _database(data_root: Path | None) -> Database:
    database = Database((data_root or default_data_root()).resolve())
    database.initialize()
    return database


def _editorial_policy_hashes() -> tuple[str, str]:
    project_root = Path(__file__).parents[3]
    tone_hash = hashlib.sha256((project_root / "tone-of-voice.md").read_bytes()).hexdigest()
    lock = json.loads((project_root / "skills-lock.json").read_text(encoding="utf-8"))
    humanizer_hash = str(lock["skills"]["humanizer-ru"]["computedHash"])
    return tone_hash, humanizer_hash


def _validation_failure(error: Exception) -> NoReturn:
    if isinstance(error, ValidationError):
        details: dict[str, object] = {
            "errorCount": error.error_count(),
            "issues": [item["msg"] for item in error.errors(include_context=False)[:10]],
        }
        message = "Редакционный файл не прошёл проверку."
    else:
        details = {}
        message = str(error)
    _fail("VALIDATION_FAILED", message, "Исправьте указанные данные и повторите импорт.", **details)


def _mutation_failure(error: Exception, command_id: str) -> NoReturn:
    if isinstance(error, IdempotencyConflict):
        _fail(
            "IDEMPOTENCY_CONFLICT",
            "Этот command_id уже использован с другими данными.",
            "Создайте новый command_id или повторите исходную команду без изменений.",
            commandId=command_id,
        )
    if isinstance(error, StateConflict):
        _fail(
            "STATE_CONFLICT",
            "Команда не соответствует текущему этапу Выпуска.",
            "Обновите статус и выполните показанное следующее действие.",
            reason=str(error),
        )
    _validation_failure(error)


@setup_app.command("accept-media-profile")
def setup_accept_media_profile(
    profile_path: Annotated[
        Path, typer.Option("--profile", exists=True, dir_okay=False)
    ],
    confirmation: Annotated[str, typer.Option("--confirmation")],
    actor: Annotated[str, typer.Option("--actor")] = "operator",
    data_root: Annotated[Path | None, typer.Option("--data-root")] = None,
) -> None:
    """Persist explicit operator acceptance of one exact machine profile."""
    try:
        profile = AlignmentProfile.model_validate_json(
            profile_path.read_text(encoding="utf-8")
        )
        result = accept_media_profile(
            _database(data_root),
            profile=profile,
            actor=actor,
            confirmation=confirmation,
        )
    except (ValidationError, ValueError, UnicodeDecodeError, OSError) as error:
        _validation_failure(error)
    else:
        _emit(result)


@setup_app.command("validate")
def setup_validate(
    config: Annotated[Path, typer.Option("--config", dir_okay=False)],
) -> None:
    """Validate explicit local setup without publishing or registering a task."""
    try:
        configured = load_config(config)
    except ConfigLoadError as error:
        _fail(
            "VALIDATION_FAILED",
            str(error),
            "Исправьте config/smm-agent.toml; secret values в config не допускаются.",
        )
    else:
        _emit(validate_capabilities(configured, config_path=config))


@setup_login_app.command("dzen")
def setup_login_dzen(
    config: Annotated[Path, typer.Option("--config", dir_okay=False)],
) -> None:
    """Hand off a one-time Dzen session to a visible Windows browser and its user."""
    try:
        configured = load_config(config)
    except ConfigLoadError as error:
        _fail(
            "VALIDATION_FAILED",
            str(error),
            "Исправьте config/smm-agent.toml before the headful Dzen login.",
        )

    if os.name != "nt":
        _emit(
            DzenLoginHandoff(
                state="unavailable",
                browserProfile=configured.dzen.browser_profile,
                message="Dzen login требует headful browser на Windows; текущий host не Windows.",
                nextAction=(
                    "Запустите эту команду на ноутбуке Сергея Николаевича под setup account."
                ),
            )
        )
        raise typer.Exit(2)

    _emit(
        DzenLoginHandoff(
            state="requires_headful_windows",
            browserProfile=configured.dzen.browser_profile,
            message=(
                "Откройте видимый Dzen browser profile и выполните вход лично. "
                "Команда не читает пароль, не обходит MFA и не запускает headless login."
            ),
            nextAction=(
                "После ручного входа выполните live Dzen smoke из следующего Slice 6 шага."
            ),
        )
    )


@release_app.command("start")
def release_start(
    command_id: Annotated[str, typer.Option("--command-id")],
    topic_file: Annotated[Path, typer.Option("--topic-file", exists=True, dir_okay=False)],
    actor: Annotated[str, typer.Option("--actor")] = "codex",
    expected_revision: Annotated[int, typer.Option("--expected-revision", min=0)] = 0,
    data_root: Annotated[Path | None, typer.Option("--data-root")] = None,
) -> None:
    """UC-02: create the only active release from a UTF-8 topic file."""
    try:
        topic = topic_file.read_text(encoding="utf-8")
        result = create_release(
            _database(data_root),
            command_id=command_id,
            topic=topic,
            actor=actor,
            expected_revision=expected_revision,
        )
    except UnicodeDecodeError:
        _fail("VALIDATION_FAILED", "Файл темы должен быть UTF-8.", "Пересохраните файл в UTF-8.")
    except ValueError as error:
        _fail("VALIDATION_FAILED", str(error), "Укажите непустую тему выпуска.")
    except IdempotencyConflict:
        _fail(
            "IDEMPOTENCY_CONFLICT",
            "Этот command_id уже использован с другими данными.",
            "Создайте новый command_id или повторите исходную команду без изменений.",
            commandId=command_id,
        )
    except StateConflict as error:
        _fail(
            "STATE_CONFLICT",
            "Ожидаемая версия не совпадает с допустимой.",
            "Обновите статус и повторите команду для показанной версии.",
            reason=str(error),
        )
    except ReleaseConflict as error:
        _fail(
            "STATE_CONFLICT",
            "Уже существует активный выпуск.",
            "Завершите текущий выпуск или запросите его статус.",
            activeReleaseId=str(error),
        )
    else:
        _emit(result)


@release_app.command("status")
def release_status(
    release_id: Annotated[str | None, typer.Option("--release-id")] = None,
    data_root: Annotated[Path | None, typer.Option("--data-root")] = None,
) -> None:
    """UC-13: report one release and its next action."""
    try:
        result = get_release_status(_database(data_root), release_id)
    except ReleaseNotFound:
        _fail(
            "RELEASE_NOT_FOUND",
            "Выпуск не найден.",
            "Начните новый выпуск или проверьте release_id.",
            releaseId=release_id,
        )
    else:
        _emit(result)


@release_app.command("import-plan")
def release_import_plan(
    command_id: Annotated[str, typer.Option("--command-id")],
    expected_revision: Annotated[int, typer.Option("--expected-revision", min=1)],
    file: Annotated[Path, typer.Option("--file", exists=True, dir_okay=False)],
    actor: Annotated[str, typer.Option("--actor")] = "codex",
    data_root: Annotated[Path | None, typer.Option("--data-root")] = None,
) -> None:
    """UC-02/03: import a validated plan and open the plan gate."""
    try:
        plan = PlanDocument.model_validate_json(file.read_text(encoding="utf-8"))
        result = import_plan(
            _database(data_root),
            command_id=command_id,
            expected_revision=expected_revision,
            actor=actor,
            plan=plan,
        )
    except (ValidationError, ValueError, UnicodeDecodeError) as error:
        _validation_failure(error)
    except (IdempotencyConflict, StateConflict) as error:
        _mutation_failure(error, command_id)
    else:
        _emit(result)


@release_app.command("import-editorial")
def release_import_editorial(
    command_id: Annotated[str, typer.Option("--command-id")],
    expected_revision: Annotated[int, typer.Option("--expected-revision", min=1)],
    bundle: Annotated[Path, typer.Option("--bundle", exists=True, file_okay=False)],
    actor: Annotated[str, typer.Option("--actor")] = "codex",
    data_root: Annotated[Path | None, typer.Option("--data-root")] = None,
) -> None:
    """UC-04: import one complete editorial package and open its gate."""
    try:
        manifest = bundle / "manifest.json"
        document = EditorialBundle.model_validate_json(manifest.read_text(encoding="utf-8"))
        tone_hash, humanizer_hash = _editorial_policy_hashes()
        result = import_editorial(
            _database(data_root),
            command_id=command_id,
            expected_revision=expected_revision,
            actor=actor,
            bundle_root=bundle,
            bundle=document,
            docx_builder=PythonDocxBuilder(),
            image_inspector=PillowImageInspector(),
            tone_of_voice_sha256=tone_hash,
            humanizer_skill_version=humanizer_hash,
        )
    except (ValidationError, ValueError, UnicodeDecodeError, OSError) as error:
        _validation_failure(error)
    except (IdempotencyConflict, StateConflict) as error:
        _mutation_failure(error, command_id)
    else:
        _emit(result)


@release_app.command("decide")
def release_decide(
    command_id: Annotated[str, typer.Option("--command-id")],
    expected_revision: Annotated[int, typer.Option("--expected-revision", min=1)],
    gate: Annotated[GateChoice, typer.Option("--gate")],
    decision: Annotated[DecisionChoice, typer.Option("--decision")],
    actor: Annotated[str, typer.Option("--actor")],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    data_root: Annotated[Path | None, typer.Option("--data-root")] = None,
) -> None:
    """UC-03/05/08: record the Author's decision over one shown gate."""
    try:
        result = decide_gate(
            _database(data_root),
            command_id=command_id,
            expected_revision=expected_revision,
            actor=actor,
            gate=gate.value,
            decision=decision.value,
            reason=reason,
        )
    except (IdempotencyConflict, StateConflict, ValueError) as error:
        _mutation_failure(error, command_id)
    else:
        _emit(result)


@release_app.command("revise")
def release_revise(
    command_id: Annotated[str, typer.Option("--command-id")],
    expected_revision: Annotated[int, typer.Option("--expected-revision", min=1)],
    target: Annotated[str, typer.Option("--target")],
    reason: Annotated[str, typer.Option("--reason")],
    actor: Annotated[str, typer.Option("--actor")],
    data_root: Annotated[Path | None, typer.Option("--data-root")] = None,
) -> None:
    """UC-03/05/15: request a new artifact version and invalidate dependants."""
    try:
        result = request_revision(
            _database(data_root),
            command_id=command_id,
            expected_revision=expected_revision,
            actor=actor,
            target=target,
            reason=reason,
        )
    except (IdempotencyConflict, StateConflict, ValueError) as error:
        _mutation_failure(error, command_id)
    else:
        _emit(result)


@release_app.command("select-recording")
def release_select_recording(
    command_id: Annotated[str, typer.Option("--command-id")],
    expected_revision: Annotated[int, typer.Option("--expected-revision", min=1)],
    candidate_id: Annotated[str, typer.Option("--candidate-id")],
    inbox: Annotated[Path, typer.Option("--inbox", exists=True, file_okay=False)],
    actor: Annotated[str, typer.Option("--actor")],
    ffmpeg: Annotated[str, typer.Option("--ffmpeg")] = "ffmpeg",
    ffprobe: Annotated[str, typer.Option("--ffprobe")] = "ffprobe",
    data_root: Annotated[Path | None, typer.Option("--data-root")] = None,
) -> None:
    """UC-06: bind the Author's explicit choice to one ambiguous candidate."""
    try:
        result = select_recording_candidate(
            _database(data_root),
            command_id=command_id,
            expected_revision=expected_revision,
            actor=actor,
            candidate_id=candidate_id,
            inbox=inbox,
            media_tool=FFmpegMediaTool(ffmpeg=ffmpeg, ffprobe=ffprobe),
        )
    except (IdempotencyConflict, StateConflict, ValueError, OSError) as error:
        _mutation_failure(error, command_id)
    else:
        _emit(result)


@release_app.command("set-target")
def release_set_target(
    command_id: Annotated[str, typer.Option("--command-id")],
    expected_revision: Annotated[int, typer.Option("--expected-revision", min=1)],
    target_at: Annotated[str, typer.Option("--target-at")],
    actor: Annotated[str, typer.Option("--actor")],
    timezone: Annotated[str, typer.Option("--timezone")] = "Europe/Moscow",
    data_root: Annotated[Path | None, typer.Option("--data-root")] = None,
) -> None:
    """UC-08: bind a future publication time before final approval."""
    try:
        result = set_publication_target(
            _database(data_root),
            command_id=command_id,
            expected_revision=expected_revision,
            actor=actor,
            target_at=target_at,
            timezone=timezone,
        )
    except (IdempotencyConflict, StateConflict, ValueError) as error:
        _mutation_failure(error, command_id)
    else:
        _emit(result)


def main() -> None:
    try:
        app(standalone_mode=False)
    except typer.Exit as error:
        raise SystemExit(error.exit_code) from None
    except ClickException as error:
        _emit(
            ErrorResponse(
                error=ErrorDetail(
                    code="VALIDATION_FAILED",
                    message="Команда или её параметры указаны неверно.",
                    hint="Исправьте команду по описанию smmctl.",
                    requestId=utc_request_id(),
                    details={"reason": error.format_message()},
                )
            )
        )
        raise SystemExit(2) from None
    except Exception as error:
        print(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "Не удалось выполнить команду.",
                        "hint": "Сохраните diagnostics и обратитесь к Sardor.",
                        "requestId": utc_request_id(),
                        "details": {"type": type(error).__name__},
                    },
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
