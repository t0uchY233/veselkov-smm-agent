"""Executable local media worker; installation and supervision arrive in Slice 6."""

import argparse
import hashlib
import json
import shutil
import time
from pathlib import Path

from smm_agent.adapters.media.ffmpeg import FFmpegMediaTool
from smm_agent.adapters.media.offline_asr import OfflineJsonRecognizer
from smm_agent.adapters.notification.replay import ReplayAlertTransport
from smm_agent.adapters.publishing.replay import ReplayPublisher
from smm_agent.application.setup_service import require_accepted_media_profile
from smm_agent.contracts.video import AlignmentProfile
from smm_agent.platform.db import Database
from smm_agent.worker.publication_worker import PublicationTick, PublicationWorker
from smm_agent.worker.video_worker import VideoWorker, WorkerTick


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smm-worker")
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--inbox", required=True, type=Path)
    parser.add_argument("--alignment-profile", required=True, type=Path)
    parser.add_argument("--asr-executable", required=True, type=Path)
    parser.add_argument("--asr-model", required=True, type=Path)
    parser.add_argument("--calibration-corpus", required=True, type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--poll-seconds", default=5, type=float)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--publication-replay",
        action="store_true",
        help="Run publication coordination with non-network replay adapters.",
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
        raise SystemExit(f"executable not found: {value}")
    return Path(resolved).resolve()


def _verify_profile(args: argparse.Namespace, profile: AlignmentProfile) -> tuple[Path, Path]:
    asr = args.asr_executable.resolve()
    model = args.asr_model.resolve()
    corpus = args.calibration_corpus.resolve()
    ffmpeg = _executable(args.ffmpeg)
    ffprobe = _executable(args.ffprobe)
    expected = {
        asr: profile.asr_executable_sha256,
        model: profile.model_sha256,
        corpus: profile.corpus_sha256,
        ffmpeg: profile.ffmpeg_sha256,
        ffprobe: profile.ffprobe_sha256,
    }
    for path, digest in expected.items():
        if not path.is_file() or _sha256(path) != digest:
            raise SystemExit(f"runtime file does not match accepted profile: {path}")
    return ffmpeg, ffprobe


def _tick_payload(tick: WorkerTick) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "outcome": tick.outcome,
        "candidates": list(tick.candidates),
        "release": (
            tick.release.model_dump(mode="json", by_alias=True) if tick.release else None
        ),
    }


def _publication_tick_payload(tick: PublicationTick) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "outcome": tick.outcome,
        "publication": (
            tick.publication.model_dump(mode="json") if tick.publication else None
        ),
    }


def main() -> None:
    args = _parser().parse_args()
    database = Database(args.data_root.resolve())
    database.initialize()
    alignment_profile = AlignmentProfile.model_validate_json(
        args.alignment_profile.read_text(encoding="utf-8")
    )
    require_accepted_media_profile(database, alignment_profile)
    ffmpeg, ffprobe = _verify_profile(args, alignment_profile)
    asr_command = alignment_profile.asr_argv_template
    asr_executable = args.asr_executable.resolve()
    asr_model = args.asr_model.resolve()
    command_paths = {Path(item).resolve() for item in asr_command if item != "{source}"}
    if Path(asr_command[0]).resolve() != asr_executable or asr_model not in command_paths:
        raise SystemExit("ASR command must use the verified executable and model")
    worker = VideoWorker(
        database=database,
        inbox=args.inbox,
        alignment_profile=alignment_profile,
        recognizer=OfflineJsonRecognizer(
            tuple(asr_command),
            runtime_id=alignment_profile.asr_runtime,
            model_sha256=alignment_profile.model_sha256,
        ),
        media_tool=FFmpegMediaTool(
            ffmpeg=str(ffmpeg),
            ffprobe=str(ffprobe),
            author_focal_x=alignment_profile.author_focal_x,
            author_focal_y=alignment_profile.author_focal_y,
        ),
        stable_seconds=30,
    )
    publication_worker = (
        PublicationWorker(
            database=database,
            publishers={
                "youtube": ReplayPublisher(
                    "youtube", state_path=database.data_root / "state/replay-youtube.json"
                ),
                "dzen": ReplayPublisher(
                    "dzen", state_path=database.data_root / "state/replay-dzen.json"
                ),
                "telegram": ReplayPublisher(
                    "telegram",
                    state_path=database.data_root / "state/replay-telegram.json",
                ),
            },
            alert_transport=ReplayAlertTransport(
                state_path=database.data_root / "state/replay-alerts.json"
            ),
        )
        if args.publication_replay
        else None
    )

    def run_once() -> dict[str, object]:
        if publication_worker is not None:
            publication_tick = publication_worker.run_once()
            if publication_tick.outcome != "idle":
                return _publication_tick_payload(publication_tick)
        return _tick_payload(worker.run_once())

    if args.once:
        print(json.dumps(run_once(), ensure_ascii=False))
        return
    while True:
        run_once()
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
