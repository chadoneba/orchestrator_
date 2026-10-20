from datetime import date

def attention_color(session_status: str, session_date: date) -> str:
    """Возвращает цвет строки в формате hex."""
    if session_status == "planned" and session_date < date.today() and (date.today() - session_date).days > 2:
        return "#ef5350"
    if session_status == "planned" and session_date < date.today() and (date.today() - session_date).days > 0:
            return "#efef50"
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