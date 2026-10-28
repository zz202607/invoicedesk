Set objShell = CreateObject("Shell.Application")
Set objFolder = objShell.BrowseForFolder(0, "选择包含发票PDF的文件夹", &H10 + &H40, "<REIMBURSEMENT_DIR>")
If Not objFolder Is Nothing Then
    Set objFSO = CreateObject("Scripting.FileSystemObject")
    ' False = ANSI encoding (system default, matches GBK on Chinese Windows)
    Set objFile = objFSO.CreateTextFile(WScript.Arguments(0), False)
    objFile.Write objFolder.Self.Path
    objFile.Close
End If
