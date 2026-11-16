@echo off
cd /d "%~dp0"
if not exist worker.env (
  echo Copy worker.env.example to worker.env and add your credentials locally.
  pause
  exit /b 1
)
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe paper_worker.py --env worker.env
) else (
  py -3 paper_worker.py --env worker.env
)
pause
