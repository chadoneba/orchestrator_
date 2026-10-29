from flask import Flask
from flask_migrate import Migrate
from models import db
from routes import bp
from main_api import api_bp
from runservice import run_bp
from tools import tools_bp
from config import load_config
from sqlalchemy import event
import os


def create_app():
    app = Flask(__name__)

    basedir = os.path.abspath(os.path.dirname(__file__))

    app.config.update(load_config())

    app.config["SQLALCHEMY_DATABASE_URI"] = (
        f"sqlite:///{os.path.join(basedir, 'instance', 'orchestrator.db')}"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"check_same_thread": False}
    }

    db.init_app(app)
    migrate = Migrate(app, db)

    # ── SQLite WAL + прагмы (как в run_service) ──
    with app.app_context():
        engine = db.get_engine()

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute("PRAGMA busy_timeout=5000;")
            cursor.close()

    app.register_blueprint(bp)
    app.register_blueprint(run_bp)
    app.register_blueprint(tools_bp)
    app.register_blueprint(api_bp)

    with app.app_context():
        from models import Performer
        if not Performer.query.filter_by(name="owner").first():
            db.session.add(Performer(name="owner"))
            db.session.commit()

    from view_helpers import attention_color, status_color

    @app.context_processor
    def inject_helpers():
        return dict(
            attention_color=attention_color,
            status_color=status_color,
        )

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True,
            host='0.0.0.0',
            port=5000)