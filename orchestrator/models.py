from flask_sqlalchemy import SQLAlchemy
import json
from sqlalchemy import MetaData

convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

db = SQLAlchemy(metadata=MetaData(naming_convention=convention))

class Course(db.Model):
    __tablename__ = "courses"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(255), nullable=False, unique=True)
    course_type = db.Column(db.String(100), nullable=False)
    notes = db.Column(db.Text, default="")

    source_url = db.Column(db.String(1000), default="")  # URL конференции mathnet

    # Общая часть пути для всех записей курса
    source_file_path = db.Column(db.String(1000), default="")
    output_file_path = db.Column(db.String(1000), default="")

    sessions = db.relationship("Session", back_populates="course", lazy="dynamic")

    def __repr__(self):
        return f"<Course {self.name} ({self.course_type})>"


# ================================================================
# Association table (Room ↔ Session, many-to-many)
# ================================================================

session_rooms = db.Table(
    'session_rooms',
    db.Column('session_id', db.Integer, db.ForeignKey('sessions.id'), primary_key=True),
    db.Column('room_id',    db.Integer, db.ForeignKey('rooms.id'),    primary_key=True)
)


# ================================================================
# Room
# ================================================================

class Room(db.Model):
    __tablename__ = 'rooms'

    id   = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)

    sessions = db.relationship(
        'Session',
        secondary=session_rooms,
        back_populates='rooms'
    )

    def __repr__(self):
        return f'<Room {self.name}>'


# ================================================================
# Performer
# ================================================================

class Performer(db.Model):
    __tablename__ = 'performers'

    id   = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)

    sessions = db.relationship('Session', back_populates='performer')

    def __repr__(self):
        return f'<Performer {self.name}>'


class Session(db.Model):
    __tablename__ = "sessions"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    external_id = db.Column(db.String(100), nullable=False, unique=True)
    title = db.Column(db.String(500), nullable=False)
    author = db.Column(db.String(255), nullable=False)
    date = db.Column(db.Date, nullable=False)

    time_start = db.Column(db.Time)               # ← время начала (для сортировки/фильтрации)

    status = db.Column(db.String(20), nullable=False, default="planned")

    course_id = db.Column(db.Integer, db.ForeignKey("courses.id"), nullable=True)
    course = db.relationship("Course", back_populates="sessions")

    performer_id = db.Column(db.Integer, db.ForeignKey('performers.id'), nullable=True)
    performer    = db.relationship('Performer', back_populates='sessions')

    rooms = db.relationship(
        'Room',
        secondary=session_rooms,
        back_populates='sessions'
    )

    # Путь к папке с исходниками (без имени файла)
    source_file_path = db.Column(db.String(1000), default="")
    # Путь к выходной папке
    output_file_path = db.Column(db.String(1000), default="")

    notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    updated_at = db.Column(
        db.DateTime, server_default=db.func.now(), onupdate=db.func.now()
    )

    # Клипы (файлы + таймкоды)
    clips = db.relationship(
        "Clip", back_populates="session",
        cascade="all, delete-orphan",
        order_by="Clip.sort_order",
    )

    VALID_STATUSES = (
        "planned",
        "happened",
        "escaped",
        "received",
        "ready_for_edit",
        "ready_for_upload",
        "done",
        "removed",
    )

    def __repr__(self):
        return f"<Session {self.external_id}: {self.title[:50]} [{self.status}]>"


class Clip(db.Model):
    """Один файл-исходник + диапазон времени внутри сессии.
    Несколько клипов = склейка."""
    __tablename__ = "clips"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_id = db.Column(db.Integer, db.ForeignKey("sessions.id"), nullable=False)
    session = db.relationship("Session", back_populates="clips")

    file_path = db.Column(db.String(500), default="")       # имя файла
    start_time = db.Column(db.String(8), default="")         # HH:MM:SS
    duration = db.Column(db.String(8), default="")           # HH:MM:SS
    sort_order = db.Column(db.Integer, default=0)

    def __repr__(self):
        return f"<Clip {self.id}: {self.file_path} [{self.start_time}→{self.duration}]>"

class ConcatJob(db.Model):
    """Склейка MTS-файлов для сессии."""
    __tablename__ = "concat_jobs"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_id = db.Column(db.Integer, db.ForeignKey("sessions.id"), nullable=False)
    session = db.relationship("Session", backref="concat_jobs")

    folder_path = db.Column(db.String(1000), default="")   # course + session
    file_list = db.Column(db.Text, default="[]")             # JSON-массив имён файлов
    status = db.Column(db.String(20), default="pending")   # pending / merged / failed

    created_at = db.Column(db.DateTime, server_default=db.func.now())

    def __repr__(self):
        return f"<ConcatJob {self.id}: {self.status} [{len(self.get_files())} files]>"

    def get_files(self) -> list:
        import json as _json
        try:
            return _json.loads(self.file_list)
        except (json.JSONDecodeError, TypeError):
            return []

    def set_files(self, files: list):
        import json as _json
        self.file_list = _json.dumps(files, ensure_ascii=False)

class Command(db.Model):
    """Команда (ffmpeg и т.п.) для задачи, привязана к сессии."""
    __tablename__ = "commands"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_id = db.Column(db.Integer, db.ForeignKey("sessions.id"), nullable=False)
    session = db.relationship("Session", backref="commands")

    command = db.Column(db.Text, default="")
    status = db.Column(db.String(20), default="pending")  # pending / queued / done / failed

    created_at = db.Column(db.DateTime, server_default=db.func.now())

    def __repr__(self):
        return f"<Command {self.id}: session={self.session_id} [{self.status}]>"


class SessionUpdated(db.Model):
    """Лог изменений полей сессии при проверке обновлений."""
    __tablename__ = "session_updates"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_id = db.Column(db.Integer, db.ForeignKey("sessions.id"), nullable=False)
    session = db.relationship("Session", backref="updates")

    old_data = db.Column(db.Text, default="{}")  # JSON: старые title, author, date
    new_data = db.Column(db.Text, default="{}")  # JSON: новые значения
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    def __repr__(self):
        return f"<SessionUpdated {self.id}: session={self.session_id}>"

    def get_old(self) -> dict:
        try:
            return json.loads(self.old_data)
        except (json.JSONDecodeError, TypeError):
            return {}

    def get_new(self) -> dict:
        try:
            return json.loads(self.new_data)
        except (json.JSONDecodeError, TypeError):
            return {}