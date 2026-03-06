from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py
from setuptools.command.sdist import sdist as _sdist


def _sync_migration_assets() -> None:
    repo_root = Path(__file__).resolve().parent
    src_alembic_ini = repo_root / "alembic.ini"
    src_migrations = repo_root / "migrations"

    dst_package = repo_root / "pixlvault"
    dst_alembic_ini = dst_package / "alembic.ini"
    dst_migrations = dst_package / "migrations"

    src_exists = src_alembic_ini.exists() and src_migrations.exists()
    dst_exists = dst_alembic_ini.exists() and dst_migrations.exists()

    if not src_exists:
        if dst_exists:
            return
        raise FileNotFoundError(
            "Expected migration assets either at repository root (alembic.ini and migrations/) "
            "or already synced in pixlvault/ package paths."
        )

    dst_package.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_alembic_ini, dst_alembic_ini)

    if dst_migrations.exists():
        shutil.rmtree(dst_migrations)
    shutil.copytree(src_migrations, dst_migrations)


class build_py(_build_py):
    def run(self):
        _sync_migration_assets()
        super().run()


class sdist(_sdist):
    def run(self):
        _sync_migration_assets()
        super().run()


if __name__ == "__main__":
    setup(
        cmdclass={
            "build_py": build_py,
            "sdist": sdist,
        }
    )
