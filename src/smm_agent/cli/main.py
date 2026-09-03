"""Narrow JSON-only CLI used by Codex and operators."""

import json
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from pydantic import BaseModel

from smm_agent.application.release_service import (
    IdempotencyConflict,
    ReleaseNotFound,
    StateConflict,
    create_release,
    get_release_status,
    utc_request_id,
)
from smm_agent.contracts.cli import ErrorDetail, ErrorResponse
from smm_agent.domain.release import ReleaseConflict
from smm_agent.platform.config import default_data_root
from smm_agent.platform.db import Database

app = typer.Typer(add_completion=False, no_args_is_help=True)
release_app = typer.Typer(add_completion=False, no_args_is_help=True)
app.add_typer(release_app, name="release")


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
    raise typer.Exit(code=2)


def _database(data_root: Path | None) -> Database:
    database = Database((data_root or default_data_root()).resolve())
    database.initialize()
    return database


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


def main() -> None:
    try:
        app()
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
