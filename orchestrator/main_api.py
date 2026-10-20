import os
import requests
from datetime import datetime
from flask import Blueprint, render_template, jsonify, request, current_app
from parsers import MathNetParser, parse_rooms, parse_time_start
from models import db, Session, ConcatJob, Command, Course, Room
from api_helpers import (normalize_path,
                        _full_source_path,
                        _full_output_path,
                        _records_folder,
                        _extract_number,
                        _check_course_updates,
                        _ready_folder,
                        _list_files)
from view_helpers import attention_color


api_bp = Blueprint("api", __name__, url_prefix="/api")

# ------------------------------------------------------------------
# API: парсинг по URL
# ------------------------------------------------------------------
@api_bp.route("/parse", methods=["POST"])
def api_parse():
    data = request.get_json(silent=True) or {}

    url = (data.get("url", "") or "").strip()
    if not url:
        return jsonify({"error": "Поле url обязательно"}), 400

    min_date = None
    min_date_str = (data.get("min_date", "") or "").strip()
    if min_date_str:
        try:
            min_date = datetime.strptime(min_date_str, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "min_date должен быть YYYY-MM-DD"}), 400

    try:
        parser = MathNetParser(url, min_date=min_date)
        records = parser.parse()
        return jsonify({
            "url": url,
            "count": len(records),
            "records": records,
        })
    except requests.RequestException as e:
        return jsonify({"error": f"Ошибка HTTP: {e}"}), 502
    except Exception as e:
        return jsonify({"error": f"Ошибка парсинга: {e}"}), 500

@api_bp.route("/api/validate-path", methods=["POST"])
def api_validate_path():
    """Проверка одного пути."""
    data = request.get_json(silent=True) or {}
    path = (data.get("path", "") or "").strip()

    if not path:
        return jsonify({"valid": False, "error": "Путь не указан"})

    normalized = normalize_path(path)
    exists = os.path.exists(normalized)
    is_dir = os.path.isdir(normalized) if exists else False
    is_file = os.path.isfile(normalized) if exists else False

    return jsonify({
        "original": path,
        "normalized": normalized,
        "exists": exists,
        "is_dir": is_dir,
        "is_file": is_file,
    })


@api_bp.route("/validate-session-path/<int:session_id>", methods=["GET"])
def api_validate_session_path(session_id):
    """Проверка полного пути для сессии (course + session)."""
    session = Session.query.get_or_404(session_id)
    src_override = request.args.get("src_override", None)
    out_override = request.args.get("out_override", None)

    result = {"session_id": session_id, "external_id": session.external_id}

    # Source
    src_rel = src_override if src_override is not None else (session.source_file_path or "")
    if session.course and session.course.source_file_path:
        full_source = normalize_path(
            os.path.join(session.course.source_file_path, src_rel)
        )
    else:
        full_source = normalize_path(src_rel)

    result["source"] = {
        "full_path": full_source,
        "exists": os.path.exists(full_source) if full_source else False,
    }

    # Output
    out_rel = out_override if out_override is not None else (session.output_file_path or "")
    if session.course and session.course.output_file_path:
        full_output = normalize_path(
            os.path.join(
                session.course.output_file_path, out_rel, f"{session.external_id}.mp4"
            )
        )
    else:
        full_output = normalize_path(
            os.path.join(out_rel, f"{session.external_id}.mp4")
        )

    output_dir = os.path.dirname(full_output)
    result["output"] = {
        "full_path": full_output,
        "dir_exists": os.path.isdir(output_dir) if output_dir else False,
    }

    return jsonify(result)

@api_bp.route("/create-folders/<int:session_id>", methods=["POST"])
def api_create_folders(session_id):
    """Создаёт структуру папок records/ready для сессии."""
    session = Session.query.get_or_404(session_id)

    if not session.course:
        return jsonify({"error": "Запись не привязана к курсу"}), 400
    if not session.course.source_file_path and not session.course.output_file_path:
        return jsonify({"error": "У курса не заданы пути (source_file_path и output_file_path)"}), 400

    date_folder = session.date.strftime("%m.%d") if session.date else "unknown"

    created = []
    errors = []

    if session.course.source_file_path:
        src_folder = normalize_path(
            os.path.join(session.course.source_file_path, date_folder, "records")
        )
        try:
            os.makedirs(src_folder, exist_ok=True)
            created.append({"path": src_folder, "type": "source_records"})
        except OSError as e:
            errors.append({"path": src_folder, "error": str(e)})

    if session.course.output_file_path:
        out_folder = normalize_path(
            os.path.join(session.course.output_file_path, date_folder, "ready")
        )
        try:
            os.makedirs(out_folder, exist_ok=True)
            created.append({"path": out_folder, "type": "output_ready"})
        except OSError as e:
            errors.append({"path": out_folder, "error": str(e)})

    return jsonify({
        "created": created,
        "errors": errors,
        "ok": len(errors) == 0,
    })

