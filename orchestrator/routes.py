import json as json_module
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from models import db, Session, Course, Clip, ConcatJob, Command, SessionUpdated
from parsers import MathNetParser
from datetime import datetime
import os
import re
from flask import jsonify
import requests

bp = Blueprint("main", __name__)


# ------------------------------------------------------------------
# Дашборд
# ------------------------------------------------------------------

@bp.route("/")
def dashboard():
    min_date_str = request.args.get("min_date", "").strip()
    color_filter = request.args.get("color", "").strip()
    course_filter = request.args.get("course_id", "").strip()
    type_filter = request.args.get("course_type", "").strip()

    query = Session.query

    # Фильтр по дате
    min_date = None
    if min_date_str:
        try:
            min_date = datetime.strptime(min_date_str, "%Y-%m-%d").date()
        except ValueError:
            min_date = None
    if min_date:
        query = query.filter(Session.date >= min_date)

    # Фильтр по курсу
    if course_filter:
        query = query.filter(Session.course_id == int(course_filter))

    # Фильтр по типу курса
    if type_filter:
        query = query.join(Course).filter(Course.course_type == type_filter)

    sessions = query.order_by(Session.date.desc(), Session.external_id.desc()).all()

    # Фильтр по цвету — Python (attention_color требует объект)
    if color_filter:
        sessions = [s for s in sessions if attention_color(s) == color_filter]

    # Данные для фильтров
    courses = Course.query.order_by(Course.name).all()
    course_types = sorted(set(
        ct for ct, in db.session.query(Course.course_type).distinct()
        if ct
    ))

    return render_template(
        "dashboard.html",
        sessions=sessions,
        min_date=min_date_str,
        color_filter=color_filter,
        course_filter=course_filter,
        type_filter=type_filter,
        courses=courses,
        course_types=course_types,
    )

# ------------------------------------------------------------------
# CRUD курсов — только список
# ------------------------------------------------------------------
@bp.route("/courses")
def courses():
    all_courses = Course.query.order_by(Course.name).all()
    return render_template("courses.html", courses=all_courses)


