"""Сборка пакета guide_robot_voice."""

from glob import glob

from setuptools import find_packages, setup

PACKAGE_NAME = "guide_robot_voice"

setup(
    name=PACKAGE_NAME,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{PACKAGE_NAME}"]),
        (f"share/{PACKAGE_NAME}", ["package.xml"]),
        (f"share/{PACKAGE_NAME}/launch", glob("launch/*.launch.py")),
        (f"share/{PACKAGE_NAME}/config", glob("config/*.yaml")),
        (
            f"share/{PACKAGE_NAME}/models",
            glob("models/*.onnx") + glob("models/*.onnx.json") + glob("models/*_tokens.txt"),
        ),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Mook",
    maintainer_email="mook@innopolis.university",
    description="Аудио-I/O робота-экскурсовода.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            f"tts_node = {PACKAGE_NAME}.tts_node:main",
            f"audio_frontend = {PACKAGE_NAME}.audio_frontend:main",
            f"vad_node = {PACKAGE_NAME}.vad_node:main",
            f"asr_node = {PACKAGE_NAME}.asr_node:main",
            f"wakeword_node = {PACKAGE_NAME}.wakeword_node:main",
        ],
    },
)
