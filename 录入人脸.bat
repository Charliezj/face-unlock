@echo off
setlocal
set "PYW=D:\Python432\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"
start "" "%PYW%" "%~dp0FaceUnlock.pyw" --setup
