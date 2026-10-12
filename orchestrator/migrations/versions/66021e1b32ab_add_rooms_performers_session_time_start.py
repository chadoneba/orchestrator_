"""add rooms, performers, session.time_start

Revision ID: 66021e1b32ab
Revises: 07b274c229ec
Create Date: 2026-10-12 12:49:33.796470

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '66021e1b32ab'
down_revision = '07b274c229ec'
branch_labels = None
depends_on = None


def upgrade():
    # --- 1. Создаём таблицы (их не было в прошлой миграции) ---
    op.create_table(
        'rooms',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=50), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )

    op.create_table(
        'performers',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )

    op.create_table(
        'session_rooms',
        sa.Column('session_id', sa.Integer(), nullable=False),
        sa.Column('room_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['room_id'], ['rooms.id'], ),
        sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ),
        sa.PrimaryKeyConstraint('session_id', 'room_id'),
    )

    # --- 2. Добавляем поля в sessions ---
    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('time_start', sa.Time(), nullable=True))
        batch_op.add_column(sa.Column('performer_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f('fk_sessions_performer_id_performers'),
            'performers',
            ['performer_id'],
            ['id'],
        )


def downgrade():
    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f('fk_sessions_performer_id_performers'), type_='foreignkey'
        )
        batch_op.drop_column('performer_id')
        batch_op.drop_column('time_start')

    op.drop_table('session_rooms')
    op.drop_table('performers')
    op.drop_table('rooms')