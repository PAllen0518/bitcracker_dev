@echo off
set CUDA=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.0
set CLBIN=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC\14.44.35207\bin\Hostx64\x64
set NVCC="%CUDA%\bin\nvcc.exe"
set TEMP=C:\Temp
set TMP=C:\Temp

%NVCC% multibit_cuda.cu -o multibit_cuda.exe -O3 -arch=sm_75 --allow-unsupported-compiler --compiler-bindir "%CLBIN%"
echo multibit_cuda.exe exit: %ERRORLEVEL%

%NVCC% multibit_cuda_threads.cu -o multibit_cuda_threads.exe -O3 -arch=sm_75 --allow-unsupported-compiler --compiler-bindir "%CLBIN%" -std=c++14
echo multibit_cuda_threads.exe exit: %ERRORLEVEL%
