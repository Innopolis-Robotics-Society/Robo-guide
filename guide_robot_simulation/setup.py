import os
from glob import glob

from setuptools import find_packages, setup

package_name = "guide_robot_simulation"

setup(
    name=package_name,
    version="0.0.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        (os.path.join("share", package_name, "launch"), glob("launch/*.py")),
        (os.path.join("share", package_name, "worlds"), glob("worlds/*")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="fabian",
    maintainer_email="fabian@todo.todo",
    description="TODO: Package description",
    license="TODO: License declaration",
    extras_require={
        "test": [
            "pytest",
        ],
    },
    entry_points={
        "console_scripts": ["sonar_merge = guide_robot_simulation.sonar_merge:main"],
    },
)
