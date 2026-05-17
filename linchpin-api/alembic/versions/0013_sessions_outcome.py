"""Session ``outcome`` field + outcome_evaluations table (v0.6.0 PR1).

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-17 00:00:00.000000

Two additions:

- ``sessions.outcome`` (JSONB, nullable) — the outcome definition the
  caller declared at session-create (``definition`` + ``rubric`` +
  ``grader``). NULL means no outcome was declared and no automatic
  evaluation runs.
- ``outcome_evaluations`` — append-only history of grader results
  keyed by ``session_id``. Each row records the grader run that
  produced ``score`` + ``rationale``. Auto-termination flows off
  the most recent row's score crossing the success threshold.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(
        "ALTER TABLE sessions ADD COLUMN outcome JSONB"
    )
    conn.exec_driver_sql(
        """
        CREATE TABLE outcome_evaluations (
            id            UUID PRIMARY KEY,
            session_id    UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            grader_id     UUID,
            score         NUMERIC NOT NULL CHECK (score >= 0 AND score <= 1),
            rationale     TEXT,
            criteria      JSONB NOT NULL DEFAULT '[]',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    conn.exec_driver_sql(
        "CREATE INDEX outcome_evaluations_session_idx "
        "ON outcome_evaluations (session_id, created_at DESC)"
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP INDEX IF EXISTS outcome_evaluations_session_idx")
    conn.exec_driver_sql("DROP TABLE IF EXISTS outcome_evaluations")
    conn.exec_driver_sql("ALTER TABLE sessions DROP COLUMN IF EXISTS outcome")
