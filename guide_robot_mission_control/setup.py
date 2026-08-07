"""Сборка пакета guide_robot_mission_control."""

from glob import glob

from setuptools import find_packages, setup

PACKAGE_NAME = "guide_robot_mission_control"

setup(
    name=PACKAGE_NAME,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{PACKAGE_NAME}"]),
        (f"share/{PACKAGE_NAME}", ["package.xml"]),
        (f"share/{PACKAGE_NAME}/launch", glob("launch/*.launch.py")),
        (f"share/{PACKAGE_NAME}/config", glob("config/*.yaml")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Mook",
    maintainer_email="mook@innopolis.university",
    description="Владелец состояния тура: FSM, стек прерываний, резюмирование нарратива.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            f"mission_fsm = {PACKAGE_NAME}.mission_fsm_node:main",
            f"narration_server = {PACKAGE_NAME}.narration_server_node:main",
            f"presence_monitor = {PACKAGE_NAME}.presence_monitor_node:main",
            f"mission_cli = {PACKAGE_NAME}.cli:main",
            f"mission_container = {PACKAGE_NAME}.mission_container:main",
        ],
    },
)
