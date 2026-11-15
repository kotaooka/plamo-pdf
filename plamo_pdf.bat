@echo off
rem Drag and drop PDF files onto this file to make bilingual HTML and open it.
rem Choose the model below: 8b (best quality, ~6.5 GB VRAM) or 0.8b (light, ~1 GB VRAM)
set MODEL=8b

chcp 65001 >nul
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

"%PY%" -c "import pymupdf" 2>nul || "%PY%" -m pip install -r "%~dp0requirements.txt"
"%PY%" "%~dp0plamo_pdf.py" --model %MODEL% --open %*
pause
