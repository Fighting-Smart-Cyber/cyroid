"""add quiz_answers to walkthrough_progress

The Knowledge Check graded itself in the browser and told nobody: the score
existed only in React state, so nothing about an assessment reached the
learner record. This column is where a graded answer lands -- one JSON object
per (range, user), keyed by question id, holding the question, the option
chosen, whether it was correct and when it was answered.

Nullable, because every existing row predates the quiz and has nothing to say
about it.

Revision ID: c4a1b2d3e5f7
Revises: b1c2d3e4f5a6
Create Date: 2026-09-22 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c4a1b2d3e5f7"
down_revision: Union[str, None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "walkthrough_progress",
        sa.Column("quiz_answers", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("walkthrough_progress", "quiz_answers")
