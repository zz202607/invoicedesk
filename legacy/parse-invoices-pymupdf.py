#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国增值税发票 PDF 解析器
使用 PyMuPDF (fitz) 正确解析自定义编码的 PDF
"""

import os
import sys
import io
import json
import re

# 设置标准输出编码为 UTF-8
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

try:
    import fitz  # PyMuPDF
except ImportError:
    print("错误: 未安装 PyMuPDF")
    print("请运行: python -m pip install PyMuPDF")
    sys.exit(1)


def extract_text_from_pdf(pdf_path):
    """使用 PyMuPDF 提取 PDF 文本"""
    try:
        doc = fitz.open(pdf_path)
        text = ""
        for page in doc:
            text += page.get_text()
        doc.close()
        return text
    except Exception as e:
        return f"[解析错误: {str(e)}]"


def extract_amount(text):
    """从发票文本中提取价税合计金额"""
    if not text or len(text) < 10:
        return 0
    
    # 中国增值税发票金额模式（按优先级）
    patterns = [
        # 价税合计（小写）
        r'价税合计.*?[¥￥]?\s*([\d,]+\.\d{2})',
        r'价税合计.*?([\d,]+\.\d{2})\s*元',
        
        # 小写
        r'小写.*?[¥￥]?\s*([\d,]+\.\d{2})',
        r'小写.*?([\d,]+\.\d{2})\s*元',
        
        # 合计
        r'合计.*?[¥￥]?\s*([\d,]+\.\d{2})',
        r'合计.*?([\d,]+\.\d{2})\s*元',
        
        # ¥ 符号
        r'[¥￥]\s*([\d,]+\.\d{2})',
        r'([\d,]+\.\d{2})\s*元',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if match:
            amount_str = match.group(1).replace(',', '')
            try:
                amount = float(amount_str)
                if 0 < amount < 100000000:
                    return amount
            except ValueError:
                continue
    
    # 备用：找所有金额，取合理范围内最大的
    all_amounts = re.findall(r'[\d,]+\.\d{2}', text)
    if all_amounts:
        valid = []
        for a in all_amounts:
            try:
                val = float(a.replace(',', ''))
                if 1 < val < 100000000:
                    valid.append(val)
            except ValueError:
                pass
        
        if valid:
            # 优先取"价税合计"后面的数字
            price_total_idx = text.find('价税合计')
            if price_total_idx >= 0:
                after_text = text[price_total_idx:]
                after_amounts = re.findall(r'[\d,]+\.\d{2}', after_text)
                if after_amounts:
                    try:
                        return float(after_amounts[0].replace(',', ''))
                    except ValueError:
                        pass
            
            return max(valid)
    
    return 0


def extract_item_name(text):
    """提取货物/服务名称"""
    if not text:
        return ''
    
    patterns = [
        # *XXX* 格式
        r'\*([^*]+)\*\s*([^\s\n\r]{2,50})',
        
        # 货物或应税劳务名称
        r'货物或应税劳务[\s\S]*?名称[\s:：]*([^\s\n\r]{2,50})',
        
        # 项目名称
        r'项目[\s\S]*?名称?[\s:：]*([^\s\n\r]{2,50})',
        
        # 品名
        r'品名[\s:：]*([^\s\n\r]{2,50})',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            name = (match.group(2) if match.lastindex >= 2 else match.group(1)).strip()
            if 1 < len(name) < 100:
                return name
    
    return ''


def extract_invoice_number(text):
    """提取发票号码"""
    if not text:
        return ''
    
    patterns = [
        r'发票号码[\s:：]*(\d{8,20})',
        r'No[\s.:]*(\d{8,20})',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    
    return ''


def extract_date(text):
    """提取开票日期"""
    if not text:
        return ''
    
    patterns = [
        r'(\d{4})年(\d{1,2})月(\d{1,2})日',
        r'(\d{4})-(\d{2})-(\d{2})',
        r'开票日期[\s:：]*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            if '年' in match.group(0):
                try:
                    return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
                except (ValueError, IndexError):
                    pass
            return match.group(1)
    
    return ''


def extract_seller(text):
    """提取销售方名称"""
    if not text:
        return ''
    
    patterns = [
        r'销售方[\s\S]*?名称[\s:：]*([^\s税号地址]{2,50}?)(?:\s{2,}|纳税人识别号|地址|开户行|$)',
        r'销\s*售\s*方[\s\S]*?名\s*称[\s:：]*([^\s税号]{2,50}?)(?:\s{2,}|纳税人识别号|$)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    
    return ''


# 分类规则（精确匹配）
CATEGORIES = {
    "差旅费": ["住宿", "酒店", "宾馆", "差旅", "机票", "火车票"],
    "餐饮费": ["餐饮", "餐费"],
    "汽车费用": ["加油", "油费", "停车费", "过路费", "过桥费", "高速公路", "通行费", "ETC"],
    "福利费": ["化妆品", "粮油", "服装", "鞋帽", "药品", "福利"],
    "业务招待费": ["礼品", "烟酒", "茶叶", "奢侈", "娱乐", "旅游"],
    "办公费用": ["办公", "文具", "打印", "纸张", "电脑", "手机", "电信", "网络", "快递", "桶装水", "顺丰"],
    "广告费用": ["广告", "宣传", "会务", "标签", "瓶身"],
    "培训费": ["培训", "学费", "教育"],
    "材料设备": ["材料", "原料", "设备", "泵", "膜", "五金", "工具", "配件", "机械", "仪器", "超滤", "热缩", "风机", "吨桶", "硅藻土", "制冷机", "硅"],
}


def categorize(item_name, file_name, full_text):
    """分类"""
    text = f"{item_name} {file_name} {full_text}".lower()
    
    for category, keywords in CATEGORIES.items():
        for kw in keywords:
            if kw.lower() in text:
                return category
    
    return "其他费用"


def parse_invoice(pdf_path):
    """解析单个PDF发票"""
    file_name = os.path.basename(pdf_path)
    text = extract_text_from_pdf(pdf_path)
    
    result = {
        "filePath": pdf_path,
        "fileName": file_name,
        "textLength": len(text),
        "amount": 0,
        "itemName": "",
        "invoiceNumber": "",
        "date": "",
        "seller": "",
        "category": "",
        "parseStatus": "未提取"
    }
    
    if text and len(text) > 10:
        result["amount"] = extract_amount(text)
        result["itemName"] = extract_item_name(text)
        result["invoiceNumber"] = extract_invoice_number(text)
        result["date"] = extract_date(text)
        result["seller"] = extract_seller(text)
        result["parseStatus"] = "成功" if result["amount"] > 0 else "未提取到金额"
    
    # 备用：从文件名提取金额
    if result["amount"] == 0:
        name_match = re.search(r'(\d+(?:,\d{3})*(?:\.\d{1,2})?)\s*$', file_name)
        if name_match:
            result["amount"] = float(name_match.group(1).replace(',', ''))
            if result["parseStatus"] == "未提取":
                result["parseStatus"] = "从文件名提取"
    
    # 备用：从文件名提取物品名
    if not result["itemName"]:
        name_part = re.sub(r'[\d,_\-\.]+$', '', file_name)
        name_part = re.sub(r'^[_\d]+', '', name_part)
        result["itemName"] = name_part.strip() or file_name.replace('.pdf', '')
    
    result["category"] = categorize(result["itemName"], file_name, text)
    
    return result, text


def scan_pdfs(directory):
    """递归扫描所有PDF文件"""
    pdf_files = []
    for root, dirs, files in os.walk(directory):
        for f in files:
            if f.lower().endswith('.pdf'):
                pdf_files.append(os.path.join(root, f))
    return pdf_files


def main():
    if len(sys.argv) < 2:
        print("用法: python parse-invoices-pymupdf.py <文件夹路径>")
        sys.exit(1)
    
    folder = sys.argv[1]
    if not os.path.isdir(folder):
        print(f"错误: 文件夹不存在 - {folder}")
        sys.exit(1)
    
    # 扫描PDF
    pdf_files = scan_pdfs(folder)
    print(f"\n找到 {len(pdf_files)} 个PDF文件\n")
    
    # 解析
    invoices = []
    all_texts = {}
    
    for i, pdf_path in enumerate(pdf_files, 1):
        result, text = parse_invoice(pdf_path)
        invoices.append(result)
        all_texts[result["fileName"]] = text[:500]  # 只保存前500字符用于调试
        
        status = f"¥{result['amount']:.2f}" if result["amount"] > 0 else result["parseStatus"]
        print(f"[{i}/{len(pdf_files)}] {result['fileName']}")
        print(f"  -> {result['category']} | {status} | {result['itemName']}")
        if result["date"]:
            print(f"  -> 日期: {result['date']}")
        if result["invoiceNumber"]:
            print(f"  -> 号码: {result['invoiceNumber']}")
        if result["seller"]:
            print(f"  -> 销售方: {result['seller']}")
        print()
    
    # 输出JSON
    output_dir = os.path.join(folder, "分类合并结果")
    os.makedirs(output_dir, exist_ok=True)
    
    output_file = os.path.join(output_dir, "invoices.json")
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(invoices, f, ensure_ascii=False, indent=2)
    
    # 保存提取的文本（用于调试）
    text_file = os.path.join(output_dir, "extracted_texts.json")
    with open(text_file, 'w', encoding='utf-8') as f:
        json.dump(all_texts, f, ensure_ascii=False, indent=2)
    
    # 统计
    total_amount = sum(inv["amount"] for inv in invoices)
    valid_count = sum(1 for inv in invoices if inv["amount"] > 0)
    
    print(f"\n总计: {len(invoices)} 张发票, 成功 {valid_count} 张, 总金额 ¥{total_amount:.2f}")
    print(f"JSON已保存至: {output_file}")
    print(f"文本已保存至: {text_file}")


if __name__ == "__main__":
    main()
