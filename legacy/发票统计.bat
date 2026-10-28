@echo off
title Invoice Stats Tool

cd /d "%~dp0"

set PY=%~dp0python_env\python.exe

if not exist "%PY%" (
    echo [ERROR] Python not found
    pause
    exit /b 1
)

"%PY%" -c "import fitz; import openpyxl" 2>nul
if %errorlevel% neq 0 (
    echo Installing dependencies...
    "%PY%" -m pip install PyMuPDF openpyxl -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn --quiet
)

if not "%~1"=="" (
    "%PY%" "%~dp0invoice_stats.py" "%~1"
    goto :done
)

set "TF=%TEMP%\_inv_folder.txt"
if exist "%TF%" del "%TF%"

cscript //nologo "%~dp0pick_folder.vbs" "%TF%" 2>nul

if exist "%TF%" (
    "%PY%" "%~dp0launcher.py" "%TF%"
    goto :done
)

"%PY%" -c "import os,sys,subprocess; p=input('Enter folder path (Enter for default): ').strip() or r'<REIMBURSEMENT_DIR>'; subprocess.run([sys.executable, os.path.join(r'%~dp0', 'invoice_stats.py'), p])"

:done
echo.
pause
