import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from smm_agent.adapters.media.ffmpeg import FFmpegMediaTool
from smm_agent.contracts.video import TimelineEntry

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg binaries are unavailable",
)


def run(*command: str) -> None:
    subprocess.run(command, capture_output=True, check=True)


def frame(video: Path, at: float, output: Path) -> Image.Image:
    run(
        "ffmpeg",
        "-y",
        "-ss",
        str(at),
        "-i",
        str(video),
        "-frames:v",
        "1",
        str(output),
    )
    with Image.open(output) as image:
        return image.convert("RGB")


def dominant(pixel: tuple[int, int, int]) -> int:
    return max(range(3), key=lambda index: pixel[index])


def test_real_ffmpeg_render_has_fullscreen_then_continuous_visual_relay(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    run(
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=320x240:r=30:d=3",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=3",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(source),
    )
    visuals = []
    for name, color in (("red", "red"), ("green", "green"), ("yellow", "yellow")):
        path = tmp_path / f"{name}.png"
        Image.new("RGB", (1080, 1080), color=color).save(path)
        visuals.append(path)
    timeline = [
        TimelineEntry(visual_id="red", start=0.5, end=1.5),
        TimelineEntry(visual_id="green", start=1.5, end=2.5),
        TimelineEntry(visual_id="yellow", start=2.5, end=3.0),
    ]
    master = tmp_path / "master.mp4"
    tool = FFmpegMediaTool(ffmpeg="ffmpeg", ffprobe="ffprobe")

    tool.render_master(source=source, visuals=visuals, timeline=timeline, output=master)
    probe = tool.probe(master)
    qc = tool.validate_master(
        master,
        probe.duration_seconds,
        source=source,
        visuals=visuals,
        timeline=timeline,
    )

    assert (probe.width, probe.height) == (1920, 1080)
    assert probe.audio_streams >= 1
    assert qc.passed, qc.checks
    expected_right_channels = [(0.25, 2), (0.75, 0), (1.75, 1), (2.75, 0)]
    for index, (timestamp, expected_channel) in enumerate(expected_right_channels):
        image = frame(master, timestamp, tmp_path / f"frame-{index}.png")
        assert dominant(image.getpixel((1500, 540))) == expected_channel
        assert dominant(image.getpixel((480, 540))) == 2

    telegram = tmp_path / "telegram.mp4"
    tool.render_telegram(master=master, output=telegram, max_bytes=49_000_000)
    telegram_qc = tool.validate_telegram(
        telegram,
        probe.duration_seconds,
        49_000_000,
        master=master,
        timeline=timeline,
    )
    assert telegram_qc.passed, telegram_qc.checks


def test_real_seven_and_a_half_minute_recording_produces_both_encodes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-450s.mp4"
    run(
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=320x240:r=30:d=450",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=450",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(source),
    )
    visuals = []
    for name, color in (("red", "red"), ("green", "green"), ("yellow", "yellow")):
        path = tmp_path / f"long-{name}.png"
        Image.new("RGB", (1080, 1080), color=color).save(path)
        visuals.append(path)
    timeline = [
        TimelineEntry(visual_id="red", start=30.0, end=180.0),
        TimelineEntry(visual_id="green", start=180.0, end=330.0),
        TimelineEntry(visual_id="yellow", start=330.0, end=450.0),
    ]
    tool = FFmpegMediaTool(ffmpeg="ffmpeg", ffprobe="ffprobe")
    master = tmp_path / "master-450s.mp4"
    telegram = tmp_path / "telegram-450s.mp4"

    tool.render_master(source=source, visuals=visuals, timeline=timeline, output=master)
    master_qc = tool.validate_master(
        master, 450.0, source=source, visuals=visuals, timeline=timeline
    )
    tool.render_telegram(master=master, output=telegram, max_bytes=49_000_000)
    telegram_qc = tool.validate_telegram(
        telegram, 450.0, 49_000_000, master=master, timeline=timeline
    )

    assert master_qc.passed, master_qc.checks
    assert telegram_qc.passed, telegram_qc.checks
    assert telegram.stat().st_size <= 49_000_000
