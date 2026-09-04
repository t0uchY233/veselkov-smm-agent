import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from PIL import Image


def run_cli(project_root: Path, data_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(project_root / "src")
    return subprocess.run(
        [sys.executable, "-m", "smm_agent.cli.main", *args, "--data-root", str(data_root)],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def response(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.stderr == ""
    return json.loads(result.stdout)


def start_release(project_root: Path, data_root: Path, tmp_path: Path) -> dict[str, Any]:
    topic = tmp_path / "topic.txt"
    topic.write_text("Как договор замораживает деньги", encoding="utf-8")
    result = run_cli(
        project_root,
        data_root,
        "release",
        "start",
        "--command-id",
        "start-1",
        "--topic-file",
        str(topic),
    )
    assert result.returncode == 0
    return response(result)


def write_plan(tmp_path: Path) -> Path:
    path = tmp_path / "plan.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "title": "КОНТРАКТ ЕСТЬ, А ДЕНЕГ НЕТ",
                "thesis": "Работа превращается в деньги после документов и срока оплаты.",
                "sections": ["Проблема", "Механизм", "Расчёт", "Действия"],
                "target_duration_minutes": 7.5,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def write_editorial_bundle(tmp_path: Path, *, visual_count: int = 3) -> Path:
    bundle = tmp_path / "editorial"
    bundle.mkdir(exist_ok=True)
    anchors = ["АКТ ФИКСИРУЕТ ДОЛГ", "СРОК ЗАПУСКАЕТ ОПЛАТУ", "ДЕНЬГИ В ОБОРОТЕ"]
    filler = " ".join(f"слово{i}" for i in range(430))
    main_text = f"{anchors[0]}. {filler} {anchors[1]}. {anchors[2]}."
    output_hash = hashlib.sha256(main_text.encode("utf-8")).hexdigest()
    project_root = Path(__file__).parents[2]
    tone_hash = hashlib.sha256((project_root / "tone-of-voice.md").read_bytes()).hexdigest()
    skills_lock = json.loads((project_root / "skills-lock.json").read_text(encoding="utf-8"))

    visuals = []
    for index, anchor in enumerate(anchors[:visual_count], start=1):
        asset = bundle / f"visual-{index}.png"
        Image.new("RGB", (1080, 1080), color=(245, 245, 245)).save(asset)
        visuals.append(
            {
                "visual_id": f"visual-{index}",
                "kind": ["table", "chart", "image"][index - 1],
                "anchor_text": anchor,
                "purpose": "Показывает финансовый смысл этапа.",
                "asset_path": asset.name,
                "caption": f"Визуал {index}",
                "claim_ids": ["claim-1"],
            }
        )

    cover = bundle / "cover.png"
    Image.new("RGB", (1280, 720), color=(255, 255, 255)).save(cover)
    manifest = {
        "schema_version": "1.0",
        "main_text": main_text,
        "sources": [
            {
                "source_id": "source-1",
                "url": "https://example.com/source",
                "title": "Источник",
                "publisher": "Example",
                "checked_at": "2026-09-04T08:00:00Z",
                "evidence_excerpt_hash": hashlib.sha256(b"evidence excerpt").hexdigest(),
            }
        ],
        "claims": [
            {
                "claim_id": "claim-1",
                "exact_text": "Документы подтверждают требование оплаты.",
                "materiality": "material",
                "status": "supported",
                "source_ids": ["source-1"],
            }
        ],
        "visuals": visuals,
        "cover_path": cover.name,
        "telegram": {
            "title": "КОНТРАКТ ЕСТЬ, А ДЕНЕГ НЕТ",
            "lead": "Выполненная работа ещё не означает оплату.",
            "steps": ["Фиксируй результат", "Собирай документы", "Ставь срок оплаты"],
            "cta": (
                "Читайте подробнее в [блоге]({{dzen_url}}) "
                "и смотрите на [YouTube]({{youtube_url}})."
            ),
            "reactions": [
                {"emoji": "👍", "text": "Документы идут вместе с работой"},
                {"emoji": "🤯", "text": "Деньги могут зависнуть"},
                {"emoji": "🤨", "text": "Сколько зависло у вас?"},
            ],
        },
        "youtube": {
            "title": "Контракт есть, а денег нет",
            "description": "Как превратить выполненную работу в денежное требование.",
            "tags": ["финансы", "строительство"],
        },
        "dzen": {"title": "Почему выполненная работа ещё не стала деньгами"},
        "humanizer": {
            "skill_version": skills_lock["skills"]["humanizer-ru"]["computedHash"],
            "tone_of_voice_sha256": tone_hash,
            "input_sha256": hashlib.sha256(b"evidence draft").hexdigest(),
            "stages": ["evidence_draft", "tone_of_voice", "humanizer", "lint"],
            "tool_types": ["web", "imagegen"],
            "output_sha256": output_hash,
            "error_count": 0,
        },
    }
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return bundle


def test_plan_and_editorial_gates_reach_awaiting_recording(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[2]
    data_root = tmp_path / "data"
    started = start_release(project_root, data_root, tmp_path)

    plan = run_cli(
        project_root,
        data_root,
        "release",
        "import-plan",
        "--command-id",
        "plan-1",
        "--expected-revision",
        str(started["revision"]),
        "--file",
        str(write_plan(tmp_path)),
    )
    assert plan.returncode == 0
    plan_json = response(plan)
    assert plan_json["state"] == "plan_pending"
    assert plan_json["pending_gate"] == "plan"

    plan_approved = run_cli(
        project_root,
        data_root,
        "release",
        "decide",
        "--command-id",
        "approve-plan-1",
        "--expected-revision",
        str(plan_json["revision"]),
        "--gate",
        "plan",
        "--decision",
        "approved",
        "--actor",
        "author",
    )
    approved_json = response(plan_approved)
    assert approved_json["state"] == "editorial_building"

    bundle = write_editorial_bundle(tmp_path)
    editorial = run_cli(
        project_root,
        data_root,
        "release",
        "import-editorial",
        "--command-id",
        "editorial-1",
        "--expected-revision",
        str(approved_json["revision"]),
        "--bundle",
        str(bundle),
    )
    assert editorial.returncode == 0
    editorial_json = response(editorial)
    assert editorial_json["state"] == "editorial_pending"
    assert editorial_json["pending_gate"] == "editorial"

    teleprompter = Path(editorial_json["artifacts"]["teleprompter"])
    dzen = Path(editorial_json["artifacts"]["dzen"])
    docx = Path(editorial_json["artifacts"]["docx"])
    main_text = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))["main_text"]
    assert teleprompter.read_text(encoding="utf-8") == f"Здравствуйте, друзья.\n\n{main_text}"
    assert dzen.read_text(encoding="utf-8") == main_text
    assert docx.read_bytes().startswith(b"PK")

    editorial_approved = run_cli(
        project_root,
        data_root,
        "release",
        "decide",
        "--command-id",
        "approve-editorial-1",
        "--expected-revision",
        str(editorial_json["revision"]),
        "--gate",
        "editorial",
        "--decision",
        "approved",
        "--actor",
        "author",
    )
    assert response(editorial_approved)["state"] == "awaiting_recording"


def test_editorial_rejects_wrong_visual_count(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[2]
    data_root = tmp_path / "data"
    started = start_release(project_root, data_root, tmp_path)
    plan = response(
        run_cli(
            project_root,
            data_root,
            "release",
            "import-plan",
            "--command-id",
            "plan-1",
            "--expected-revision",
            str(started["revision"]),
            "--file",
            str(write_plan(tmp_path)),
        )
    )
    approved = response(
        run_cli(
            project_root,
            data_root,
            "release",
            "decide",
            "--command-id",
            "approve-plan-1",
            "--expected-revision",
            str(plan["revision"]),
            "--gate",
            "plan",
            "--decision",
            "approved",
            "--actor",
            "author",
        )
    )

    invalid = run_cli(
        project_root,
        data_root,
        "release",
        "import-editorial",
        "--command-id",
        "editorial-invalid",
        "--expected-revision",
        str(approved["revision"]),
        "--bundle",
        str(write_editorial_bundle(tmp_path, visual_count=2)),
    )

    assert invalid.returncode == 2
    assert response(invalid)["error"]["code"] == "VALIDATION_FAILED"


def test_revision_invalidates_editorial_approval(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[2]
    data_root = tmp_path / "data"
    started = start_release(project_root, data_root, tmp_path)
    plan = response(
        run_cli(
            project_root,
            data_root,
            "release",
            "import-plan",
            "--command-id",
            "plan-1",
            "--expected-revision",
            str(started["revision"]),
            "--file",
            str(write_plan(tmp_path)),
        )
    )
    approved = response(
        run_cli(
            project_root,
            data_root,
            "release",
            "decide",
            "--command-id",
            "approve-plan-1",
            "--expected-revision",
            str(plan["revision"]),
            "--gate",
            "plan",
            "--decision",
            "approved",
            "--actor",
            "author",
        )
    )
    editorial = response(
        run_cli(
            project_root,
            data_root,
            "release",
            "import-editorial",
            "--command-id",
            "editorial-1",
            "--expected-revision",
            str(approved["revision"]),
            "--bundle",
            str(write_editorial_bundle(tmp_path)),
        )
    )
    awaiting = response(
        run_cli(
            project_root,
            data_root,
            "release",
            "decide",
            "--command-id",
            "approve-editorial-1",
            "--expected-revision",
            str(editorial["revision"]),
            "--gate",
            "editorial",
            "--decision",
            "approved",
            "--actor",
            "author",
        )
    )

    revised = run_cli(
        project_root,
        data_root,
        "release",
        "revise",
        "--command-id",
        "revise-1",
        "--expected-revision",
        str(awaiting["revision"]),
        "--target",
        "main_text",
        "--reason",
        "Исправить сумму в расчёте",
        "--actor",
        "author",
    )

    assert revised.returncode == 0
    revised_json = response(revised)
    assert revised_json["state"] == "revision_requested"
    assert revised_json["pending_gate"] is None
    assert "main_text" not in revised_json["artifacts"]
    assert "teleprompter" not in revised_json["artifacts"]
    assert "dzen" not in revised_json["artifacts"]

    rebuilt = run_cli(
        project_root,
        data_root,
        "release",
        "import-editorial",
        "--command-id",
        "editorial-2",
        "--expected-revision",
        str(revised_json["revision"]),
        "--bundle",
        str(tmp_path / "editorial"),
    )
    assert rebuilt.returncode == 0
    rebuilt_json = response(rebuilt)
    assert rebuilt_json["state"] == "editorial_pending"
    assert rebuilt_json["pending_gate"] == "editorial"
