from pathlib import Path

from core.personality import DEFAULT_PERSONA_TEXT, PersonalityEngine


def test_personality_defaults_create_new_layout(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"

    PersonalityEngine.ensure_default_files(config_dir)

    personality_path = config_dir / "personality.yml"
    persona_path = config_dir / "personas" / "yukiko.md"
    assert personality_path.exists()
    assert persona_path.exists()
    assert "persona_file: personas/yukiko.md" in personality_path.read_text(encoding="utf-8")
    assert "YuKiKo（雪）" in persona_path.read_text(encoding="utf-8")

    engine = PersonalityEngine.from_file(personality_path)
    assert "不是冷冰冰的工具" in engine.persona_text


def test_personality_defaults_migrate_legacy_persona(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "persona.md").write_text("legacy persona text\n", encoding="utf-8")

    PersonalityEngine.ensure_default_files(config_dir)

    persona_path = config_dir / "personas" / "yukiko.md"
    assert persona_path.read_text(encoding="utf-8").strip() == "legacy persona text"
    assert DEFAULT_PERSONA_TEXT.startswith("YuKiKo")
