import os
import requests
from flask import Blueprint, render_template, jsonify, request

from models import Session
from routes import LOGO_PATH, _records_folder, _ready_folder

run_bp = Blueprint("runservice", __name__, url_prefix="/run")

# Константа — адрес внешнего сервиса
RUN_SERVICE_URL = "http://127.0.0.1:8000"


def _call(method, path, **kwargs):
    """Универсальный вызов внешнего API. Возвращает (json, None) или (None, error)."""
    url = f"{RUN_SERVICE_URL}{path}"
    try:
        r = requests.request(method, url, timeout=15, **kwargs)
        r.raise_for_status()
        return r.json(), None
    except requests.exceptions.RequestException as e:
        return None, str(e)


def _duration_to_seconds(value):
    """'HH:MM:SS' или 'MM:SS' -> секунды (float)."""
    if not value:
        return 0.0
    parts = [float(p) for p in str(value).split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0]


def build_logo_payload(session):
    """Сессия -> тело POST /queue (QueueCreate + task_uid). None, если нет клипов."""
    clips = [c for c in session.clips if c.file_path]
    if not clips:
        return None

    records = _records_folder(session)
    segments = []
    total = 0.0

    for clip in clips:
        segments.append({
            "path": os.path.join(records, clip.file_path),
            "start": clip.start_time or "00:00:00",
            "duration": clip.duration or "00:00:00",
        })
        total += _duration_to_seconds(clip.duration)

    output_path = os.path.join(_ready_folder(session), f"{session.external_id}.mp4")

    payload = {
        "task_type": "transcode_logo",
        "params": {
            "segments": segments,
            "logo_path": LOGO_PATH,
            "output_path": output_path,
            "total_duration": total,
        },
    }

    # task_uid = external_id сессии (строка). None -> сервис сгенерирует сам.
    if session.external_id is not None:
        payload["task_uid"] = str(session.external_id)

    return payload


# ------------------------------------------------------------------
# Страница просмотра очереди
# ------------------------------------------------------------------
@run_bp.route("/")
def queue_page():
    return render_template("runservice_queue.html")


# ------------------------------------------------------------------
# API-прокси к внешнему сервису
# ------------------------------------------------------------------
@run_bp.route("/api/queue")
def api_queue():
    data, err = _call("GET", "/queue")
    if err:
        return jsonify({"error": err}), 502
    return jsonify(data)


@run_bp.route("/api/worker/status")
def api_worker_status():
    data, err = _call("GET", "/worker/status")
    if err:
        return jsonify({"error": err}), 502
    return jsonify(data)


@run_bp.route("/api/worker/pause", methods=["POST"])
def api_worker_pause():
    data, err = _call("POST", "/worker/pause")
    if err:
        return jsonify({"error": err}), 502
    return jsonify(data)


@run_bp.route("/api/worker/resume", methods=["POST"])
def api_worker_resume():
    data, err = _call("POST", "/worker/resume")
    if err:
        return jsonify({"error": err}), 502
    return jsonify(data)


@run_bp.route("/api/task/<string:task_uid>/progress")
def api_progress(task_uid):
    data, err = _call("GET", f"/queue/{task_uid}/progress")
    if err:
        return jsonify({"error": err}), 502
    return jsonify(data)


@run_bp.route("/api/task/<string:task_uid>/cancel", methods=["POST"])
def api_cancel(task_uid):
    data, err = _call("DELETE", f"/queue/{task_uid}")
    if err:
        return jsonify({"error": err}), 502
    return jsonify(data)


# ------------------------------------------------------------------
# Отправка сессии во внешнюю очередь
# ------------------------------------------------------------------
@run_bp.route("/api/send/<int:session_id>", methods=["POST"])
def api_send(session_id):
    session = Session.query.get_or_404(session_id)
    payload = build_logo_payload(session)
    if payload is None:
        return jsonify({"error": "Нет клипов с file_path"}), 400
    data, err = _call("POST", "/queue", json=payload)
    if err:
        return jsonify({"error": err}), 502
    return jsonify(data)

# ------------------------------------------------------------------
# История выполненных задач
# ------------------------------------------------------------------
@run_bp.route("/api/history")
def api_history():
    data, err = _call("GET", "/history")
    if err:
        return jsonify({"error": err}), 502
    return jsonify(data)


@run_bp.route("/api/history/clear", methods=["POST"])
def api_history_clear():
    body = request.get_json(silent=True) or {}
    before = body.get("before")
    if not before:
        return jsonify({"error": "before (ISO-дата) обязателен"}), 400
    data, err = _call("DELETE", f"/history?before={before}")
    if err:
        return jsonify({"error": err}), 502
    # DELETE /history возвращает пустое тело или {}
    return jsonify(data if data is not None else {"ok": True})
