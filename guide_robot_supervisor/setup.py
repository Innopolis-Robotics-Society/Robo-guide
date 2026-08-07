from glob import glob

from setuptools import find_packages, setup

package_name = "guide_robot_supervisor"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/launch", glob("launch/*.launch.py")),
        ("share/" + package_name + "/config", glob("config/*.yaml")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Mook",
    maintainer_email="mook@innopolis.university",
    description="Lifecycle orchestration and pluggable watchdogs for Robo-guide.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "supervisor = guide_robot_supervisor.supervisor_node:main",
        ],
    },
)