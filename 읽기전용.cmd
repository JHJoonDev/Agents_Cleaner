@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3.11 scripts\main.py --read-only
if errorlevel 1 pause
