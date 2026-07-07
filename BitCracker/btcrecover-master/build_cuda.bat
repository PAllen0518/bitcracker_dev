@echo off
setlocal

REM CUDA Toolkit install dir. CUDA_PATH is normally set automatically by the
REM Toolkit installer; override CUDA_TOOLKIT_DIR yourself if it's somewhere else.
if not defined CUDA_TOOLKIT_DIR (
    if defined CUDA_PATH (
        set "CUDA_TOOLKIT_DIR=%CUDA_PATH%"
    ) else (
        set "CUDA_TOOLKIT_DIR=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.0"
    )
)

REM MSVC host-compiler bin dir. Override VS_TOOLS_BINDIR if your VS version,
REM edition, or MSVC toolset build number differs from the default below.
if not defined VS_TOOLS_BINDIR (
    set "VS_TOOLS_BINDIR=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC\14.44.35207\bin\Hostx64\x64"
)

REM nvcc writes temp files during compilation; a TEMP path containing spaces
REM (e.g. a Windows username with a space) breaks nvcc's temp-file handling.
REM Override BUILD_TEMP_DIR if C:\Temp isn't suitable on your machine.
if not defined BUILD_TEMP_DIR set "BUILD_TEMP_DIR=C:\Temp"
if not exist "%BUILD_TEMP_DIR%" mkdir "%BUILD_TEMP_DIR%"

set "NVCC=%CUDA_TOOLKIT_DIR%\bin\nvcc.exe"
set "TEMP=%BUILD_TEMP_DIR%"
set "TMP=%BUILD_TEMP_DIR%"

if not exist "%NVCC%" (
    echo ERROR: nvcc.exe not found at "%NVCC%"
    echo Set CUDA_TOOLKIT_DIR to your CUDA Toolkit install directory and re-run.
    exit /b 1
)

"%NVCC%" multibit_cuda.cu -o multibit_cuda.exe -O3 -arch=sm_75 --allow-unsupported-compiler --compiler-bindir "%VS_TOOLS_BINDIR%"
echo multibit_cuda.exe exit: %ERRORLEVEL%

"%NVCC%" multibit_cuda_threads.cu -o multibit_cuda_threads.exe -O3 -arch=sm_75 --allow-unsupported-compiler --compiler-bindir "%VS_TOOLS_BINDIR%" -std=c++14
echo multibit_cuda_threads.exe exit: %ERRORLEVEL%

endlocal