# ------------------------------------------------------------------
# Импорт: форма с URL → /api/parse → база
# ------------------------------------------------------------------
# ------------------------------------------------------------------
# Импорт — двухэтапный
# ------------------------------------------------------------------
@bp.route("/import", methods=["GET", "POST"])
def import_sessions():
    if request.method == "POST":
        action = request.form.get("action", "parse")  # "parse" или "save"

        # --- ЭТАП 2: сохранение в базу ---
        # --- ЭТАП 2: сохранение в базу ---
        if action == "save":
            course_name = (request.form.get("course_name", "") or "").strip()
            course_type = (request.form.get("course_type", "") or "").strip()
            min_date_str = (request.form.get("min_date", "") or "").strip()
            source_url = (request.form.get("source_url", "") or "").strip()
            record_count = int(request.form.get("record_count", "0"))


            if not record_count:
                flash("Нет данных для сохранения", "error")
                return redirect(url_for("main.import_sessions"))

            # Собираем записи из формы
            records = []
            for i in range(record_count):
                ext_id = (request.form.get(f"ext_id_{i}", "") or "").strip()
                title = (request.form.get(f"title_{i}", "") or "").strip()
                author = (request.form.get(f"author_{i}", "") or "").strip()
                date_str = (request.form.get(f"date_{i}", "") or "").strip()
                status = (request.form.get(f"status_{i}", "") or "").strip()

                if not ext_id or not title:
                    continue

                records.append({
                    "external_id": ext_id,
                    "title": title,
                    "author": author,
                    "date": date_str,
                    "status": status,
                })

            # Дофильтрация по min_date
            if min_date_str:
                try:
                    min_date = datetime.strptime(min_date_str, "%Y-%m-%d").date()
                except ValueError:
                    flash("Неверный формат даты", "error")
                    return redirect(url_for("main.import_sessions"))
                records = [
                    r for r in records
                    if r["date"] and datetime.strptime(r["date"], "%Y-%m-%d").date() >= min_date
                ]

            if not records:
                flash("После фильтрации не осталось записей", "warning")
                return redirect(url_for("main.import_sessions"))

            # Курс: найти или создать
            course = None
            if course_name and course_type:
                course = Course.query.filter_by(
                    name=course_name, course_type=course_type
                ).first()
                if not course:
                    course = Course(name=course_name, course_type=course_type, source_url=source_url)
                    db.session.add(course)
                    db.session.flush()
            elif course_name:
                course = Course.query.filter_by(name=course_name).first()

            # Сохранение
            imported = 0
            skipped = 0

            for rec in records:
                ext_id = rec["external_id"]
                if Session.query.filter_by(external_id=ext_id).first():
                    skipped += 1
                    continue
                try:
                    rec_date = datetime.strptime(rec["date"], "%Y-%m-%d").date()
                except (ValueError, KeyError):
                    skipped += 1
                    continue

                session = Session(
                    external_id=ext_id,
                    title=rec["title"],
                    author=rec.get("author", "Неизвестен"),
                    date=rec_date,
                    status=rec["status"],
                    course_id=course.id if course else None,
                )
                db.session.add(session)
                imported += 1

            db.session.commit()
            course_label = f"«{course.name}»" if course else "без курса"
            flash(
                f"Сохранено: {imported}, пропущено: {skipped} (курс: {course_label})",
                "success",
            )
            return redirect(url_for("main.dashboard"))

        # --- ЭТАП 1: парсинг ---
        url = (request.form.get("url", "") or "").strip()
        min_date_str = (request.form.get("min_date", "") or "").strip()

        if not url:
            flash("URL обязателен", "error")
            return render_template("import.html", phase=1)

        min_date = None
        if min_date_str:
            try:
                min_date = datetime.strptime(min_date_str, "%Y-%m-%d").date()
            except ValueError:
                flash("Неверный формат даты. Используй YYYY-MM-DD", "error")
                return render_template("import.html", phase=1)

        try:
            parser = MathNetParser(url, min_date=min_date)
            records = parser.parse()
        except requests.RequestException as e:
            flash(f"Ошибка HTTP: {e}", "error")
            return render_template("import.html", phase=1)
        except Exception as e:
            flash(f"Ошибка парсинга: {e}", "error")
            return render_template("import.html", phase=1)

        if not records:
            flash("Не получено ни одной записи", "warning")
            return render_template("import.html", phase=1)

        return render_template(
            "import.html",
            phase=2,
            records=records,
            records_json=json_module.dumps(records, ensure_ascii=False),
            parsed_count=len(records),
            parsed_url=url,
        )

    # GET
    course_name = request.args.get("course_name", "")
    return render_template("import.html", phase=1, pref_course_name=course_name)


# ------------------------------------------------------------------
# API: парсинг по URL
# ------------------------------------------------------------------
@bp.route("/api/parse", methods=["POST"])
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


# ------------------------------------------------------------------
# Смена статуса
# ------------------------------------------------------------------
@bp.route("/session/<int:session_id>/status", methods=["POST"])
def update_status(session_id):
    session = Session.query.get_or_404(session_id)
    new_status = request.form.get("status", "")

    if new_status in Session.VALID_STATUSES:
        session.status = new_status
        db.session.commit()
        flash(f"Статус изменён на «{new_status}»", "success")

    return redirect(request.referrer or url_for("main.dashboard"))

@bp.route("/courses/<int:course_id>/edit", methods=["GET", "POST"])
def edit_course(course_id):
    course = Course.query.get_or_404(course_id)

    if request.method == "POST":
        course.name = (request.form.get("name", "") or "").strip()
        course.course_type = (request.form.get("course_type", "") or "").strip()
        course.source_file_path = (request.form.get("source_file_path", "") or "").strip()
        course.output_file_path = (request.form.get("output_file_path", "") or "").strip()
        course.notes = (request.form.get("notes", "") or "").strip()
        course.source_url = (request.form.get("source_url", "") or "").strip()

        if not course.name or not course.course_type:
            flash("Название и тип курса обязательны", "error")
            return render_template("edit_course.html", course=course)

        db.session.commit()
        flash(f"Курс «{course.name}» обновлён", "success")
        return redirect(url_for("main.courses"))

    return render_template("edit_course.html", course=course)

def normalize_path(value: str) -> str:
    """Замена \ → / и сжатие // → /"""
    return re.sub(r"/+", "/", re.sub(r"\\+", "/", value))


