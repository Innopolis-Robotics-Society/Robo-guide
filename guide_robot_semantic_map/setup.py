"""Сборка пакета guide_robot_semantic_map."""

from glob import glob

from setuptools import find_packages, setup

PACKAGE_NAME = "guide_robot_semantic_map"

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
