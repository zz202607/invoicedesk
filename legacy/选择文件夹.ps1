# 选择文件夹对话框 - 修复BOM问题
$ErrorActionPreference = "Stop"

try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing

    $f = New-Object System.Windows.Forms.FolderBrowserDialog
    $f.Description = "选择包含发票PDF的文件夹"
    $f.SelectedPath = "<REIMBURSEMENT_DIR>"
    $f.ShowNewFolderButton = $false

    $result = $f.ShowDialog()
    
    if ($result -eq "OK") {
        # 使用 ASCII 编码避免 BOM 问题
        [System.IO.File]::WriteAllText("$PSScriptRoot\selected_folder.txt", $f.SelectedPath, [System.Text.Encoding]::ASCII)
        exit 0
    } else {
        exit 1
    }
} catch {
    exit 1
}
