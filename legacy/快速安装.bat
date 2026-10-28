@echo off
chcp 65001 >nul

:: 智能安装脚本 - 多源下载
set INSTALL_DIR=%~dp0python_env
set PYTHON_EXE=%INSTALL_DIR%\python.exe

:: 如果已安装，直接跳过
if exist "%PYTHON_EXE%" (
    "%PYTHON_EXE%" -c "import fitz; import openpyxl" 2>nul
    if %errorlevel%==0 (
        echo [OK] Python 环境已就绪
        exit /b 0
    )
)

echo ========================================
echo   安装 Python + PyMuPDF
echo ========================================
echo.

:: 方案1: 检查是否已有 Python 在系统路径
where python >nul 2>&1
if %errorlevel%==0 (
    echo 尝试使用系统 Python...
    python -c "import fitz; import openpyxl; print('OK')" 2>nul
    if %errorlevel%==0 (
        echo [OK] 系统 Python 可用！
        exit /b 0
    )
)

:: 方案2: 检查常见安装路径
for %%p in (
    "C:\Python311\python.exe"
    "C:\Python310\python.exe"
    "C:\Program Files\Python311\python.exe"
    "C:\Program Files\Python310\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
) do (
    if exist %%~p (
        echo 找到 Python: %%~p
        set SYS_PYTHON=%%~p
        goto :check_libs
    )
)

:check_libs
if defined SYS_PYTHON (
    "%SYS_PYTHON%" -c "import fitz; import openpyxl; print('OK')" 2>nul
    if %errorlevel%==0 (
        echo [OK] Python 环境就绪
        echo PYTHON_EXE=%SYS_PYTHON% > "%~dp0python_path.txt"
        exit /b 0
    )
    
    echo 安装 PyMuPDF...
    "%SYS_PYTHON%" -m pip install PyMuPDF openpyxl --quiet
    if %errorlevel%==0 (
        echo [OK] 安装完成！
        echo PYTHON_EXE=%SYS_PYTHON% > "%~dp0python_path.txt"
        exit /b 0
    )
)

:: 方案3: 下载嵌入式 Python
echo.
echo 正在下载 Python 嵌入式版本...
echo 这可能需要几分钟，请耐心等待...
echo.

:: 创建安装目录
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

:: 尝试多个下载源
set ZIP_FILE=%INSTALL_DIR%\python.zip
set DOWNLOADED=0

:: 源1: 官方
echo 尝试从官方源下载...
certutil -urlcache -split -f "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip" "%ZIP_FILE%" >nul 2>&1
if exist "%ZIP_FILE%" set DOWNLOADED=1

:: 源2: 如果官方失败，尝试镜像
if !DOWNLOADED!==0 (
    echo 尝试从镜像源下载...
    powershell -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://mirrors.huaweicloud.com/python/3.11.9/python-3.11.9-embed-amd64.zip' -OutFile '%ZIP_FILE%' -UseBasicParsing" 2>nul
    if exist "%ZIP_FILE%" set DOWNLOADED=1
)

if !DOWNLOADED!==0 (
    echo.
    echo [!] 自动下载失败
    echo.
    echo 请手动完成以下步骤：
    echo 1. 下载: https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip
    echo 2. 解压到: %INSTALL_DIR%
    echo 3. 在 %INSTALL_DIR%\python311._pth 最后添加一行: import site
    echo 4. 运行: %INSTALL_DIR%\python.exe -m pip install PyMuPDF openpyxl
    echo.
    pause
    exit /b 1
)

echo 解压 Python...
powershell -Command "Expand-Archive -Path '%ZIP_FILE%' -DestinationPath '%INSTALL_DIR%' -Force" 2>nul

if not exist "%INSTALL_DIR%\python.exe" (
    echo [错误] 解压失败
    pause
    exit /b 1
)

:: 启用 pip
echo import site>> "%INSTALL_DIR%\python311._pth"

:: 安装 pip
echo 安装 pip...
certutil -urlcache -split -f "https://bootstrap.pypa.io/get-pip.py" "%TEMP%\get-pip.py" >nul 2>&1
if exist "%TEMP%\get-pip.py" (
    "%INSTALL_DIR%\python.exe" "%TEMP%\get-pip.py" --no-warn-script-location 2>nul
)

:: 安装依赖
echo.
echo 安装 PyMuPDF...
"%INSTALL_DIR%\python.exe" -m pip install PyMuPDF --no-warn-script-location --trusted-host pypi.org --trusted-host files.pythonhosted.org

echo 安装 openpyxl...
"%INSTALL_DIR%\python.exe" -m pip install openpyxl --no-warn-script-location --trusted-host pypi.org --trusted-host files.pythonhosted.org

:: 验证
echo.
echo ========================================
"%INSTALL_DIR%\python.exe" -c "import fitz; import openpyxl; print(f'PyMuPDF: {fitz.__version__}'); print('openpyxl: OK'); print('[OK] 安装完成！')"
if %errorlevel% neq 0 (
    echo [!] 安装可能不完整
    echo 请手动运行:
    echo   %INSTALL_DIR%\python.exe -m pip install PyMuPDF openpyxl
) else (
    echo PYTHON_EXE=%INSTALL_DIR%\python.exe > "%~dp0python_path.txt"
)

echo.
pause