@bp.route("/api/validate-path", methods=["POST"])
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


@bp.route("/api/validate-session-path/<int:session_id>", methods=["GET"])
def api_validate_session_path(session_id):
    """Проверка полного пути для сессии (course + session).
    Можно передать ?src_override=...&out_override=... чтобы проверить
    несохранённые значения из формы.
    """
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

@bp.route("/session/<int:session_id>/edit", methods=["GET", "POST"])
def edit_session(session_id):
    session = Session.query.get_or_404(session_id)
    courses = Course.query.order_by(Course.name).all()

    if request.method == "POST":
        action = request.form.get("action", "save")

        if action == "save":
            session.title = (request.form.get("title", "") or "").strip()
            session.author = (request.form.get("author", "") or "").strip()
            session.source_file_path = normalize_path(
                (request.form.get("source_file_path", "") or "").strip()
            )
            session.output_file_path = normalize_path(
                (request.form.get("output_file_path", "") or "").strip()
            )
            session.notes = (request.form.get("notes", "") or "").strip()

            new_course_id = request.form.get("course_id", "")
            if new_course_id:
                session.course_id = int(new_course_id)
            else:
                session.course_id = None

            # --- Клипы: удалить старые, создать новые ---
            Clip.query.filter_by(session_id=session.id).delete()

            clip_count = int(request.form.get("clip_count", "0"))
            for i in range(clip_count):
                fp = (request.form.get(f"clip_file_path_{i}", "") or "").strip()
                st = (request.form.get(f"clip_start_time_{i}", "") or "").strip()
                dur = (request.form.get(f"clip_duration_{i}", "") or "").strip()

                # Пропускаем полностью пустые строки
                if not fp and not st and not dur:
                    continue

                clip = Clip(
                    session_id=session.id,
                    file_path=fp,
                    start_time=st,
                    duration=dur,
                    sort_order=i,
                )
                db.session.add(clip)

            db.session.commit()
            flash(f"Запись «{session.external_id}» обновлена", "success")

        elif action == "status":
            new_status = request.form.get("status", "")
            if new_status in Session.VALID_STATUSES:
                session.status = new_status
                db.session.commit()
                flash(f"Статус изменён на «{new_status}»", "success")

        return redirect(url_for("main.edit_session", session_id=session_id))

    # --- GET: сборка данных ---
    source_full = _full_source_path(session)
    output_full = _full_output_path(session)

    source_exists = os.path.exists(source_full) if source_full else False
    output_dir = os.path.dirname(output_full) if output_full else ""
    output_dir_exists = os.path.isdir(output_dir) if output_dir else False

    clips = session.clips
    # Если нет ни одного клипа — передаём один пустой для формы
    if not clips:
        clips = [Clip(file_path="", start_time="", duration="", sort_order=0)]

    return render_template(
        "edit_session.html",
        session=session,
        courses=courses,
        source_full=source_full,
        output_full=output_full,
        source_exists=source_exists,
        output_dir_exists=output_dir_exists,
        valid_statuses=Session.VALID_STATUSES,
        clips=clips,
    )


# --- Хелперы ---

def _full_source_path(session: Session) -> str:
    """Полный путь к папке с исходниками."""
    if session.course and session.course.source_file_path:
        return normalize_path(
            os.path.join(session.course.source_file_path, session.source_file_path or "")
        )
    return normalize_path(session.source_file_path or "")

def _full_output_path(session: Session, filename: str = None) -> str:
    """Полный путь к выходному файлу."""
    if filename is None:
        filename = f"{session.external_id}.mp4"
    if session.course and session.course.output_file_path:
        return normalize_path(
            os.path.join(session.course.output_file_path, session.output_file_path or "", filename)
        )
    return normalize_path(os.path.join(session.output_file_path or "", filename))

