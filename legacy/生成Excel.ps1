<#
生成Excel报表和分类HTML - 使用JSON数据
#>

$ErrorActionPreference = "Stop"

# 从环境变量或默认值获取文件夹路径
if ($env:SELECTED_FOLDER) {
    $FOLDER = $env:SELECTED_FOLDER
} else {
    $FOLDER = "<REIMBURSEMENT_DIR>"
}

$OUTPUT_DIR = Join-Path $FOLDER "分类合并结果"
$JSON_FILE = Join-Path $OUTPUT_DIR "invoices.json"

if (-not (Test-Path $JSON_FILE)) {
    Write-Host "错误: 未找到 invoices.json，请先运行发票解析" -ForegroundColor Red
    exit 1
}

$invoices = Get-Content $JSON_FILE -Raw -Encoding UTF8 | ConvertFrom-Json

Write-Host ""
Write-Host "[1/2] 生成Excel统计表..." -ForegroundColor Yellow

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$excelFile = Join-Path $OUTPUT_DIR "发票分类统计_${timestamp}.xlsx"

# 明细CSV
$csvDetail = [System.IO.Path]::GetTempFileName() + "_detail.csv"
$detailRows = @()
foreach ($inv in $invoices) {
    $detailRows += [PSCustomObject]@{
        "序号"="$($detailRows.Count+1)"
        "文件路径"="$($inv.filePath)"
        "物品/项目"="$($inv.itemName)"
        "类别"="$($inv.category)"
        "金额(元)"=$inv.amount
        "发票号码"="$($inv.invoiceNumber)"
        "开票日期"="$($inv.date)"
        "销售方"="$($inv.seller)"
        "解析状态"="$($inv.parseStatus)"
    }
}
$detailRows | Export-Csv -Path $csvDetail -NoTypeInformation -Encoding UTF8

# 分类汇总CSV
$csvSummary = [System.IO.Path]::GetTempFileName() + "_summary.csv"
$catStats = @{}
foreach ($inv in $invoices) {
    $cat = $inv.category
    if (-not $catStats.ContainsKey($cat)) { $catStats[$cat] = @{Count=0;Total=0} }
    $catStats[$cat].Count++
    $catStats[$cat].Total += $inv.amount
}
$sortedCats = $catStats.GetEnumerator() | Sort-Object { $_.Value.Total } -Descending
$totalAmount = ($invoices | Measure-Object amount -Sum).Sum
$summaryRows = foreach ($entry in $sortedCats) {
    [PSCustomObject]@{
        "类别"="$($entry.Key)"
        "笔数"="$($entry.Value.Count)"
        "金额(元)"=$entry.Value.Total
        "占比(%)"=if($totalAmount -gt 0){[math]::Round($entry.Value.Total/$totalAmount*100,1)}else{0}
        "平均金额"=if($entry.Value.Count -gt 0){[math]::Round($entry.Value.Total/$entry.Value.Count,2)}else{0}
    }
}
$summaryRows | Export-Csv -Path $csvSummary -NoTypeInformation -Encoding UTF8

# Excel转换
$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false; $excel.DisplayAlerts = $false
$wb = $excel.Workbooks.Open($csvDetail)
$ws1 = $wb.Worksheets.Item(1); $ws1.Name = "发票明细"
$ws2 = $wb.Worksheets.Add(); $ws2.Name = "分类汇总"
$summaryData = Import-Csv -Path $csvSummary -Encoding UTF8
$row = 1; $ws2.Cells.Item($row,1).Value2 = "分类金额汇总"; $ws2.Range("A1:E1").Merge(); $row = 2
foreach ($h in @("类别","笔数","金额(元)","占比(%)","平均金额")) { $ws2.Cells.Item($row, [Array]::IndexOf(@("类别","笔数","金额(元)","占比(%)","平均金额"), $h) + 1).Value2 = $h }
$row++
foreach ($s in $summaryData) {
    for ($i = 0; $i -lt 5; $i++) {
        $ws2.Cells.Item($row, $i+1).Value2 = $s.PSObject.Properties[$i].Value
    }
    $row++
}
$ws2.Cells.Item($row,1).Value2 = "合计"; $ws2.Cells.Item($row,2).Value2 = "$($invoices.Count)"; $ws2.Cells.Item($row,3).Value2 = "$totalAmount"
$ws2.Columns.AutoFit() | Out-Null; $ws1.Columns.AutoFit() | Out-Null
$wb.SaveAs($excelFile, 51); $wb.Close(); $excel.Quit()
Remove-Item $csvDetail -Force -ErrorAction SilentlyContinue; Remove-Item $csvSummary -Force -ErrorAction SilentlyContinue
[System.Runtime.Interopservices.Marshal]::ReleaseComObject($ws1) | Out-Null
[System.Runtime.Interopservices.Marshal]::ReleaseComObject($ws2) | Out-Null
[System.Runtime.Interopservices.Marshal]::ReleaseComObject($wb) | Out-Null
[System.Runtime.Interopservices.Marshal]::ReleaseComObject($excel) | Out-Null
[System.GC]::Collect(); [System.GC]::WaitForPendingFinalizers()
Write-Host "  Excel: $excelFile" -ForegroundColor Green

Write-Host ""
Write-Host "[2/2] 生成分类HTML..." -ForegroundColor Yellow

