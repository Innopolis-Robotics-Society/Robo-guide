"""Сборка пакета guide_robot_llm."""

from glob import glob

from setuptools import find_packages, setup

PACKAGE_NAME = "guide_robot_llm"

setup(
    name=PACKAGE_NAME,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{PACKAGE_NAME}"]),
        (f"share/{PACKAGE_NAME}", ["package.xml"]),
        (f"share/{PACKAGE_NAME}/launch", glob("launch/*.launch.py")),
        (f"share/{PACKAGE_NAME}/config", glob("config/*.yaml") + glob("config/*.txt")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Mook",
    maintainer_email="mook@innopolis.university",
    description="Голосовой чат с LLM робота-экскурсовода.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            f"chat_node = {PACKAGE_NAME}.chat_node:main",
        ],
    },
)