@bp.route("/api/create-folders/<int:session_id>", methods=["POST"])
def api_create_folders(session_id):
    """Создаёт структуру папок records/ready для сессии."""
    session = Session.query.get_or_404(session_id)

    if not session.course:
        return jsonify({"error": "Запись не привязана к курсу"}), 400
    if not session.course.source_file_path and not session.course.output_file_path:
        return jsonify({"error": "У курса не заданы пути (source_file_path и output_file_path)"}), 400

    # Формат папки: MM.DD (например "10.03" для 3 октября)
    date_folder = session.date.strftime("%m.%d") if session.date else "unknown"

    created = []
    errors = []

    # Source: курс/source/MM.DD/records
    if session.course.source_file_path:
        src_folder = normalize_path(
            os.path.join(session.course.source_file_path, date_folder, "records")
        )
        try:
            os.makedirs(src_folder, exist_ok=True)
            created.append({"path": src_folder, "type": "source_records"})
        except OSError as e:
            errors.append({"path": src_folder, "error": str(e)})

    # Output: курс/output/MM.DD/ready
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

@bp.route("/api/list-mts-files/<int:session_id>", methods=["GET"])
def api_list_mts_files(session_id):
    """Возвращает список .mts файлов из папки course/session source."""
    session = Session.query.get_or_404(session_id)

    folder = _full_source_path(session)
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


@bp.route("/api/concat-save/<int:session_id>", methods=["POST"])
def api_concat_save(session_id):
    """Сохраняет список выбранных MTS-файлов в ConcatJob."""
    session = Session.query.get_or_404(session_id)
    data = request.get_json(silent=True) or {}

    files = data.get("files", [])
    if not files or not isinstance(files, list):
        return jsonify({"error": "Список файлов обязателен"}), 400

    folder = _full_source_path(session)
    job = ConcatJob(
        session_id=session.id,
        folder_path=folder,
        status="pending",
    )
    job.set_files(files)
    db.session.add(job)
    db.session.commit()

    return jsonify({
        "id": job.id,
        "folder_path": job.folder_path,
        "file_count": len(files),
        "status": job.status,
    })

@bp.route("/commands")
def commands_page():
    commands = Command.query.order_by(Command.created_at.desc()).all()
    return render_template("commands.html", commands=commands)

@bp.route("/log")
def log_page():
    course_id = request.args.get("course_id", "").strip()
    page = request.args.get("page", "1")

    query = SessionUpdated.query.order_by(SessionUpdated.created_at.desc())

    if course_id:
        query = query.join(Session).filter(Session.course_id == int(course_id))

    try:
        page_num = int(page)
    except ValueError:
        page_num = 1

    per_page = 50
    updates = query.paginate(page=page_num, per_page=per_page, error_out=False)

    courses = Course.query.order_by(Course.name).all()

    return render_template(
        "log.html",
        updates=updates,
        courses=courses,
        course_filter=course_id,
    )

@bp.route("/commands/generate", methods=["POST"])
def commands_generate():
    """Генерирует команды для всех сессий в ready_for_edit."""
    sessions = Session.query.filter_by(status="ready_for_edit").all()

    generated = 0
    updated = 0
    skipped = 0

    for s in sessions:
        cmd = build_transcode_commands(s)

        if not cmd:
            skipped += 1
            continue

        existing = Command.query.filter_by(session_id=s.id).first()
        if existing:
            existing.command = cmd
            existing.status = "pending"
            updated += 1
        else:
            db.session.add(Command(session_id=s.id, command=cmd, status="pending"))
            generated += 1

    db.session.commit()
    flash(
        f"Сгенерировано: {generated}, обновлено: {updated}, пропущено: {skipped}",
        "success",
    )
    return redirect(url_for("main.commands_page"))

import re as _re

# ------------------------------------------------------------------
# Конфиг команд
# ------------------------------------------------------------------
LOGO_PATH = "/mnt/d/Maltsev/2026/logo_left_1080.png"  # ← замени на реальный путь


def _extract_number(filename: str) -> int:
    """00777.MTS → 777"""
    m = _re.search(r"(\d+)", os.path.splitext(filename)[0])
    return int(m.group(1)) if m else 0


