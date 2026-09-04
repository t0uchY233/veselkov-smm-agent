"""FFmpeg/ffprobe implementation of deterministic video rendering."""

import json
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageChops, ImageOps, ImageStat

from smm_agent.adapters.media.process import CommandTimedOut, run_bounded
from smm_agent.contracts.video import MediaProbe, TimelineEntry, VideoQc


class MediaCommandFailed(RuntimeError):
    pass


class FFmpegMediaTool:
    def __init__(
        self,
        *,
        ffmpeg: str,
        ffprobe: str,
        timeout_seconds: float = 1800,
        author_focal_x: float = 0.5,
        author_focal_y: float = 0.5,
    ) -> None:
        if not 0 <= author_focal_x <= 1 or not 0 <= author_focal_y <= 1:
            raise ValueError("author focal point must be between zero and one")
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.timeout_seconds = timeout_seconds
        self.author_focal_x = author_focal_x
        self.author_focal_y = author_focal_y

    @staticmethod
    def _temporary(output: Path) -> Path:
        return output.with_name(f"{output.stem}.tmp{output.suffix}")

    def probe(self, source: Path) -> MediaProbe:
        completed = self._run(
            [
                self.ffprobe,
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(source),
            ]
        )
        payload = json.loads(completed.stdout)
        streams = payload.get("streams", [])
        videos = [item for item in streams if item.get("codec_type") == "video"]
        audios = [item for item in streams if item.get("codec_type") == "audio"]
        if not videos:
            raise MediaCommandFailed("ffprobe did not find a video stream")
        video = videos[0]
        rate = Fraction(str(video.get("avg_frame_rate", "0/1")))
        real_rate = Fraction(str(video.get("r_frame_rate", "0/1")))
        return MediaProbe(
            duration_seconds=float(payload["format"]["duration"]),
            video_streams=len(videos),
            audio_streams=len(audios),
            width=int(video["width"]),
            height=int(video["height"]),
            frame_rate=float(rate),
            real_frame_rate=float(real_rate),
            audio_sample_rate=(int(audios[0]["sample_rate"]) if audios else None),
            video_codec=str(video.get("codec_name")) if video.get("codec_name") else None,
            audio_codec=(str(audios[0].get("codec_name")) if audios else None),
        )

    def master_command(
        self,
        *,
        source: Path,
        visuals: list[Path],
        timeline: list[TimelineEntry],
        output: Path,
    ) -> list[str]:
        if len(visuals) != len(timeline) or not visuals:
            raise ValueError("visuals and timeline must have the same non-zero length")
        command = [self.ffmpeg, "-y", "-i", str(source)]
        for visual in visuals:
            command.extend(["-loop", "1", "-i", str(visual)])
        filters = [
            "[0:v]split=2[source_full][source_left]",
            "[source_full]scale=1920:1080:force_original_aspect_ratio=increase,"
            f"crop=1920:1080:(iw-1920)*{self.author_focal_x}:"
            f"(ih-1080)*{self.author_focal_y},fps=30,setsar=1[fullscreen]",
            "[source_left]scale=960:1080:force_original_aspect_ratio=increase,"
            f"crop=960:1080:(iw-960)*{self.author_focal_x}:"
            f"(ih-1080)*{self.author_focal_y},fps=30,setsar=1,"
            f"split={len(visuals)}"
            + "".join(f"[left{index}]" for index in range(len(visuals))),
        ]
        for index in range(len(visuals)):
            filters.append(
                f"[{index + 1}:v]scale=960:960:force_original_aspect_ratio=decrease,"
                f"pad=960:1080:0:60:color=white,setsar=1[visual{index}]"
            )
            filters.append(f"[left{index}][visual{index}]hstack=inputs=2[panel{index}]")
        previous = "fullscreen"
        for index, entry in enumerate(timeline):
            condition = (
                f"gte(t,{entry.start:.3f})"
                if index == len(timeline) - 1
                else f"between(t,{entry.start:.3f},{entry.end - 0.000001:.6f})"
            )
            output_label = f"composite{index}"
            filters.append(
                f"[{previous}][panel{index}]overlay=0:0:enable='{condition}'[{output_label}]"
            )
            previous = output_label
        temporary = self._temporary(output)
        command.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                f"[{previous}]",
                "-map",
                "0:a:0",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-r",
                "30",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-ar",
                "48000",
                "-movflags",
                "+faststart",
                "-shortest",
                str(temporary),
            ]
        )
        return command

    def render_master(
        self,
        *,
        source: Path,
        visuals: list[Path],
        timeline: list[TimelineEntry],
        output: Path,
    ) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._temporary(output)
        command = self.master_command(
            source=source, visuals=visuals, timeline=timeline, output=output
        )
        self._run(command)
        temporary.replace(output)

    def render_telegram(self, *, master: Path, output: Path, max_bytes: int) -> None:
        duration = self.probe(master).duration_seconds
        audio_bitrate = 96_000
        video_bitrate = max(180_000, int(max_bytes * 8 * 0.92 / duration) - audio_bitrate)
        temporary = self._temporary(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                self.ffmpeg,
                "-y",
                "-i",
                str(master),
                "-vf",
                "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
                "-c:v",
                "libx264",
                "-preset",
                "slow",
                "-b:v",
                str(video_bitrate),
                "-maxrate",
                str(video_bitrate),
                "-bufsize",
                str(video_bitrate * 2),
                "-r",
                "30",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-ar",
                "48000",
                "-movflags",
                "+faststart",
                str(temporary),
            ]
        )
        if temporary.stat().st_size > max_bytes:
            temporary.unlink(missing_ok=True)
            raise MediaCommandFailed("Telegram encode exceeds the configured byte limit")
        temporary.replace(output)

    def validate_master(
        self,
        path: Path,
        expected_duration: float,
        *,
        source: Path,
        visuals: list[Path],
        timeline: list[TimelineEntry],
    ) -> VideoQc:
        qc = self._validate(
            path, expected_duration, expected_size=(1920, 1080), max_bytes=None
        )
        layout = self._validate_master_layout(path, source, visuals, timeline)
        checks = {**qc.checks, **layout}
        return qc.model_copy(update={"passed": all(checks.values()), "checks": checks})

    def validate_telegram(
        self,
        path: Path,
        expected_duration: float,
        max_bytes: int,
        *,
        master: Path,
        timeline: list[TimelineEntry],
    ) -> VideoQc:
        qc = self._validate(
            path, expected_duration, expected_size=(1280, 720), max_bytes=max_bytes
        )
        fidelity = self._validate_telegram_fidelity(path, master, timeline)
        checks = {**qc.checks, "visual_readability": fidelity}
        return qc.model_copy(update={"passed": all(checks.values()), "checks": checks})

    @staticmethod
    def _difference(left: Image.Image, right: Image.Image) -> float:
        difference = ImageChops.difference(left.convert("RGB"), right.convert("RGB"))
        return sum(ImageStat.Stat(difference).rms) / (3 * 255)

    def _frame(self, video: Path, timestamp: float, output: Path) -> Image.Image:
        self._run(
            [
                self.ffmpeg,
                "-y",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                str(output),
            ]
        )
        with Image.open(output) as image:
            return image.convert("RGB")

    def _validate_master_layout(
        self,
        master: Path,
        source: Path,
        visuals: list[Path],
        timeline: list[TimelineEntry],
    ) -> dict[str, bool]:
        if len(visuals) != len(timeline) or not timeline:
            return {"fullscreen_before_anchor": False, "visual_timeline": False}
        with tempfile.TemporaryDirectory(prefix="smm-qc-") as directory:
            root = Path(directory)
            before = max(0.0, timeline[0].start / 2)
            master_before = self._frame(master, before, root / "master-before.png")
            source_before = self._frame(source, before, root / "source-before.png")
            expected_full = ImageOps.fit(
                source_before,
                (1920, 1080),
                method=Image.Resampling.LANCZOS,
                centering=(self.author_focal_x, self.author_focal_y),
            )
            fullscreen_ok = self._difference(master_before, expected_full) <= 0.08
            timeline_ok = True
            samples: list[tuple[Path, float]] = []
            for visual, entry in zip(visuals, timeline, strict=True):
                duration = entry.end - entry.start
                offsets = (
                    min(0.08, duration / 3),
                    duration / 2,
                    max(duration - 0.08, duration / 2),
                )
                samples.extend((visual, entry.start + offset) for offset in offsets)
            for index, (visual, timestamp) in enumerate(samples):
                rendered = self._frame(master, timestamp, root / f"master-{index}.png")
                right = rendered.crop((960, 0, 1920, 1080))
                with Image.open(visual) as visual_image:
                    expected = ImageOps.contain(
                        visual_image.convert("RGB"), (960, 960), Image.Resampling.LANCZOS
                    )
                panel = Image.new("RGB", (960, 1080), "white")
                panel.paste(expected, ((960 - expected.width) // 2, (1080 - expected.height) // 2))
                left_mean = sum(ImageStat.Stat(rendered.crop((0, 0, 960, 1080))).mean) / 3
                visual_matches = self._difference(right, panel) <= 0.12
                timeline_ok = timeline_ok and left_mean > 5 and visual_matches
        return {"fullscreen_before_anchor": fullscreen_ok, "visual_timeline": timeline_ok}

    def _validate_telegram_fidelity(
        self, telegram: Path, master: Path, timeline: list[TimelineEntry]
    ) -> bool:
        if not timeline:
            return False
        sample_times = [timeline[0].start / 2]
        sample_times.extend((entry.start + entry.end) / 2 for entry in timeline)
        sample_times.append(max(timeline[-1].start, timeline[-1].end - 0.1))
        with tempfile.TemporaryDirectory(prefix="smm-qc-") as directory:
            root = Path(directory)
            for index, timestamp in enumerate(sample_times):
                telegram_frame = self._frame(
                    telegram, timestamp, root / f"telegram-{index}.png"
                )
                master_frame = self._frame(master, timestamp, root / f"master-{index}.png")
                expected = master_frame.resize((1280, 720), Image.Resampling.LANCZOS)
                if self._difference(telegram_frame, expected) > 0.12:
                    return False
        return True

    def _validate(
        self,
        path: Path,
        expected_duration: float,
        *,
        expected_size: tuple[int, int],
        max_bytes: int | None,
    ) -> VideoQc:
        probe = self.probe(path)
        decoded = self._run([self.ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"])
        with path.open("rb") as stream:
            header = stream.read(16 * 1024 * 1024)
        moov = header.find(b"moov")
        mdat = header.find(b"mdat")
        checks = {
            "duration": abs(probe.duration_seconds - expected_duration) <= 0.2,
            "resolution": (probe.width, probe.height) == expected_size,
            "video_h264": probe.video_codec == "h264",
            "audio_aac": probe.audio_codec == "aac",
            "cfr_30": (
                abs(probe.frame_rate - 30) <= 0.01
                and probe.real_frame_rate is not None
                and abs(probe.real_frame_rate - 30) <= 0.01
            ),
            "audio_48khz": probe.audio_sample_rate == 48_000,
            "faststart": 0 <= moov < mdat,
            "decoded": decoded.returncode == 0,
            "size": max_bytes is None or path.stat().st_size <= max_bytes,
        }
        return VideoQc(
            passed=all(checks.values()),
            duration_seconds=probe.duration_seconds,
            width=probe.width,
            height=probe.height,
            video_codec=probe.video_codec or "",
            audio_codec=probe.audio_codec or "",
            decoded=checks["decoded"],
            size_bytes=path.stat().st_size,
            checks=checks,
        )

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            completed = run_bounded(command, timeout_seconds=self.timeout_seconds)
        except CommandTimedOut as error:
            raise MediaCommandFailed(f"media {error}") from error
        if completed.returncode != 0:
            detail = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else ""
            raise MediaCommandFailed(f"media command failed ({completed.returncode}): {detail}")
        return completed
