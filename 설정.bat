@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw setup_gui.py) || (python setup_gui.py)