# ------------------------------------------------------------------
# Подготовка: список MTS-файлов и сохранение склейки
# ------------------------------------------------------------------

@api_bp.route("/list-mts-files/<int:session_id>", methods=["GET"])
def api_list_mts_files(session_id):
    """Возвращает список .mts файлов из папки course/session source."""
    session = Session.query.get_or_404(session_id)

    # Если session.source_file_path заполнен — используем как раньше
    if session.source_file_path and session.source_file_path.strip():
        folder = _full_source_path(session)
    else:
        # Поле пустое — fallback на сгенерированную папку records
        folder = _records_folder(session)

    if not folder:
        return jsonify({"error": "Не задан путь к исходникам"}), 400
    if not os.path.isdir(folder):
        return jsonify({
            "error": f"Папка не существует: {folder}",
            "folder": folder,
            "files": [],
        }), 404

    try:
        all_files = sorted(os.listdir(folder))
        mts_files = [
            {
                "name": f,
                "size": os.path.getsize(os.path.join(folder, f)),
                "mtime": os.path.getmtime(os.path.join(folder, f)),
            }
            for f in all_files
            if f.lower().endswith(".mts")
        ]
    except OSError as e:
        return jsonify({"error": str(e), "folder": folder, "files": []}), 500

    return jsonify({
        "folder": folder,
        "count": len(mts_files),
        "files": mts_files,
    })


@api_bp.route("concat-save/<int:session_id>", methods=["POST"])
def api_concat_save(session_id):
    """Сохраняет ConcatJob. При совпадении session+files — перезаписывает."""
    session = Session.query.get_or_404(session_id)
    data = request.get_json(silent=True) or {}

    files = data.get("files", [])
    if not files or not isinstance(files, list):
        return jsonify({"error": "Список файлов обязателен"}), 400

    if session.source_file_path and session.source_file_path.strip():
        folder = _full_source_path(session)
    else:
        folder = _records_folder(session)

    import json as _json
    files_json = _json.dumps(files, ensure_ascii=False)

    existing = ConcatJob.query.filter_by(
        session_id=session.id, file_list=files_json
    ).first()

    # Генерируем имя
    ext_id = session.external_id
    nums = sorted(_extract_number(f) for f in files)
    if len(nums) == 1:
        output_name = f"{ext_id}_joined_{nums[0]}.ts"
    else:
        output_name = f"{ext_id}_joined_{nums[0]}_{nums[-1]}.ts"

    if existing:
        existing.status = "pending"
        existing.folder_path = folder
        existing.output_name = output_name
        db.session.commit()
        return jsonify({
            "id": existing.id,
            "output_name": output_name,
            "file_count": len(files),
            "status": existing.status,
            "overwritten": True,
        })

    job = ConcatJob(
        session_id=session.id,
        folder_path=folder,
        output_name=output_name,
        status="pending",
    )
    job.set_files(files)
    db.session.add(job)
    db.session.commit()

    return jsonify({
        "id": job.id,
        "output_name": output_name,
        "file_count": len(files),
        "status": job.status,
    })

@api_bp.route("/command/<int:command_id>/status", methods=["POST"])
def api_command_status(command_id):
    """Изменить статус одной команды."""
    cmd = Command.query.get_or_404(command_id)
    data = request.get_json(silent=True) or {}
    new_status = data.get("status", "").strip()

    if new_status not in ("pending", "queued", "done", "failed"):
        return jsonify({"error": f"Недопустимый статус: {new_status}"}), 400

    cmd.status = new_status
    db.session.commit()
    return jsonify({"id": cmd.id, "status": cmd.status})


@api_bp.route("/commands/queued", methods=["GET"])
def api_commands_queued():
    """Возвращает все команды со статусом queued."""
    cmds = Command.query.filter_by(status="queued").order_by(Command.id).all()
    pipeline = " && ".join(c.command for c in cmds)
    return jsonify({
        "count": len(cmds),
        "pipeline": pipeline,
        "commands": [{"id": c.id, "session_id": c.session_id, "command": c.command} for c in cmds],
    })


