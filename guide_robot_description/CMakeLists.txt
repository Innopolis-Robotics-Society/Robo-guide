cmake_minimum_required(VERSION 3.8)
project(guide_robot_description)

find_package(ament_cmake REQUIRED)
find_package(Python3 REQUIRED COMPONENTS Interpreter)

set(ROBOT_PARAMS  ${CMAKE_CURRENT_SOURCE_DIR}/config/robot_params.yaml)
set(RENDER_SCRIPT ${CMAKE_CURRENT_SOURCE_DIR}/scripts/render_params.py)
set(GEN_DIR       ${CMAKE_CURRENT_BINARY_DIR}/config)
file(MAKE_DIRECTORY ${GEN_DIR})

set(GENERATED_CONFIGS "")
foreach(cfg controllers.yaml)
  add_custom_command(
    OUTPUT  ${GEN_DIR}/${cfg}
    COMMAND ${Python3_EXECUTABLE} ${RENDER_SCRIPT}
            ${ROBOT_PARAMS}
            ${CMAKE_CURRENT_SOURCE_DIR}/config/${cfg}.in
            ${GEN_DIR}/${cfg}
    DEPENDS ${ROBOT_PARAMS} ${RENDER_SCRIPT}
            ${CMAKE_CURRENT_SOURCE_DIR}/config/${cfg}.in
    COMMENT "Rendering ${cfg} from robot_params.yaml")
  list(APPEND GENERATED_CONFIGS ${GEN_DIR}/${cfg})
endforeach()

add_custom_target(render_configs ALL DEPENDS ${GENERATED_CONFIGS})

install(FILES ${ROBOT_PARAMS} ${GENERATED_CONFIGS}
        DESTINATION share/${PROJECT_NAME}/config)
install(PROGRAMS ${RENDER_SCRIPT} DESTINATION lib/${PROJECT_NAME})
install(DIRECTORY urdf meshes DESTINATION share/${PROJECT_NAME})

ament_package()
