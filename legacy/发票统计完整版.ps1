<#
发票统计+分类工具 - 纯PowerShell极速版（无COM依赖）
功能：
1. 扫描所有子文件夹PDF发票（含"未用"文件夹）
2. 按票据分类.doc的规则分类
3. 每个分类生成一个HTML文件（可直接打印为PDF）
4. 生成Excel统计表
#>

$ErrorActionPreference = "Stop"

# ============ 分类定义（按票据分类.doc） ============
$CategoryRules = @{
    "差旅费"       = @("住宿","酒店","宾馆","车费","轻轨","机票","火车票","客运","旅行","差旅")
    "餐饮费"       = @("餐饮","餐费","饭","食","招待","饮食")
    "汽车费用"     = @("加油","油费","停车","汽车维修","过路费","高速","过路","ETC")
    "福利费"       = @("化妆品","粮油","服装","鞋帽","药品","超市","福利")
    "业务招待费"   = @("礼品","烟酒","茶叶","金银","首饰","奢侈品","娱乐","旅游")
    "办公费用"     = @("办公","文具","打印","纸张","墨盒","硒鼓","电脑","手机","桶装水","快递","植物","电信","网络")
    "广告费用"     = @("广告","宣传","会务","标签","瓶身")
    "培训费"       = @("培训","学费","教育")
    "其他费用"     = @("劳务","服务","咨询","设计","技术")
    "材料设备"     = @("材料","原料","设备","泵","膜","土","硅","五金","工具","配件","机械","仪器","超滤","热缩","风机","吨桶")
}

function Get-InvoiceCategory {
    param([string]$ItemName, [string]$FileName)
    $text = "$ItemName $FileName".ToLower()
    foreach ($cat in $CategoryRules.Keys) {
        foreach ($kw in $CategoryRules[$cat]) {
            if ($text -like "*$kw*") { return $cat }
        }
    }
    return "其他费用"
}

function Extract-AmountFromFilename {
    param([string]$FileName)
    $name = [System.IO.Path]::GetFileNameWithoutExtension($FileName)
    $nameMatch = [regex]::Match($name, "(\d+(?:,\d{3})*(?:\.\d{1,2})?)\s*$")
    if ($nameMatch.Success) {
        $amountStr = $nameMatch.Groups[1].Value -replace ","
        $amount = 0
        if ([double]::TryParse($amountStr, [ref]$amount)) { return $amount }
    }
    return 0
}

function Extract-ItemNameFromFilename {
    param([string]$FileName)
    $name = [System.IO.Path]::GetFileNameWithoutExtension($FileName)
    $textPart = $name -replace "[\d,_\-\.]+$" -replace "^[_\d]+"
    if ([string]::IsNullOrWhiteSpace($textPart)) { $textPart = $name }
    return $textPart.Trim()
}

$FOLDER = "<REIMBURSEMENT_DIR>"
$OUTPUT_DIR = Join-Path $FOLDER "分类合并结果"
if (-not (Test-Path $OUTPUT_DIR)) { New-Item -ItemType Directory -Path $OUTPUT_DIR -Force | Out-Null }

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  发票统计 + 分类工具" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# 1. 扫描所有PDF
Write-Host "[1/4] 扫描PDF文件..." -ForegroundColor White
$pdfFiles = Get-ChildItem -Path $FOLDER -Recurse -Filter "*.pdf" -File
Write-Host "  找到 $($pdfFiles.Count) 个PDF文件（含所有子文件夹）" -ForegroundColor Green

# 2. 解析并分类
Write-Host ""
Write-Host "[2/4] 解析并分类..." -ForegroundColor Yellow
$invoices = @()
$classified = @{}

foreach ($pdf in $pdfFiles) {
    $itemName = Extract-ItemNameFromFilename -FileName $pdf.Name
    $amount = Extract-AmountFromFilename -FileName $pdf.Name
    $category = Get-InvoiceCategory -ItemName $itemName -FileName $pdf.Name
    
    $inv = [PSCustomObject]@{
        FilePath = $pdf.FullName; FileName = $pdf.Name; ItemName = $itemName
        Category = $category; Amount = $amount; Folder = $pdf.DirectoryName
        Status = if ($amount -gt 0) { "成功" } else { "未提取" }
    }
    $invoices += $inv
    if (-not $classified.ContainsKey($category)) { $classified[$category] = @() }
    $classified[$category] += $inv
    
    $statusText = if ($amount -gt 0) { "Yuan $amount" } else { "未提取" }
    $color = if ($amount -gt 0) { "White" } else { "Red" }
    Write-Host "  $($pdf.Name) -> $category | $statusText" -ForegroundColor $color
}

# 3. 统计 + Excel
$totalAmount = ($invoices | Measure-Object Amount -Sum).Sum
$validCount = ($invoices | Where-Object { $_.Amount -gt 0 }).Count