$htmlTemplate = @"
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>{TITLE}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: "Microsoft YaHei", sans-serif; padding: 20px; background: #f5f5f5; color: #333; }}
        .container {{ max-width: 900px; margin: 0 auto; background: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
        h1 {{ font-size: 22px; color: #2F5496; margin-bottom: 8px; }}
        .meta {{ font-size: 13px; color: #888; margin-bottom: 20px; }}
        .invoice {{ border: 1px solid #e0e0e0; border-radius: 6px; padding: 15px; margin-bottom: 15px; }}
        .invoice h3 {{ font-size: 14px; color: #2F5496; margin-bottom: 8px; }}
        .invoice p {{ font-size: 13px; margin: 3px 0; }}
        .amount {{ font-size: 16px; font-weight: bold; color: #d46b08; }}
        .total {{ font-size: 18px; font-weight: bold; color: #2F5496; text-align: right; margin-top: 20px; padding-top: 15px; border-top: 2px solid #2F5496; }}
        @media print {{
            body {{ background: #fff; padding: 0; }}
            .container {{ box-shadow: none; padding: 15px; }}
            .invoice {{ page-break-inside: avoid; }}
        }}
    </style>
</head>
<body>
<div class="container">
    <h1>{TITLE}</h1>
    <div class="meta">{META}</div>
    {INVOICES}
    <div class="total">总金额: {TOTAL}</div>
</div>
</body>
</html>
"@

# 按分类分组
$classified = @{}
foreach ($inv in $invoices) {
    $cat = $inv.category
    if (-not $classified.ContainsKey($cat)) { $classified[$cat] = @() }
    $classified[$cat] += $inv
}

foreach ($cat in $classified.Keys) {
    $catInvoices = $classified[$cat]
    $outputHtml = Join-Path $OUTPUT_DIR "${cat}.html"
    Write-Host "  [$cat] $($catInvoices.Count) 张发票..." -ForegroundColor White
    
    $invoicesHtml = ""
    for ($i = 0; $i -lt $catInvoices.Count; $i += 2) {
        $inv1 = $catInvoices[$i]
        $inv2 = if ($i + 1 -lt $catInvoices.Count) { $catInvoices[$i + 1] } else { $null }
        
        $invoicesHtml += "<div style='display:flex;gap:15px;margin-bottom:15px;'>"
        
        # 左半张
        $invoicesHtml += "<div class='invoice' style='flex:1;'>"
        $invoicesHtml += "<h3>发票 $($i+1)</h3>"
        $invoicesHtml += "<p><b>物品:</b> $($inv1.itemName)</p>"
        $invoicesHtml += "<p class='amount'>金额: ¥$($inv1.amount.ToString('N2'))</p>"
        if ($inv1.invoiceNumber) { $invoicesHtml += "<p><b>号码:</b> $($inv1.invoiceNumber)</p>" }
        if ($inv1.date) { $invoicesHtml += "<p><b>日期:</b> $($inv1.date)</p>" }
        if ($inv1.seller) { $invoicesHtml += "<p><b>销售方:</b> $($inv1.seller)</p>" }
        $invoicesHtml += "<p><b>文件:</b> $($inv1.fileName)</p>"
        $invoicesHtml += "</div>"
        
        # 右半张
        if ($inv2) {
            $invoicesHtml += "<div class='invoice' style='flex:1;'>"
            $invoicesHtml += "<h3>发票 $($i+2)</h3>"
            $invoicesHtml += "<p><b>物品:</b> $($inv2.itemName)</p>"
            $invoicesHtml += "<p class='amount'>金额: ¥$($inv2.amount.ToString('N2'))</p>"
            if ($inv2.invoiceNumber) { $invoicesHtml += "<p><b>号码:</b> $($inv2.invoiceNumber)</p>" }
            if ($inv2.date) { $invoicesHtml += "<p><b>日期:</b> $($inv2.date)</p>" }
            if ($inv2.seller) { $invoicesHtml += "<p><b>销售方:</b> $($inv2.seller)</p>" }
            $invoicesHtml += "<p><b>文件:</b> $($inv2.fileName)</p>"
            $invoicesHtml += "</div>"
        }
        
        $invoicesHtml += "</div>"
    }
    
    $catTotal = ($catInvoices | Measure-Object amount -Sum).Sum
    $html = $htmlTemplate -replace "{TITLE}", "$cat - 发票汇总（共 $($catInvoices.Count) 张）"
    $html = $html -replace "{META}", "生成日期: $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
    $html = $html -replace "{INVOICES}", $invoicesHtml
    $html = $html -replace "{TOTAL}", "¥$catTotal.ToString('N2')"
    
    [System.IO.File]::WriteAllText($outputHtml, $html, [System.Text.Encoding]::UTF8)
    Write-Host "    -> $outputHtml" -ForegroundColor Green
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  全部完成！" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  输出目录: $OUTPUT_DIR"
Write-Host ""
Write-Host "生成的文件:" -ForegroundColor Yellow
Get-ChildItem -Path $OUTPUT_DIR | Where-Object { $_.Name -notlike "*.json" } | ForEach-Object {
    $size = if ($_.Length -gt 1MB) { "$([math]::Round($_.Length/1MB,1)) MB" } else { "$([math]::Round($_.Length/1KB,1)) KB" }
    Write-Host "  $($_.Name) ($size)" -ForegroundColor White
}
Write-Host ""
Write-Host "提示: HTML文件可在浏览器打开后 Ctrl+P 打印为PDF" -ForegroundColor Yellow
