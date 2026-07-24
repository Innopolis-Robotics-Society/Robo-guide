#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include "guide_robot_sonar/sonar_driver.hpp"

namespace py = pybind11;

PYBIND11_MODULE(furo_sonars_cpp, m)
{
  m.doc() = "C++ low-level FURO-D serial sonar driver python bindings";

  py::class_<guide_robot_sonar::SonarDriver>(m, "SonarDriver")
    .def(
      py::init<const std::string &, int>(), py::arg("port") = "/dev/ttyCH341USB0",
      py::arg("baudrate") = 9600)
    .def("start", &guide_robot_sonar::SonarDriver::start)
    .def("stop", &guide_robot_sonar::SonarDriver::stop)
    .def("get_ranges", &guide_robot_sonar::SonarDriver::get_ranges);
}
