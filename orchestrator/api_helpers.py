import re
import os
import requests
from datetime import datetime, time as dt_time
from parsers import MathNetParser, parse_rooms, parse_time_start
from flask import current_app
from models import db, Session, Course, SessionUpdated, Room
from config import ConfigKey
import json as json_module
# --- Хелперы ---

def normalize_path(value: str) -> str:
    """Замена \ → / и сжатие // → /"""
    return re.sub(r"/+", "/", re.sub(r"\\+", "/", value))

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

def _list_files(folder: str, exts: tuple = None) -> list:
    """Список имён файлов в папке ([] если папки нет или пуста)."""
    if not folder or not os.path.isdir(folder):
        return []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    result = []
    for f in names:
        full = os.path.join(folder, f)
        if not os.path.isfile(full):
            continue
        if exts and not f.lower().endswith(exts):
            continue
        result.append(f)
    return result


import re as _re

# ------------------------------------------------------------------
# Конфиг команд
# ------------------------------------------------------------------

def _extract_number(filename: str) -> int:
    """00777.MTS → 777"""
    m = _re.search(r"(\d+)", os.path.splitext(filename)[0])
    return int(m.group(1)) if m else 0


def build_transcode_commands(session: Session) -> str:
    """Собирает команды для transcode.py.
       Обрабатывает все pending ConcatJob, а не только первый.
       Несколько команд разделяются через &&."""
    src_folder = _records_folder(session)
    ext_id = session.external_id

    clips = [c for c in session.clips if c.file_path]
    if len(clips) > 1:
        return ""

    parts = []
    seen_files = set()  # ← защита от дубликатов

    # --- Склейки: обрабатываем ВСЕ pending ---
    concat_jobs = [j for j in session.concat_jobs if j.status == "pending"]
    for concat_job in concat_jobs:
        files = concat_job.get_files()
        if len(files) < 2:
            parts.append(f"# ConcatJob {concat_job.id}: недостаточно файлов")
            continue

        # Пропускаем повторяющиеся наборы файлов
        key = tuple(sorted(files))
        if key in seen_files:
            continue
        seen_files.add(key)

        nums = [_extract_number(f) for f in files]
        range_str = f"{min(nums)}-{max(nums)}"

        # Имя: output_name, если заполнен; иначе генерируем из номеров
        if concat_job.output_name:
            joined_name = concat_job.output_name
        else:
            if len(nums) == 1:
                joined_name = f"{ext_id}_joined_{nums[0]}.ts"
            else:
                joined_name = f"{ext_id}_joined_{min(nums)}_{max(nums)}.ts"

        joined_ts = os.path.join(src_folder, joined_name)

        cmd1 = (
            f"python transcode.py "
            f'-w "{src_folder}" '
            f'-r "{range_str}" '
            f'-o "{joined_ts}"'
        )

        # Выходной mp4: рядом с joined_ts (в records), имя из output_name
        base_name = os.path.splitext(os.path.basename(joined_ts))[0]
        out_mp4 = os.path.join(src_folder, f"{base_name}.mp4")

        clip = clips[0] if clips else None
        ss = f"-s {clip.start_time}" if clip and clip.start_time else ""
        dd = f"-d {clip.duration}" if clip and clip.duration else ""
        cmd2 = (
            f"python transcode.py "
            f'-v "{joined_ts}" '
            f"{ss}"
            f"{dd}"
            f'-o "{out_mp4}"'
        )

        parts.append(f"{cmd1} && {cmd2}")

    # --- Одиночный клип (только если нет склеек) ---
    if not concat_jobs and len(clips) == 1:
        clip = clips[0]
        file_full = os.path.join(src_folder, clip.file_path)
        out_full_path = _ready_folder(session)
        out_full = os.path.join(out_full_path, f"{ext_id}.mp4")

        ss = f"-s {clip.start_time}" if clip.start_time else ""
        dd = f"-d {clip.duration}" if clip.duration else ""

        logo_path = current_app.config[ConfigKey.LOGO_PATH]

        parts.append(
            f"python transcode.py "
            f'-v "{file_full}" '
            f"{ss}"
            f"{dd}"
            f'-lg "{logo_path}" '
            f'-o "{out_full}"'
        )

    return " && ".join(parts) if parts else ""

# ------------------------------------------------------------------
# Помощники для проверки обновлений
# ------------------------------------------------------------------

def _titles_equivalent(
    stored_title: str, stored_author: str,
    parsed_title: str, parsed_author: str,
) -> bool:
    st = stored_title.strip()
    sa = stored_author.strip()
    pt = parsed_title.strip()
    pa = parsed_author.strip()

    if st == pt and sa == pa:
        return True

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

    if st == pt and not pa:
        return True

    return False


def _check_course_updates(course: Course) -> dict:
    """Проверяет один курс."""
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

    for ext_id, s in existing_by_id.items():
        if ext_id not in parsed_by_id:
            if s.status != "removed":
                s.status = "removed"
                removed_count += 1
                log.append(f"❌ Удалена: {ext_id} — {s.title[:60]}")

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

        if _titles_equivalent(s.title, s.author, rec_title, rec_author):
            if s.date == rec_date:
                continue
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

        upd = SessionUpdated(
            session_id=s.id,
            old_data=json_module.dumps(old, ensure_ascii=False),
            new_data=json_module.dumps(new, ensure_ascii=False),
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
        # --- Backfill: time_start + rooms для существующих ---
    backfilled_time = 0
    backfilled_rooms = 0

    for ext_id, s in existing_by_id.items():
        rec = parsed_by_id.get(ext_id)
        if not rec:
            continue

        # time_start
        if s.time_start is None:
            ts = parse_time_start(rec.get("time_range", ""))
            if ts:
                h, m = ts.split(':')
                s.time_start = dt_time(int(h), int(m))
                backfilled_time += 1
                log.append(f"🕐 backfill time: {ext_id} → {ts}")

        # rooms
        existing_rooms = {r.name for r in s.rooms}
        parsed_names = set(parse_rooms(rec.get("room", "")))
        new_rooms = parsed_names - existing_rooms
        for rname in new_rooms:
            room = Room.query.filter_by(name=rname).first()
            if not room:
                room = Room(name=rname)
                db.session.add(room)
                db.session.flush()
            s.rooms.append(room)
            backfilled_rooms += 1
            log.append(f"🚪 backfill room: {ext_id} → {rname}")

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
        "backfilled_time": backfilled_time,
        "backfilled_rooms": backfilled_rooms,
        "log": log,
    }

def _records_folder(session: Session) -> str:
    """Полный путь к папке records (промежуточные .ts/.mp4)."""
    if session.course and session.course.source_file_path:
        date_folder = session.date.strftime("%m.%d") if session.date else "unknown"
        return normalize_path(
            os.path.join(session.course.source_file_path, date_folder, "records")
        )
    return ""


def _ready_folder(session: Session) -> str:
    """Полный путь к папке ready (готовые файлы)."""
    if session.course and session.course.output_file_path:
        date_folder = session.date.strftime("%m.%d") if session.date else "unknown"
        return normalize_path(
            os.path.join(session.course.output_file_path, date_folder, "ready")
        )
    return ""
