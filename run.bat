@echo off
rem Habit Pet 启动器：双击运行（后台无窗口）
cd /d "%~dp0"
start "HabitPet" /b pythonw -m habitpet