@api_bp.route("/attention-colors", methods=["GET"])
def api_attention_colors():
    """Возвращает список уникальных цветов из attention_color()."""
    sessions = Session.query.all()

    color_map = {}
    for s in sessions:
        c = attention_color(session_date=s.date, session_status=s.status)
        color_map[c] = color_map.get(c, 0) + 1

    result = [
        {"color": c, "count": cnt}
        for c, cnt in sorted(color_map.items(), key=lambda x: -x[1])
    ]
    return jsonify(result)

@api_bp.route("/check-course-updates/<int:course_id>", methods=["POST"])
def api_check_course_updates(course_id):
    course = Course.query.get_or_404(course_id)
    result = _check_course_updates(course)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


@api_bp.route("/check-all-courses", methods=["POST"])
def api_check_all_courses():
    courses = Course.query.all()
    courses_with_url = [c for c in courses if c.source_url.strip()]

    if not courses_with_url:
        return jsonify({"error": "Нет курсов с source_url"}), 400

    all_log = []
    total_new = 0
    total_removed = 0
    total_changed = 0
    total_parsed = 0
    total_btime = 0
    total_brooms = 0
    errors = []

    for course in courses_with_url:
        result = _check_course_updates(course)
        if "error" in result:
            errors.append(f"{course.name}: {result['error']}")
            continue
        total_new += result["new"]
        total_removed += result["removed"]
        total_changed += result["changed"]
        total_parsed += result["parsed_count"]
        total_btime += result.get("backfilled_time", 0)
        total_brooms += result.get("backfilled_rooms", 0)
        for line in result["log"]:
            all_log.append(f"[{course.name}] {line}")

    if not all_log and errors:
        all_log.append("❌ Все курсы вернули ошибки")

    return jsonify({
        "courses_checked": len(courses_with_url) - len(errors),
        "errors": errors,
        "total_parsed": total_parsed,
        "new": total_new,
        "removed": total_removed,
        "changed": total_changed,
        "backfilled_time": total_btime,
        "backfilled_rooms": total_brooms,
        "log": all_log,
    })

@api_bp.route("/backfill-sessions", methods=["POST"])
def api_backfill_sessions():
    """Разовый backfill: time_start + rooms для существующих сессий."""
    courses = Course.query.filter(Course.source_url != '').all()

    if not courses:
        return jsonify({"error": "Нет курсов с source_url"}), 400

    total_time = 0
    total_rooms = 0
    errors = []
    details = []

    for course in courses:
        try:
            parser = MathNetParser(course.source_url)
            parsed = parser.parse()
        except Exception as e:
            errors.append(f"{course.name}: {e}")
            continue

        parsed_by_id = {r["external_id"]: r for r in parsed}
        sessions = Session.query.filter_by(course_id=course.id).all()

        for session in sessions:
            rec = parsed_by_id.get(session.external_id)
            if not rec:
                continue

            changed = []

            # --- status ---

            if session.status != "done" and rec.get("status", "") == "done":
                session.status = "done"
                changed.append("status=done")

            # --- time_start ---
            if session.time_start is None:
                ts = parse_time_start(rec.get("time_range", ""))
                if ts:
                    from datetime import time as dt_time
                    h, m = ts.split(':')
                    session.time_start = dt_time(int(h), int(m))
                    changed.append(f"time_start={ts}")
                    total_time += 1

            # --- rooms ---
            existing = {r.name for r in session.rooms}
            parsed_names = set(parse_rooms(rec.get("room", "")))
            new_rooms = parsed_names - existing
            for rname in new_rooms:
                room = Room.query.filter_by(name=rname).first()
                if not room:
                    room = Room(name=rname)
                    db.session.add(room)
                    db.session.flush()
                session.rooms.append(room)
                changed.append(f"+room={rname}")
                total_rooms += 1

            if changed:
                details.append(f"{session.external_id}: {', '.join(changed)}")

        db.session.commit()

    return jsonify({
        "ok": True,
        "time_start_updated": total_time,
        "rooms_added": total_rooms,
        "errors": errors,
        "details": details[:100],
    })

@api_bp.route("/list-edit-files/<int:session_id>", methods=["GET"])
def api_list_edit_files(session_id):
    """Список .ts и .mp4 файлов из папки records."""
    session = Session.query.get_or_404(session_id)

    folder = _records_folder(session)
    if not folder:
        return jsonify({"error": "У курса не задан source_file_path", "folder": "", "files": []}), 400
    if not os.path.isdir(folder):
        return jsonify({"error": f"Папка не существует: {folder}", "folder": folder, "files": []}), 404

    try:
        all_files = sorted(os.listdir(folder))
        files = [
            {
                "name": f,
                "size": os.path.getsize(os.path.join(folder, f)),
                "mtime": os.path.getmtime(os.path.join(folder, f)),
            }
            for f in all_files
            if f.lower().endswith((".ts", ".mp4"))
        ]
    except OSError as e:
        return jsonify({"error": str(e), "folder": folder, "files": []}), 500

    return jsonify({"folder": folder, "count": len(files), "files": files})


