from flask import Flask
from flask_migrate import Migrate
from models import db
from routes import bp
import os

def create_app():
    app = Flask(__name__)

    basedir = os.path.abspath(os.path.dirname(__file__))
    app.config["SECRET_KEY"] = "dev-key-change-me-in-production"
    app.config["SQLALCHEMY_DATABASE_URI"] = (
        f"sqlite:///{os.path.join(basedir, 'instance', 'orchestrator.db')}"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)
    migrate = Migrate(app, db)   # ← добавить
    app.register_blueprint(bp)

    with app.app_context():
        from models import Performer
        if not Performer.query.filter_by(name="owner").first():
            db.session.add(Performer(name="owner"))
            db.session.commit()

    from routes import attention_color, status_color

    @app.context_processor
    def inject_helpers():
        return dict(
            attention_color=attention_color,
            status_color=status_color,
        )

    return app

if __name__ == "__main__":
    app = create_app()
    app.run(debug=True)