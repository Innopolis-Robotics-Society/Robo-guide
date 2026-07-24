cmake_minimum_required(VERSION 3.8)
project(guide_robot_description)

find_package(ament_cmake REQUIRED)

install(
  DIRECTORY urdf
  DESTINATION share/${PROJECT_NAME}
)

# params — установить только если папка существует
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/params")
  install(
    DIRECTORY params
    DESTINATION share/${PROJECT_NAME}
  )
endif()

# meshes — пока не добавлены, раскомментировать когда появятся
# install(
#   DIRECTORY meshes
#   DESTINATION share/${PROJECT_NAME}
# )

ament_package()
