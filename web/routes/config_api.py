"""LLM API Key configuration page."""

from flask import Blueprint, render_template, request, jsonify
from web.models import db, SystemConfig as DbConfig

bp = Blueprint("config_api", __name__)


@bp.route("/")
def index():
    """Show API configuration page."""
    api_key = DbConfig.query.filter_by(key="llm_api_key").first()
    api_base = DbConfig.query.filter_by(key="llm_api_base").first()
    model = DbConfig.query.filter_by(key="llm_model").first()

    # Mask the key for display
    masked_key = ""
    if api_key and api_key.value:
        k = api_key.value
        if len(k) > 8:
            masked_key = k[:3] + "*" * (len(k) - 8) + k[-4:]
        else:
            masked_key = "***"

    return render_template(
        "config_api.html",
        api_key=api_key,
        api_base=api_base,
        model=model,
        masked_key=masked_key,
    )


@bp.route("/save", methods=["POST"])
def save():
    """Save API configuration."""
    data = request.get_json() or {}
    for key in ["llm_api_key", "llm_api_base", "llm_model"]:
        if key in data and data[key]:
            row = DbConfig.query.filter_by(key=key).first()
            if row:
                row.value = str(data[key])
    db.session.commit()

    # Also update .env file so existing pipeline code sees the change.
    # In Docker this file may be mounted read-only, so DB remains source of truth.
    env_synced = _update_env_file(data)

    return jsonify({"status": "ok", "env_synced": env_synced})


def _update_env_file(data: dict) -> bool:
    """Write API key changes back to .env for CLI compatibility."""
    from pathlib import Path

    # Resolve relative to project root (parent of web/ directory)
    _project_root = Path(__file__).resolve().parent.parent.parent
    env_path = _project_root / ".env"
    if not env_path.exists():
        return False

    lines = env_path.read_text(encoding="utf-8").splitlines()
    updated: set[str] = set()

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("LLM_API_KEY="):
            if "llm_api_key" in data and data["llm_api_key"]:
                new_lines.append(f"LLM_API_KEY={data['llm_api_key']}")
                updated.add("llm_api_key")
                continue
        elif stripped.startswith("LLM_API_BASE="):
            if "llm_api_base" in data and data["llm_api_base"]:
                new_lines.append(f"LLM_API_BASE={data['llm_api_base']}")
                updated.add("llm_api_base")
                continue
        elif stripped.startswith("LLM_MODEL="):
            if "llm_model" in data and data["llm_model"]:
                new_lines.append(f"LLM_MODEL={data['llm_model']}")
                updated.add("llm_model")
                continue
        new_lines.append(line)

    for key in data:
        if key not in updated and data[key]:
            env_var = key.upper()
            new_lines.append(f"{env_var}={data[key]}")

    try:
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        return True
    except OSError:
        return False
