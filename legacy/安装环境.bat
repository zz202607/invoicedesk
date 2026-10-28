@echo off
chcp 65001 >nul
echo ============================================
echo   安装 Python + PyMuPDF 环境
echo ============================================
echo.

:: 设置路径
set INSTALL_DIR=%~dp0python_env
set PYTHON_URL=https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip
set PYTHON_ZIP=%TEMP%\python-3.11.9-embed.zip

:: 检查是否已安装
if exist "%INSTALL_DIR%\python.exe" (
    echo Python 已安装，检查依赖...
    "%INSTALL_DIR%\python.exe" -c "import fitz; import openpyxl; print('PyMuPDF:', fitz.__version__); print('openpyxl: OK')" 2>nul
    if %errorlevel%==0 (
        echo.
        echo [OK] 所有依赖已就绪！
        echo.
        pause
        exit /b 0
    )
)

echo [1/4] 下载 Python 3.11.9 嵌入式版本...
echo.
echo 如果下载失败，请手动从以下地址下载：
echo %PYTHON_URL%
echo 并放置到: %PYTHON_ZIP%
echo.

:: 使用 certutil 下载（Windows内置，更可靠）
certutil -urlcache -split -f "%PYTHON_URL%" "%PYTHON_ZIP%" >nul 2>&1

if not exist "%PYTHON_ZIP%" (
    echo.
    echo [!] certutil 下载失败，尝试使用 PowerShell 下载...
    powershell -Command "Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%PYTHON_ZIP%' -UseBasicParsing" 2>nul
)

if not exist "%PYTHON_ZIP%" (
    echo.
    echo [错误] 下载失败！
    echo 请手动下载 Python 3.11.9 embeddable:
    echo %PYTHON_URL%
    echo 保存到: %PYTHON_ZIP%
    echo.
    pause
    exit /b 1
)

echo.
echo [2/4] 解压 Python...
if exist "%INSTALL_DIR%" rmdir /s /q "%INSTALL_DIR%"
mkdir "%INSTALL_DIR%"

:: 使用 PowerShell 解压
powershell -Command "Expand-Archive -Path '%PYTHON_ZIP%' -DestinationPath '%INSTALL_DIR%' -Force" 2>nul

if not exist "%INSTALL_DIR%\python.exe" (
    echo.
    echo [错误] 解压失败！
    pause
    exit /b 1
)

echo [3/4] 启用 pip...
:: 修改 python311._pth 文件以启用 pip
echo python311.zip>> "%INSTALL_DIR%\python311._pth"
echo .>> "%INSTALL_DIR%\python311._pth"
echo import site>> "%INSTALL_DIR%\python311._pth"

:: 下载 get-pip.py
echo 下载 pip...
certutil -urlcache -split -f "https://bootstrap.pypa.io/get-pip.py" "%TEMP%\get-pip.py" >nul 2>&1

if exist "%TEMP%\get-pip.py" (
    echo 安装 pip...
    "%INSTALL_DIR%\python.exe" "%TEMP%\get-pip.py" --no-warn-script-location 2>nul
)

echo.
echo [4/4] 安装 PDF 解析库...
echo.

echo 安装 PyMuPDF (PDF解析)...
"%INSTALL_DIR%\python.exe" -m pip install PyMuPDF --no-warn-script-location --trusted-host pypi.org --trusted-host files.pythonhosted.org 2>&1 | findstr /v "Requirement already satisfied"

echo 安装 openpyxl (Excel生成)...
"%INSTALL_DIR%\python.exe" -m pip install openpyxl --no-warn-script-location --trusted-host pypi.org --trusted-host files.pythonhosted.org 2>&1 | findstr /v "Requirement already satisfied"

echo.
:: 验证安装
echo ============================================
echo   验证安装...
echo ============================================
"%INSTALL_DIR%\python.exe" -c "import fitz; import openpyxl; print(f'PyMuPDF: {fitz.__version__}'); print('openpyxl: OK')"
if %errorlevel% neq 0 (
    echo.
    echo [!] 依赖安装可能不完整，请手动执行：
    echo   cd "%INSTALL_DIR%"
    echo   python.exe -m pip install PyMuPDF openpyxl
) else (
    echo.
    echo ============================================
    echo   [OK] 安装完成！
    echo ============================================
    echo.
    echo 现在可以运行 发票统计.bat 了
)

echo.
pause