@api_bp.route("/list-ready-files/<int:session_id>", methods=["GET"])
def api_list_ready_files(session_id):
    """Список файлов из папки ready."""
    session = Session.query.get_or_404(session_id)

    folder = _ready_folder(session)
    if not folder:
        return jsonify({"error": "У курса не задан output_file_path", "folder": "", "files": []}), 400
    if not os.path.isdir(folder):
        return jsonify({"error": f"Папка не существует: {folder}", "folder": folder, "files": []}), 404

    try:
        all_files = sorted(os.listdir(folder))
        files = [
            {
                "name": f,
                "size": os.path.getsize(os.path.join(folder, f)),
                "mtime": os.path.getmtime(os.path.join(folder, f)),
            }
            for f in all_files
            if os.path.isfile(os.path.join(folder, f))
        ]
    except OSError as e:
        return jsonify({"error": str(e), "folder": folder, "files": []}), 500

    return jsonify({"folder": folder, "count": len(files), "files": files})

@api_bp.route("/update-statuses", methods=["POST"])
def api_update_statuses():
    """Обновляет статусы сессий по правилам, не трогая done."""
    # --- те же фильтры, что на дашборде ---
    min_date_str = request.args.get("min_date", "").strip()
    course_filter = request.args.get("course_id", "").strip()
    type_filter = request.args.get("course_type", "").strip()
    status_filter = request.args.get("status", "").strip()
    color_filter = request.args.get("color", "").strip()

    query = Session.query.filter(Session.status != "done")

    min_date = None
    if min_date_str:
        try:
            min_date = datetime.strptime(min_date_str, "%Y-%m-%d").date()
        except ValueError:
            min_date = None
    if min_date:
        query = query.filter(Session.date >= min_date)

    if course_filter:
        query = query.filter(Session.course_id == int(course_filter))
    if type_filter:
        query = query.join(Course).filter(Course.course_type == type_filter)
    if status_filter:
        query = query.filter(Session.status == status_filter)

    sessions = query.all()

    if color_filter:
        sessions = [s for s in sessions if attention_color(session_date=s.date, session_status=s.status) == color_filter]

    updated = 0
    details = []

    for s in sessions:
        new_status = None

        # 1. В ready есть external_id.mp4 → ready_for_upload
        ready_folder = _ready_folder(s)
        if ready_folder and os.path.isfile(
            os.path.join(ready_folder, f"{s.external_id}.mp4")
        ):
            new_status = "ready_for_upload"

        # 2. Есть клипы, но mp4 ещё нет → ready_for_edit
        if new_status is None:
            if [c for c in s.clips if c.file_path]:
                new_status = "ready_for_edit"

        # 3. Нет клипов, но есть .ts/.mp4 в records → prepared
        if new_status is None:
            if _list_files(_records_folder(s), exts=(".ts", ".mp4")):
                new_status = "prepared"

        # 4. Нет ничего выше, но есть MTS → received
        if new_status is None:
            if _list_files(_records_folder(s), exts=(".mts",)):
                new_status = "received"

        if new_status and new_status != s.status:
            details.append(f"{s.external_id}: {s.status} → {new_status}")
            s.status = new_status
            updated += 1

    db.session.commit()

    return jsonify({"updated": updated, "details": details})

@api_bp.route("/concat-jobs/<int:session_id>", methods=["GET"])
def api_concat_jobs(session_id):
    """Возвращает все ConcatJob для сессии."""
    Session.query.get_or_404(session_id)
    jobs = ConcatJob.query.filter_by(session_id=session_id).order_by(ConcatJob.id).all()
    return jsonify({
        "jobs": [{
            "id": j.id,
            "files": j.get_files(),
            "output_name": j.output_name,
            "status": j.status,
        } for j in jobs]
    })

@api_bp.route("/concat-delete", methods=["POST"])
def api_concat_delete():
    """Удаляет выбранные ConcatJob."""
    data = request.get_json(silent=True) or {}
    ids = [int(i) for i in data.get("ids", []) if str(i).isdigit()]
    if not ids:
        return jsonify({"error": "Нет id"}), 400
    deleted = ConcatJob.query.filter(ConcatJob.id.in_(ids)).delete(synchronize_session=False)
    db.session.commit()
    return jsonify({"deleted": deleted})