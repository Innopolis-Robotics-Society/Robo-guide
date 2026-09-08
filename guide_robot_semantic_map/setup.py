"""Сборка пакета guide_robot_semantic_map."""

from glob import glob
from pathlib import Path

from setuptools import find_packages, setup

PACKAGE_NAME = "guide_robot_semantic_map"


def _media_data_files() -> list[tuple[str, list[str]]]:
    """(target_dir, files) на каждую поддиректорию content/media/<exhibit_id>/ (Task B, B2).

    data_files не сохраняет вложенность сам -- один tuple на директорию,
    иначе все файлы под media/ слились бы в один общий install-каталог.
    """
    entries: list[tuple[str, list[str]]] = []
    media_root = Path("content/media")
    if not media_root.is_dir():
        return entries
    for exhibit_dir in sorted(p for p in media_root.iterdir() if p.is_dir()):
        files = sorted(str(f) for f in exhibit_dir.iterdir() if f.is_file())
        if files:
            entries.append((f"share/{PACKAGE_NAME}/content/media/{exhibit_dir.name}", files))
    return entries


setup(
    name=PACKAGE_NAME,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{PACKAGE_NAME}"]),
        (f"share/{PACKAGE_NAME}", ["package.xml"]),
        (f"share/{PACKAGE_NAME}/launch", glob("launch/*.launch.py")),
        (f"share/{PACKAGE_NAME}/config", glob("config/*.yaml") + glob("config/*.geojson")),
        (f"share/{PACKAGE_NAME}/content", glob("content/*.yaml")),
        *_media_data_files(),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Mook",
    maintainer_email="mook@innopolis.university",
    description="Read-only заземление робота-гида: локации, туры, контент, маршруты.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            f"content_server = {PACKAGE_NAME}.content_server:main",
            f"location_server = {PACKAGE_NAME}.location_server:main",
            f"route_planner = {PACKAGE_NAME}.route_planner:main",
        ],
    },
)
