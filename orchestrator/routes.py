import json as json_module
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app
from models import db, Session, Course, Clip, ConcatJob, Command, SessionUpdated, Room, Performer
from datetime import datetime, date, time as dt_time
import os
from flask import jsonify
import requests
from view_helpers import attention_color
from parsers import parse_time_start, parse_rooms, MathNetParser

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
    status_filter = request.args.get("status", "").strip()
    performer_filter = request.args.get("performer_id", "").strip()

    query = Session.query

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
    
    if performer_filter == "none":
        query = query.filter(Session.performer_id.is_(None))
    elif performer_filter:
        query = query.filter(Session.performer_id == int(performer_filter))
        

    sessions = query.order_by(Session.date.desc(), Session.external_id.desc()).all()

    if color_filter:
        sessions = [s for s in sessions if attention_color(session_status=s.status, session_date=s.date) == color_filter]

    courses = Course.query.order_by(Course.name).all()
    course_types = sorted(set(
        ct for ct, in db.session.query(Course.course_type).distinct()
        if ct
    ))

    valid_statuses = Session.VALID_STATUSES

    performers_all = Performer.query.order_by(Performer.name).all()

    return render_template(
        "dashboard.html",
        sessions=sessions,
        min_date=min_date_str,
        color_filter=color_filter,
        course_filter=course_filter,
        type_filter=type_filter,
        status_filter=status_filter,   
        performer_filter=performer_filter,
        performers_all=performers_all,                       
        courses=courses,
        course_types=course_types,
        valid_statuses=valid_statuses,
    )

# ------------------------------------------------------------------
# CRUD курсов — только список
# ------------------------------------------------------------------
@bp.route("/courses")
def courses():
    all_courses = Course.query.order_by(Course.name).all()
    return render_template("courses.html", courses=all_courses)


# ------------------------------------------------------------------
# Импорт — двухэтапный
# ------------------------------------------------------------------
@bp.route("/import", methods=["GET", "POST"])
def import_sessions():
    if request.method == "POST":
        action = request.form.get("action", "parse")  # "parse" или "save"

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
                raw_room = (request.form.get(f"room_{i}", "") or "").strip()
                time_range = (request.form.get(f"time_range_{i}", "") or "").strip()

                if not ext_id or not title:
                    continue

                records.append({
                    "external_id": ext_id,
                    "title": title,
                    "author": author,
                    "date": date_str,
                    "status": status,
                    "room": raw_room,
                    "time_range": time_range,
                })

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
            assigned = 0    

            for rec in records:
                ext_id = rec["external_id"]

                existing = Session.query.filter_by(external_id=ext_id).first()
                if existing:
                    # Сессия уже существует
                    if existing.course_id is None and course is not None:
                        # Без курса → привязываем к новому курсу
                        existing.course_id = course.id
                        assigned += 1
                    else:
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

                # --- Время начала ---
                ts = parse_time_start(rec.get("time_range", ""))
                if ts:
                    try:
                        h, m = map(int, ts.split(":"))
                        session.time_start = dt_time(h, m)
                    except (ValueError, TypeError):
                        pass

                db.session.add(session)
                db.session.flush()  # нужен session.id

                # --- Комнаты (парсим при сохранении) ---
                room_names = parse_rooms(rec.get("room", ""))
                for rname in room_names:
                    room = Room.query.filter_by(name=rname).first()
                    if not room:
                        room = Room(name=rname)
                        db.session.add(room)
                        db.session.flush()
                    session.rooms.append(room)

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


@bp.route("/session/<int:session_id>/edit", methods=["GET", "POST"])
def edit_session(session_id):
    session = Session.query.get_or_404(session_id)
    courses = Course.query.order_by(Course.name).all()
    rooms_all = Room.query.order_by(Room.name).all()
    performers_all = Performer.query.order_by(Performer.name).all()

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

            # --- Комнаты ---
            room_ids = request.form.getlist("room_ids")
            session.rooms.clear()
            for rid in room_ids:
                if rid:
                    room = Room.query.get(int(rid))
                    if room:
                        session.rooms.append(room)

            # --- Исполнитель ---
            performer_id = request.form.get("performer_id", "")
            if performer_id:
                session.performer_id = int(performer_id)
            else:
                session.performer_id = None

            # --- Время начала ---
            time_start_str = (request.form.get("time_start", "") or "").strip()
            if time_start_str:
                try:
                    h, m = map(int, time_start_str.split(":"))
                    session.time_start = dt_time(h, m)
                except (ValueError, TypeError):
                    session.time_start = None
            else:
                session.time_start = None

            # --- Клипы ---
            Clip.query.filter_by(session_id=session.id).delete()

            clip_count = int(request.form.get("clip_count", "0"))
            for i in range(clip_count):
                fp = (request.form.get(f"clip_file_path_{i}", "") or "").strip()
                st = (request.form.get(f"clip_start_time_{i}", "") or "").strip()
                dur = (request.form.get(f"clip_duration_{i}", "") or "").strip()

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
    if not clips:
        clips = [Clip(file_path="", start_time="", duration="", sort_order=0)]

    return render_template(
        "edit_session.html",
        session=session,
        courses=courses,
        rooms_all=rooms_all,
        performers_all=performers_all,
        source_full=source_full,
        output_full=output_full,
        source_exists=source_exists,
        output_dir_exists=output_dir_exists,
        valid_statuses=Session.VALID_STATUSES,
        clips=clips,
    )

@bp.route("/commands")
def commands_page():
    status_filter = request.args.get("status", "").strip()

    query = Command.query.order_by(Command.created_at.desc())

    if status_filter == "done":
        query = query.filter(Command.status == "done")
    elif status_filter == "all":
        pass  # всё включая done
    elif status_filter:
        query = query.filter(Command.status == status_filter)
    else:
        # По умолчанию: всё кроме done
        query = query.filter(Command.status != "done")

    commands = query.all()

    return render_template(
        "commands.html",
        commands=commands,
        status_filter=status_filter,
    )

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
    """Удаляет старые команды и генерирует новые для received + ready_for_edit."""
    sessions = Session.query.filter(
        Session.status.in_(["received", "ready_for_edit"])
    ).all()

    # Удаляем все старые команды для этих сессий
    session_ids = [s.id for s in sessions]
    if session_ids:
        Command.query.filter(
            Command.session_id.in_(session_ids)
        ).delete(synchronize_session=False)

    generated = 0
    skipped = 0

    for s in sessions:
        cmd = build_transcode_commands(s)
        if not cmd:
            skipped += 1
            continue
        db.session.add(Command(session_id=s.id, command=cmd, status="pending"))
        generated += 1

    db.session.commit()
    flash(
        f"Сгенерировано: {generated}, пропущено: {skipped}",
        "success",
    )
    return redirect(url_for("main.commands_page"))
