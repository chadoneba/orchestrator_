"""Ручные инструменты."""
import os
import shutil
from flask import Blueprint, render_template, request, jsonify, current_app

from models import Session, Performer
from api_helpers import _ready_folder, _extract_number, _records_folder

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

@tools_bp.route("/api/cleanup-joined", methods=["GET", "POST"])
def api_cleanup_joined():
    """Удаляет joined .ts и .mp4 для сессий со статусом done.
    GET / ?dry_run=1 — показать, что будет удалено.
    POST — удалить.
    """
    from models import Session

    sessions = (
        Session.query
        .filter_by(status="done")
        .order_by(Session.external_id)
        .all()
    )

    dry_run = request.args.get("dry_run") == "1" if request.method == "GET" else False

    result = []
    for s in sessions:
        src_folder = _records_folder(s)
        ext_id = s.external_id

        done_jobs = [j for j in s.concat_jobs]

        for job in done_jobs:
            files = job.get_files()
            if len(files) < 2:
                continue

            nums = [_extract_number(f) for f in files]
            range_str = f"{min(nums)}_{max(nums)}"

            if job.output_name:
                base_name = os.path.splitext(job.output_name)[0]
            else:
                base_name = f"{ext_id}_joined_{range_str}"

            ts_path = os.path.join(src_folder, f"{base_name}.ts")
            mp4_path = os.path.join(src_folder, f"{base_name}.mp4")

            item = {
                "external_id": ext_id,
                "job_id": job.id,
                "ts_path_file": mp4_path,
            }

            if dry_run:
                would_delete = []
                if os.path.isfile(ts_path):
                    would_delete.append(os.path.basename(ts_path))
                if os.path.isfile(mp4_path):
                    would_delete.append(os.path.basename(mp4_path))
                item["would_delete"] = would_delete
            else:
                deleted = []
                for path in [ts_path, mp4_path]:
                    if os.path.isfile(path):
                        os.remove(path)
                        deleted.append(os.path.basename(path))
                item["deleted"] = deleted
                item["ts_was_present"] = os.path.basename(ts_path) in deleted
                item["mp4_was_present"] = os.path.basename(mp4_path) in deleted

            result.append(item)

    if dry_run:
        total_would_delete = sum(len(r.get("would_delete", [])) for r in result)
        return jsonify({
            "dry_run": True,
            "total_would_delete": total_would_delete,
            "jobs_processed": len(result),
            "items": result,
        })
    else:
        total_deleted = sum(len(r.get("deleted", [])) for r in result)
        return jsonify({
            "total_deleted": total_deleted,
            "jobs_processed": len(result),
            "items": result,
        })
