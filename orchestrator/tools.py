"""Ручные инструменты."""
import os
import shutil
from flask import Blueprint, render_template, request, jsonify, current_app

from models import Session, Performer
from routes import _ready_folder

tools_bp = Blueprint("tools", __name__, url_prefix="/tools")


@tools_bp.route("/")
def tools_page():
    return render_template("tools.html")


# --- Проверка существования папки ---
@tools_bp.route("/api/check-folder", methods=["POST"])
def api_check_folder():
    body = request.get_json(silent=True) or {}
    path = body.get("path", "").strip()
    if not path:
        return jsonify({"ok": False, "error": "Путь не указан"})
    if not os.path.exists(path):
        return jsonify({"ok": False, "error": f"Папка не существует: {path}"})
    if not os.path.isdir(path):
        return jsonify({"ok": False, "error": f"Это не папка: {path}"})
    if not os.access(path, os.W_OK):
        return jsonify({"ok": False, "error": f"Нет прав на запись в: {path}"})
    return jsonify({"ok": True, "path": path})


# --- Список сессий ready_to_upload + копирование ---
@tools_bp.route("/api/copy-ready", methods=["POST"])
def api_copy_ready():
    body = request.get_json(silent=True) or {}
    dest = body.get("dest", "").strip()
    if not dest:
        return jsonify({"ok": False, "error": "Папка назначения не указана"})

    # Проверка папки
    if not os.path.isdir(dest):
        return jsonify({"ok": False, "error": f"Папка не существует: {dest}"})
    if not os.access(dest, os.W_OK):
        return jsonify({"ok": False, "error": f"Нет прав на запись в: {dest}"})

    sessions = Session.query.filter_by(status="ready_for_upload").all()
    if not sessions:
        return jsonify({"ok": True, "copied": 0, "skipped": 0, "errors": [], "message": "Нет сессий ready_to_upload"})

    copied = 0
    skipped = 0
    errors = []

    for session in sessions:
        ready_folder = _ready_folder(session)
        filename = f"{session.external_id}.mp4"
        src = os.path.join(ready_folder, filename)

        if not os.path.isfile(src):
            skipped += 1
            errors.append(f"Файл не найден: {src}")
            continue

        dst = os.path.join(dest, filename)
        try:
            shutil.copy2(src, dst)
            copied += 1
        except OSError as e:
            errors.append(f"Ошибка копирования {filename}: {e}")

    return jsonify({
        "ok": True,
        "copied": copied,
        "skipped": skipped,
        "errors": errors,
    })

@tools_bp.route("/api/ready-list")
def api_ready_list():
    from models import Performer

    sessions = (
        Session.query
        .filter_by(status="ready_for_upload")
        .order_by(Session.external_id)
        .all()
    )

    result = []
    for s in sessions:

        ready_folder = _ready_folder(s)
        filename = f"{s.external_id}.mp4"
        full_path = os.path.join(ready_folder, filename)

        result.append({
            "external_id": s.external_id,
            "name": s.title or "—",
            "author": s.author,
            "file_path": full_path,
            "file_exists": os.path.isfile(full_path),
        })

    return jsonify({"sessions": result})
