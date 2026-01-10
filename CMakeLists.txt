# Generated Cmake Pico project file

cmake_minimum_required(VERSION 3.13)

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# == DO NOT EDIT THE FOLLOWING LINES for the Raspberry Pi Pico VS Code Extension to work ==
if(WIN32)
    set(USERHOME $ENV{USERPROFILE})
else()
    set(USERHOME $ENV{HOME})
endif()
set(sdkVersion 2.2.0)
set(toolchainVersion 14_2_Rel1)
set(picotoolVersion 2.2.0-a4)
set(picoVscode ${USERHOME}/.pico-sdk/cmake/pico-vscode.cmake)
if (EXISTS ${picoVscode})
    include(${picoVscode})
endif()
# ====================================================================================

set(PICO_BOARD pico2_w CACHE STRING "Board type")

# Pull in Raspberry Pi Pico SDK (must be before project)
include(pico_sdk_import.cmake)

project(mindwrite_epd_stream C CXX ASM)

pico_sdk_init()

add_executable(mindwrite_epd_stream
    src/mindwrite_epd_stream.cpp
    src/epd/ssd1683_gdey0579t93.cpp
)

pico_set_program_name(mindwrite_epd_stream "mindwrite_epd_stream")
pico_set_program_version(mindwrite_epd_stream "0.1")

# USB serial prints
pico_enable_stdio_uart(mindwrite_epd_stream 0)
pico_enable_stdio_usb(mindwrite_epd_stream 1)

target_include_directories(mindwrite_epd_stream PRIVATE
    ${CMAKE_CURRENT_LIST_DIR}/src
    ${CMAKE_CURRENT_LIST_DIR}/src/epd
)

target_link_libraries(mindwrite_epd_stream
    pico_stdlib
    hardware_spi
    hardware_gpio
    pico_cyw43_arch_none
)

pico_add_extra_outputs(mindwrite_epd_stream)