def build_transcode_commands(session: Session) -> str:
    """Собирает команду (или две через &&) для transcode.py.
    Возвращает пустую строку если нечего генерировать.
    """
    src_folder = _full_source_path(session)
    ext_id = session.external_id

    # --- Случай 1: множественные клипы — пока пропускаем ---
    clips = [c for c in session.clips if c.file_path]
    if len(clips) > 1:
        return ""

    # --- Случай 2: есть ConcatJob (MTS-файлы) ---
    concat_job = session.concat_jobs[0] if session.concat_jobs else None
    if concat_job and concat_job.status == "pending":
        files = concat_job.get_files()
        if len(files) < 2:
            return f"# ConcatJob {concat_job.id}: недостаточно файлов"

        nums = [_extract_number(f) for f in files]
        range_str = f"{min(nums)}-{max(nums)}"

        joined_ts = os.path.join(src_folder, f"{ext_id}_joined.ts")

        # Команда 1: склейка MTS → TS
        cmd1 = (
            f"python transcode.py "
            f'-w "{src_folder}" '
            f'-r "{range_str}" '
            f'-o "{joined_ts}"'
        )

        # Команда 2: нарезка TS → MP4
        out_mp4 = _full_output_path(session, filename=f"{ext_id}_joined.mp4")
        clip = clips[0] if clips else None
        ss = f"-s {clip.start_time}" if clip and clip.start_time else ""
        dd = f"-d {clip.duration}" if clip and clip.duration else ""
        cmd2 = (
            f"python transcode.py "
            f'-v "{joined_ts}" '
            f"{ss} "
            f"{dd} "
            f'-o "{out_mp4}"'
        )

        return f"{cmd1} && {cmd2}"

    # --- Случай 3: одиночный клип ---
    if len(clips) == 1:
        clip = clips[0]
        file_full = os.path.join(src_folder, clip.file_path)
        out_full = _full_output_path(session)

        ss = f"-s {clip.start_time}" if clip.start_time else ""
        dd = f"-d {clip.duration}" if clip.duration else ""

        return (
            f"python transcode.py "
            f'-v "{file_full}" '
            f"{ss} "
            f"{dd} "
            f'-lg "{LOGO_PATH}" '
            f'-o "{out_full}"'
        )

    return ""

@bp.route("/api/attention-colors", methods=["GET"])
def api_attention_colors():
    """Возвращает список уникальных цветов из attention_color().
    Каждый цвет — объект {color, label, count}.
    """
    sessions = Session.query.all()

    # Считаем цвета
    color_map = {}  # color → count
    for s in sessions:
        c = attention_color(s)
        color_map[c] = color_map.get(c, 0) + 1

    # Сортируем по количеству (убывание)
    result = [
        {"color": c, "count": cnt}
        for c, cnt in sorted(color_map.items(), key=lambda x: -x[1])
    ]
    return jsonify(result)

from datetime import date
def attention_color(session: Session) -> str:
    """Возвращает цвет строки в формате hex. По умолчанию белый.
    Пропиши здесь свои правила.
    """
    if session.status == "planned" and session.date < date.today() and (date.today() - session.date).days > 2:
        return "#ef5350"
    return "#ffffff"

def status_color(status: str) -> str:
    """Цвет индикатора по статусу."""
    colors = {
        "planned": "#90a4ae",
        "happened": "#42a5f5",
        "escaped": "#ef5350",
        "received": "#26a69a",
        "ready_for_edit": "#ffa726",
        "ready_for_upload": "#ab47bc",
        "done": "#66bb6a",
    }
    return colors.get(status, "#e0e0e0")


@bp.route("/api/check-course-updates/<int:course_id>", methods=["POST"])
def api_check_course_updates(course_id):
    course = Course.query.get_or_404(course_id)
    result = _check_course_updates(course)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)

# ------------------------------------------------------------------
# Помощники для проверки обновлений
# ------------------------------------------------------------------

def _titles_equivalent(
    stored_title: str, stored_author: str,
    parsed_title: str, parsed_author: str,
) -> bool:
    """True если парснутые данные совпадают с сохранёнными,
    даже когда парсер склеил title+author в одно поле."""
    st = stored_title.strip()
    sa = stored_author.strip()
    pt = parsed_title.strip()
    pa = parsed_author.strip()

    # Полное совпадение
    if st == pt and sa == pa:
        return True

    # Парсер склеил title + author
    if sa and pt:
        variants = [
            f"{st} {sa}",
            f"{st}. {sa}",
            f"{st}, {sa}",
            f"{st} ({sa})",
            f"{sa} {st}",
        ]
        if pt in variants:
            return True
        if st in pt and sa in pt:
            return True

    # Title совпал, автор у парсера пуст (пользователь вписал вручную)
    if st == pt and not pa:
        return True

    return False


