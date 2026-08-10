"""source_index_state : empreinte de l'index amont réellement ingéré

Permet de sauter une source dont l'index upstream n'a pas bougé depuis la
dernière ingestion réussie, au lieu de re-télécharger et re-parser
l'intégralité de son catalogue à chaque cycle de sync.

L'empreinte stockée est le SHA-256 que la source amont publie elle-même et
que la chaîne de confiance authentifie déjà :
  - APT : SHA256 de Packages(.gz/.xz) déclaré dans InRelease (signée GPG)
  - RPM : SHA-256 de primary.xml déclaré dans repomd.xml
  - APK : SHA-256 de l'APKINDEX.tar.gz téléchargé (aucun manifeste amont)

La ligne n'est écrite qu'APRÈS une ingestion complète et réussie, et effacée
juste avant de commencer à réécrire la table. Une sync interrompue ne laisse
donc jamais une empreinte qui prétendrait que la base contient un catalogue
qu'elle ne contient pas.

Revision ID: 0011
Revises: 0010
"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"


def upgrade():
    op.create_table(
        "source_index_state",
        sa.Column("source_id",   sa.Text(), primary_key=True),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("updated_at",  sa.Text(), nullable=False),
    )


def downgrade():
    op.drop_table("source_index_state")
