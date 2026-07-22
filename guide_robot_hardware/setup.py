from setuptools import find_packages, setup

package_name = "guide_robot_hardware"

setup(
    name=package_name,
    version="0.0.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="sokovikov",
    maintainer_email="a.sokovikov@innopolis.university",
    description="TODO: Package description",
    license="Apache License, Version 2.0",
    extras_require={
        "test": [
            "pytest",
        ],
    },
    entry_points={
        "console_scripts": ["motor_driver = guide_robot_hardware.motor_driver_node:main"],
    },
)