def _check_course_updates(course: Course) -> dict:
    """Проверяет один курс. Возвращает {'log': [...], 'new': N, 'removed': N, 'changed': N}."""
    if not course.source_url:
        return {"error": "source_url не задан"}

    try:
        parser = MathNetParser(course.source_url)
        parsed = parser.parse()
    except requests.RequestException as e:
        return {"error": f"Ошибка HTTP: {e}"}
    except Exception as e:
        return {"error": f"Ошибка парсинга: {e}"}

    if not parsed:
        return {"error": "Парсер вернул пустой список"}

    parsed_by_id = {str(r["external_id"]): r for r in parsed}

    existing = Session.query.filter_by(course_id=course.id).all()
    existing_by_id = {s.external_id: s for s in existing}

    log = []
    new_count = 0
    removed_count = 0
    changed_count = 0

    # --- Новые ---
    for ext_id, rec in parsed_by_id.items():
        if ext_id not in existing_by_id:
            try:
                rec_date = datetime.strptime(rec["date"], "%Y-%m-%d").date()
            except (ValueError, KeyError):
                rec_date = datetime.now().date()

            session = Session(
                external_id=ext_id,
                title=rec.get("title", "Без названия"),
                author=rec.get("author", rec.get("lecturer", "Неизвестен")),
                date=rec_date,
                status="planned",
                course_id=course.id,
            )
            db.session.add(session)
            new_count += 1
            log.append(f"➕ Новая: {ext_id} — {rec.get('title', '?')[:60]}")

    # --- Удалённые ---
    for ext_id, s in existing_by_id.items():
        if ext_id not in parsed_by_id:
            if s.status != "removed":
                s.status = "removed"
                removed_count += 1
                log.append(f"❌ Удалена: {ext_id} — {s.title[:60]}")

    # --- Изменённые ---
    for ext_id, s in existing_by_id.items():
        rec = parsed_by_id.get(ext_id)
        if not rec:
            continue

        rec_title = rec.get("title", "")
        rec_author = rec.get("author", rec.get("lecturer", ""))
        try:
            rec_date = datetime.strptime(rec.get("date", ""), "%Y-%m-%d").date()
        except (ValueError, KeyError):
            rec_date = s.date

        # Сравниваем с учётом склейки title+author
        if _titles_equivalent(s.title, s.author, rec_title, rec_author):
            # Title/author не изменились — проверяем только дату
            if s.date == rec_date:
                continue
            # Изменилась только дата
            old = {"date": s.date.strftime("%Y-%m-%d") if s.date else ""}
            new = {"date": rec_date.strftime("%Y-%m-%d") if rec_date else ""}
        else:
            old = {
                "title": s.title,
                "author": s.author,
                "date": s.date.strftime("%Y-%m-%d") if s.date else "",
            }
            new = {
                "title": rec_title,
                "author": rec_author,
                "date": rec_date.strftime("%Y-%m-%d") if rec_date else "",
            }

        if old == new:
            continue

        # Сохраняем в лог
        upd = SessionUpdated(
            session_id=s.id,
            old_data=json.dumps(old, ensure_ascii=False),
            new_data=json.dumps(new, ensure_ascii=False),
        )
        db.session.add(upd)
        s.title = rec_title
        s.author = rec_author
        s.date = rec_date
        changed_count += 1

        diff_parts = []
        if old.get("title") != new.get("title"):
            diff_parts.append("title")
        if old.get("author") != new.get("author"):
            diff_parts.append("author")
        if old.get("date") != new.get("date"):
            diff_parts.append("date")
        log.append(f"✏️ Изменена: {ext_id} — поля: {', '.join(diff_parts)}")

    db.session.commit()

    if not log:
        log.append("✅ Всё актуально, изменений нет")

    return {
        "course_id": course.id,
        "course_name": course.name,
        "parsed_count": len(parsed),
        "existing_count": len(existing),
        "new": new_count,
        "removed": removed_count,
        "changed": changed_count,
        "log": log,
    }

@bp.route("/api/check-all-courses", methods=["POST"])
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
        # Лог с префиксом курса
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
        "log": all_log,
    })
