from pathlib import Path

from smm_agent.adapters.media.ffmpeg import FFmpegMediaTool
from smm_agent.contracts.video import TimelineEntry


def test_render_command_keeps_last_visual_until_end(tmp_path: Path) -> None:
    tool = FFmpegMediaTool(
        ffmpeg="ffmpeg", ffprobe="ffprobe", author_focal_x=0.25, author_focal_y=0.4
    )
    timeline = [
        TimelineEntry(visual_id="v1", start=30.0, end=100.0),
        TimelineEntry(visual_id="v2", start=100.0, end=450.0),
    ]

    command = tool.master_command(
        source=tmp_path / "source.mp4",
        visuals=[tmp_path / "v1.png", tmp_path / "v2.png"],
        timeline=timeline,
        output=tmp_path / "master.mp4",
    )
    graph = command[command.index("-filter_complex") + 1]

    assert "between(t,30.000,99.999999)" in graph
    assert "gte(t,100.000)" in graph
    assert "1920:1080" in graph
    assert "crop=960:1080:(iw-960)*0.25:(ih-1080)*0.4" in graph
    assert command[-1].endswith(".tmp.mp4")
