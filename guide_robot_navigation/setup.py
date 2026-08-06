import os
from glob import glob
from importlib.util import module_from_spec, spec_from_file_location
from setuptools import find_packages, setup

package_name = "guide_robot_navigation"

# Single source of truth lives in the sibling package, read from the SOURCE
# tree - not from install/. This keeps rendering independent of build order
# and immune to --packages-select staleness.
_here = os.path.dirname(os.path.abspath("setup.py"))
_desc = os.path.join(_here, os.pardir, "guide_robot_description")

ROBOT_PARAMS = os.path.abspath(os.path.join(_desc, "config", "robot_params.yaml"))
RENDER_SCRIPT = os.path.abspath(os.path.join(_desc, "scripts", "render_params.py"))

for _p in (ROBOT_PARAMS, RENDER_SCRIPT):
    if not os.path.isfile(_p):
        raise SystemExit(
            f"{package_name}: cannot find {_p}\n"
            "guide_robot_description must be a sibling directory in the same "
            "source tree."
        )

_spec = spec_from_file_location("render_params", RENDER_SCRIPT)
_render_params = module_from_spec(_spec)
_spec.loader.exec_module(_render_params)

GEN_DIR = "generated_config"
for _template in glob("config/*.in"):
    _render_params.render(
        ROBOT_PARAMS, _template,
        os.path.join(GEN_DIR, os.path.basename(_template)[:-3]),
    )

setup(
    name=package_name,
    version="0.0.1",
    packages=find_packages(exclude=["test"]),
    data_files=[
        (f"share/{package_name}/config", glob("config/*.lua")),
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
        (f"share/{package_name}/config", glob(f"{GEN_DIR}/*.yaml")),
        (f"share/{package_name}/launch", glob("launch/*.launch.py")),
        (f"share/{package_name}/map", glob("map/*")),
        (f"share/{package_name}/config", glob("config/*.yaml")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="mook",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "scan_qos_relay = guide_robot_navigation.scan_qos_relay:main",
            "submap_boundary_visualizer = "
            "guide_robot_navigation.submap_boundary_visualizer:main",
        ]
    },
)
