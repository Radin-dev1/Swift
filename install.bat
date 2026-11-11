@echo off
cd /d "%~dp0"
python -m pip install comtypes || py -m pip install comtypes
python install.py || py install.py
pause
