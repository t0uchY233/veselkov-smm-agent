"""Config-driven local worker composition and explicit diagnostic replay mode."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from smm_agent.adapters.media.ffmpeg import FFmpegMediaTool
from smm_agent.adapters.media.offline_asr import OfflineJsonRecognizer
from smm_agent.adapters.notification.replay import ReplayAlertTransport
from smm_agent.adapters.publishing.replay import ReplayPublisher
from smm_agent.adapters.secrets.secret_reader import WindowsCredentialSecretReader
from smm_agent.adapters.windows.live_bindings import WindowsLiveBindings
from smm_agent.application.capability_service import (
    RuntimeCapabilityUnavailable,
    require_worker_capabilities,
    validate_capabilities,
)
from smm_agent.application.provider_factory import (
    ProviderFactory,
    RuntimeCompositionError,
    SecretReader,
    WindowsProviderFactory,
)
from smm_agent.application.setup_service import require_accepted_media_profile
from smm_agent.contracts.video import AlignmentProfile
from smm_agent.platform.config import ConfigLoadError, SmmAgentConfig, configured_path, load_config
from smm_agent.platform.db import Database
from smm_agent.worker.publication_worker import PublicationTick, PublicationWorker
from smm_agent.worker.video_worker import VideoWorker, WorkerTick


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smm-worker")
    parser.add_argument(
        "--config",
        type=Path,
        help="Versioned Windows config.toml. This is the only live production mode.",
    )
    parser.add_argument(
        "--scheduled-task",
        help="Auditable Task Scheduler identity; it does not override durable due jobs.",
    )
    # Legacy explicit arguments remain diagnostic-only while migration to the
    # versioned config is completed. They may not silently create a live provider.
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--inbox", type=Path)
    parser.add_argument("--alignment-profile", type=Path)
    parser.add_argument("--asr-executable", type=Path)
    parser.add_argument("--asr-model", type=Path)
    parser.add_argument("--calibration-corpus", type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--poll-seconds", default=5, type=float)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--publication-replay",
        action="store_true",
        help="Explicit diagnostic mode only; never inferred for config-driven production startup.",
    )
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _executable(value: str) -> Path:
    resolved = shutil.which(value)
    if resolved is None:
        raise RuntimeCompositionError(f"executable not found: {value}")
    return Path(resolved).resolve()


def _verify_profile_paths(
    *,
    profile: AlignmentProfile,
    asr_executable: Path,
    asr_model: Path,
    calibration_corpus: Path,
    ffmpeg: Path,
    ffprobe: Path,
) -> tuple[Path, Path]:
    expected = {
        asr_executable: profile.asr_executable_sha256,
        asr_model: profile.model_sha256,
        calibration_corpus: profile.corpus_sha256,
        ffmpeg: profile.ffmpeg_sha256,
        ffprobe: profile.ffprobe_sha256,
    }
    for path, digest in expected.items():
        if not path.is_file() or _sha256(path) != digest:
            raise RuntimeCompositionError(f"runtime file does not match accepted profile: {path}")
    command_paths = {
        Path(item).resolve() for item in profile.asr_argv_template if item != "{source}"
    }
    if Path(profile.asr_argv_template[0]).resolve() != asr_executable.resolve():
        raise RuntimeCompositionError("ASR command must use the verified executable.")
    if asr_model.resolve() not in command_paths:
        raise RuntimeCompositionError("ASR command must use the verified model.")
    return ffmpeg, ffprobe


def _legacy_verified_paths(
    args: argparse.Namespace, profile: AlignmentProfile
) -> tuple[Path, Path]:
    required = (
        args.asr_executable,
        args.asr_model,
        args.calibration_corpus,
        args.alignment_profile,
        args.inbox,
        args.data_root,
    )
    if any(value is None for value in required):
        raise RuntimeCompositionError(
            "Укажите --config либо полный диагностический набор media arguments."
        )
    return _verify_profile_paths(
        profile=profile,
        asr_executable=cast(Path, args.asr_executable).resolve(),
        asr_model=cast(Path, args.asr_model).resolve(),
        calibration_corpus=cast(Path, args.calibration_corpus).resolve(),
        ffmpeg=_executable(args.ffmpeg),
        ffprobe=_executable(args.ffprobe),
    )


def _config_verified_paths(
    config: SmmAgentConfig, profile: AlignmentProfile
) -> tuple[Path, Path]:
    asr_command = profile.asr_argv_template
    return _verify_profile_paths(
        profile=profile,
        asr_executable=Path(asr_command[0]),
        asr_model=configured_path(config.media.asr_asset),
        calibration_corpus=configured_path(config.media.calibration_corpus),
        ffmpeg=configured_path(config.media.ffmpeg_path),
        ffprobe=configured_path(config.media.ffprobe_path),
    )


def _tick_payload(tick: WorkerTick) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "outcome": tick.outcome,
        "candidates": list(tick.candidates),
        "release": tick.release.model_dump(mode="json", by_alias=True) if tick.release else None,
    }


def _publication_tick_payload(tick: PublicationTick) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "outcome": tick.outcome,
        "publication": tick.publication.model_dump(mode="json") if tick.publication else None,
    }


@dataclass(slots=True)
class WorkerRuntime:
    database: Database
    video_worker: VideoWorker
    publication_worker: PublicationWorker | None
    scheduled_task: str | None = None

    def run_once(self) -> dict[str, object]:
        if self.publication_worker is not None:
            publication_tick = self.publication_worker.run_once()
            if publication_tick.outcome != "idle":
                payload = _publication_tick_payload(publication_tick)
                if self.scheduled_task is not None:
                    payload["scheduled_task"] = self.scheduled_task
                return payload
        payload = _tick_payload(self.video_worker.run_once())
        if self.scheduled_task is not None:
            payload["scheduled_task"] = self.scheduled_task
        return payload


class WorkerRuntimeFactory(Protocol):
    """Composition seam: production binds live providers, tests bind fakes."""

    def create(self, args: argparse.Namespace) -> WorkerRuntime: ...


class DefaultWorkerRuntimeFactory:
    def __init__(
        self,
        *,
        provider_factory: ProviderFactory | None = None,
        secret_reader: SecretReader | None = None,
    ) -> None:
        self._provider_factory = provider_factory or WindowsProviderFactory(
            bindings=WindowsLiveBindings()
        )
        self._secret_reader = secret_reader or WindowsCredentialSecretReader()

    def create(self, args: argparse.Namespace) -> WorkerRuntime:
        if args.config is not None:
            return self._configured(args)
        return self._legacy_diagnostic(args)

    def _configured(self, args: argparse.Namespace) -> WorkerRuntime:
        config_path = cast(Path, args.config)
        try:
            config = load_config(config_path)
        except ConfigLoadError as error:
            raise RuntimeCompositionError(str(error)) from error
        report = validate_capabilities(config, config_path=config_path)
        require_worker_capabilities(report)

        database = Database(configured_path(config.runtime.data_root))
        database.initialize()
        profile = AlignmentProfile.model_validate_json(
            configured_path(config.media.crop_profile).read_text(encoding="utf-8")
        )
        require_accepted_media_profile(database, profile)
        ffmpeg, ffprobe = _config_verified_paths(config, profile)
        video_worker = _video_worker(
            database=database,
            inbox=configured_path(config.files.recording_inbox),
            profile=profile,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
        )
        if args.publication_replay:
            publication_worker = _replay_publication_worker(database)
        else:
            runtime = self._provider_factory.create(
                config=config,
                database=database,
                secrets=self._secret_reader,
            )
            publication_worker = PublicationWorker(
                database=database,
                publishers=dict(runtime.publishers),
                alert_transport=runtime.alert_transport,
            )
        return WorkerRuntime(
            database=database,
            video_worker=video_worker,
            publication_worker=publication_worker,
            scheduled_task=args.scheduled_task,
        )

    def _legacy_diagnostic(self, args: argparse.Namespace) -> WorkerRuntime:
        if args.scheduled_task is not None:
            raise RuntimeCompositionError("--scheduled-task требует --config.")
        profile_path = cast(Path | None, args.alignment_profile)
        if profile_path is None:
            raise RuntimeCompositionError(
                "Live worker требует --config; diagnostic mode требует --alignment-profile."
            )
        profile = AlignmentProfile.model_validate_json(profile_path.read_text(encoding="utf-8"))
        data_root = cast(Path | None, args.data_root)
        inbox = cast(Path | None, args.inbox)
        if data_root is None or inbox is None:
            raise RuntimeCompositionError("Diagnostic worker требует --data-root и --inbox.")
        database = Database(data_root.resolve())
        database.initialize()
        require_accepted_media_profile(database, profile)
        ffmpeg, ffprobe = _legacy_verified_paths(args, profile)
        return WorkerRuntime(
            database=database,
            video_worker=_video_worker(
                database=database,
                inbox=inbox,
                profile=profile,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
            ),
            publication_worker=(
                _replay_publication_worker(database) if args.publication_replay else None
            ),
        )


def _video_worker(
    *,
    database: Database,
    inbox: Path,
    profile: AlignmentProfile,
    ffmpeg: Path,
    ffprobe: Path,
) -> VideoWorker:
    return VideoWorker(
        database=database,
        inbox=inbox,
        alignment_profile=profile,
        recognizer=OfflineJsonRecognizer(
            tuple(profile.asr_argv_template),
            runtime_id=profile.asr_runtime,
            model_sha256=profile.model_sha256,
        ),
        media_tool=FFmpegMediaTool(
            ffmpeg=str(ffmpeg),
            ffprobe=str(ffprobe),
            author_focal_x=profile.author_focal_x,
            author_focal_y=profile.author_focal_y,
        ),
        stable_seconds=30,
    )


def _replay_publication_worker(database: Database) -> PublicationWorker:
    return PublicationWorker(
        database=database,
        publishers={
            "youtube": ReplayPublisher(
                "youtube", state_path=database.data_root / "state/replay-youtube.json"
            ),
            "dzen": ReplayPublisher(
                "dzen", state_path=database.data_root / "state/replay-dzen.json"
            ),
            "telegram": ReplayPublisher(
                "telegram", state_path=database.data_root / "state/replay-telegram.json"
            ),
        },
        alert_transport=ReplayAlertTransport(
            state_path=database.data_root / "state/replay-alerts.json"
        ),
    )


def main(
    argv: list[str] | None = None,
    *,
    runtime_factory: WorkerRuntimeFactory | None = None,
) -> None:
    args = _parser().parse_args(argv)
    if args.poll_seconds <= 0:
        raise SystemExit("--poll-seconds должен быть положительным.")
    try:
        runtime = (runtime_factory or DefaultWorkerRuntimeFactory()).create(args)
    except (
        OSError,
        RuntimeCompositionError,
        RuntimeCapabilityUnavailable,
        ValueError,
    ) as error:
        raise SystemExit(f"smm-worker startup blocked: {error}") from error

    if args.once:
        print(json.dumps(runtime.run_once(), ensure_ascii=False))
        return
    while True:
        runtime.run_once()
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
