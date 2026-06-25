@echo off
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
set CUDA_PATH=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.0
set PATH=%PATH%;%CUDA_PATH%\bin;%CUDA_PATH%\nvvm\bin
set TEMP=C:\Temp
set TMP=C:\Temp
set CL_BIN=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC\14.44.35207\bin\Hostx64\x64
nvcc multibit_cuda.cu -o multibit_cuda.exe -O3 -arch=sm_75 --compiler-bindir "%CL_BIN%" --allow-unsupported-compiler
echo Exit: %ERRORLEVEL%
