"""Сборка пакета guide_robot_operator_ui."""

from glob import glob

from setuptools import find_packages, setup

PACKAGE_NAME = "guide_robot_operator_ui"

setup(
    name=PACKAGE_NAME,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{PACKAGE_NAME}"]),
        (f"share/{PACKAGE_NAME}", ["package.xml"]),
        (f"share/{PACKAGE_NAME}/launch", glob("launch/*.launch.py")),
        (f"share/{PACKAGE_NAME}/config", glob("config/*.yaml")),
        (f"share/{PACKAGE_NAME}/web", glob("web/*")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Mook",
    maintainer_email="mook@innopolis.university",
    description="Сенсорная панель оператора робота-экскурсовода (Stage 1).",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            f"operator_ui_node = {PACKAGE_NAME}.operator_ui_node:main",
        ],
    },
)
