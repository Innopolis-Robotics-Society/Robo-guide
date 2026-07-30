#include <pybind11/functional.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include "guide_robot_sonar/sonar_driver.hpp"

namespace py = pybind11;

PYBIND11_MODULE(furo_sonars_cpp, m)
{
  m.doc() = "C++ low-level Guide-Robot serial sonar driver python bindings";

  py::class_<guide_robot_sonar::SonarReading>(m, "SonarReading")
    .def_readonly("range", &guide_robot_sonar::SonarReading::range)
    .def_readonly("consecutive_errors", &guide_robot_sonar::SonarReading::consecutive_errors)
    .def_readonly("seq", &guide_robot_sonar::SonarReading::seq)
    .def_readonly("age_s", &guide_robot_sonar::SonarReading::age_s);

  py::class_<guide_robot_sonar::SonarDriver>(m, "SonarDriver")
    .def(
      py::init<const std::string &, int>(), py::arg("port") = "/dev/ttyCH341USB0",
      py::arg("baudrate") = 9600)
    .def("start", &guide_robot_sonar::SonarDriver::start)
    .def("stop", &guide_robot_sonar::SonarDriver::stop)
    .def("get_readings", &guide_robot_sonar::SonarDriver::get_readings)
    .def("is_dome_active", &guide_robot_sonar::SonarDriver::is_dome_active)
    .def("set_info_logger", &guide_robot_sonar::SonarDriver::set_info_logger)
    .def("set_error_logger", &guide_robot_sonar::SonarDriver::set_error_logger);
}