Write-Host ""
Write-Host "[3/4] 生成Excel统计表..." -ForegroundColor Yellow
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$excelFile = Join-Path $OUTPUT_DIR "发票分类统计_${timestamp}.xlsx"

$csvDetail = [System.IO.Path]::GetTempFileName() + "_detail.csv"
$detailRows = @()
foreach ($inv in $invoices) {
    $detailRows += [PSCustomObject]@{
        "序号"="$($detailRows.Count+1)"; "文件路径"="$($inv.FilePath)"; "物品/项目"="$($inv.ItemName)"
        "类别"="$($inv.Category)"; "金额(元)"=$inv.Amount; "解析状态"="$($inv.Status)"
    }
}
$detailRows | Export-Csv -Path $csvDetail -NoTypeInformation -Encoding UTF8

$csvSummary = [System.IO.Path]::GetTempFileName() + "_summary.csv"
$catStats = @{}
foreach ($inv in $invoices) {
    $cat = $inv.Category
    if (-not $catStats.ContainsKey($cat)) { $catStats[$cat] = @{Count=0;Total=0} }
    $catStats[$cat].Count++; $catStats[$cat].Total += $inv.Amount
}
$sortedCats = $catStats.GetEnumerator() | Sort-Object { $_.Value.Total } -Descending
$summaryRows = foreach ($entry in $sortedCats) {
    [PSCustomObject]@{
        "类别"="$($entry.Key)"; "笔数"="$($entry.Value.Count)"; "金额(元)"=$entry.Value.Total
        "占比(%)"=if($totalAmount -gt 0){[math]::Round($entry.Value.Total/$totalAmount*100,1)}else{0}
        "平均金额"=if($entry.Value.Count -gt 0){[math]::Round($entry.Value.Total/$entry.Value.Count,2)}else{0}
    }
}
$summaryRows | Export-Csv -Path $csvSummary -NoTypeInformation -Encoding UTF8

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

# 4. 生成分类HTML（可直接浏览器打开，Ctrl+P打印为PDF）
Write-Host ""
Write-Host "[4/4] 生成分类汇总HTML（可打印为PDF）..." -ForegroundColor Cyan

$htmlTemplate = @"
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>{TITLE}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: "Microsoft YaHei", sans-serif; padding: 20px; background: #f5f5f5; color: #333; }}
        .container {{ max-width: 800px; margin: 0 auto; background: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
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
        $invoicesHtml += "<p><b>物品:</b> $($inv1.ItemName)</p>"
        $invoicesHtml += "<p class='amount'>金额: ¥$($inv1.Amount.ToString('N2'))</p>"
        $invoicesHtml += "<p><b>文件:</b> $($inv1.FileName)</p>"
        $invoicesHtml += "<p><b>路径:</b> $($inv1.FilePath)</p>"
        $invoicesHtml += "</div>"
        
        # 右半张
        if ($inv2) {
            $invoicesHtml += "<div class='invoice' style='flex:1;'>"
            $invoicesHtml += "<h3>发票 $($i+2)</h3>"
            $invoicesHtml += "<p><b>物品:</b> $($inv2.ItemName)</p>"
            $invoicesHtml += "<p class='amount'>金额: ¥$($inv2.Amount.ToString('N2'))</p>"
            $invoicesHtml += "<p><b>文件:</b> $($inv2.FileName)</p>"
            $invoicesHtml += "<p><b>路径:</b> $($inv2.FilePath)</p>"
            $invoicesHtml += "</div>"
        }
        
        $invoicesHtml += "</div>"
    }
    
    $catTotal = ($catInvoices | Measure-Object Amount -Sum).Sum
    $html = $htmlTemplate -replace "{TITLE}", "$cat - 发票汇总（共 $($catInvoices.Count) 张）"
    $html = $html -replace "{META}", "生成日期: $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
    $html = $html -replace "{INVOICES}", $invoicesHtml
    $html = $html -replace "{TOTAL}", "¥$catTotal.ToString('N2')"
    
    [System.IO.File]::WriteAllText($outputHtml, $html, [System.Text.Encoding]::UTF8)
    Write-Host "    -> $outputHtml" -ForegroundColor Green
}

# 完成
Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  全部完成！" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  输出目录: $OUTPUT_DIR"
Write-Host ""
Write-Host "生成的文件:" -ForegroundColor Yellow
Get-ChildItem -Path $OUTPUT_DIR | ForEach-Object {
    $size = if ($_.Length -gt 1MB) { "$([math]::Round($_.Length/1MB,1)) MB" } else { "$([math]::Round($_.Length/1KB,1)) KB" }
    Write-Host "  $($_.Name) ($size)" -ForegroundColor White
}
Write-Host ""
Write-Host "提示: HTML文件可在浏览器打开后 Ctrl+P 打印为PDF" -ForegroundColor Yellow
Write-Host ""
Write-Host "按任意键退出..." -ForegroundColor Gray
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
