#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF合并工具 - 按分类合并发票PDF
每个分类生成一个PDF，A4纸每张放两张发票
"""

import os
import sys
import io
import json
import math

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

try:
    import fitz  # PyMuPDF
except ImportError:
    print("错误: 未安装 PyMuPDF")
    sys.exit(1)


def create_merged_pdf(category_name, invoice_files, output_path):
    """
    合并PDF：每个分类一个PDF，A4纸每张放两张发票
    """
    # A4尺寸（点）
    A4_WIDTH = 595.28
    A4_HEIGHT = 841.89
    
    # 创建新PDF
    merged_doc = fitz.open()
    
    for i in range(0, len(invoice_files), 2):
        # 创建新的A4页面
        page = merged_doc.new_page(width=A4_WIDTH, height=A4_HEIGHT)
        
        # 第一张发票（上半部分）
        pdf1_path = invoice_files[i]
        if os.path.exists(pdf1_path):
            try:
                src_doc1 = fitz.open(pdf1_path)
                src_page1 = src_doc1[0]
                
                # 上半部分区域：y从0到420
                rect1 = fitz.Rect(20, 20, A4_WIDTH - 20, 410)
                page.show_pdf_page(rect1, src_doc1, 0)
                src_doc1.close()
            except Exception as e:
                page.insert_text((30, 50), f"发票 {i+1}: 解析失败 - {str(e)}", fontsize=10)
        else:
            page.insert_text((30, 50), f"发票 {i+1}: 文件不存在", fontsize=10)
        
        # 第二张发票（下半部分，如果有）
        if i + 1 < len(invoice_files):
            pdf2_path = invoice_files[i + 1]
            if os.path.exists(pdf2_path):
                try:
                    src_doc2 = fitz.open(pdf2_path)
                    src_page2 = src_doc2[0]
                    
                    # 下半部分区域：y从430到820
                    rect2 = fitz.Rect(20, 430, A4_WIDTH - 20, 820)
                    page.show_pdf_page(rect2, src_doc2, 0)
                    src_doc2.close()
                except Exception as e:
                    page.insert_text((30, 460), f"发票 {i+2}: 解析失败 - {str(e)}", fontsize=10)
            else:
                page.insert_text((30, 460), f"发票 {i+2}: 文件不存在", fontsize=10)
        
        # 添加分隔线
        page.draw_line(
            fitz.Point(20, 420),
            fitz.Point(A4_WIDTH - 20, 420),
            color=(0.7, 0.7, 0.7),
            width=1
        )
    
    # 保存（先删除旧文件）
    if os.path.exists(output_path):
        try:
            os.remove(output_path)
        except PermissionError:
            # 文件被占用，换个文件名
            base, ext = os.path.splitext(output_path)
            output_path = f"{base}_new{ext}"
    merged_doc.save(output_path)
    merged_doc.close()
    return output_path


def main():
    if len(sys.argv) < 3:
        print("用法: python merge_pdfs.py <JSON文件路径> <输出目录>")
        sys.exit(1)
    
    json_file = sys.argv[1]
    output_dir = sys.argv[2]
    
    if not os.path.exists(json_file):
        print(f"错误: JSON文件不存在 - {json_file}")
        sys.exit(1)
    
    os.makedirs(output_dir, exist_ok=True)
    
    # 读取发票数据
    with open(json_file, 'r', encoding='utf-8') as f:
        invoices = json.load(f)
    
    print(f"\n{'=' * 60}")
    print(f"  PDF合并工具")
    print(f"{'=' * 60}\n")
    print(f"读取 {len(invoices)} 张发票数据\n")
    
    # 按分类分组
    classified = {}
    for inv in invoices:
        cat = inv.get("category", "其他费用")
        if cat not in classified:
            classified[cat] = []
        classified[cat].append(inv)
    
    print(f"共 {len(classified)} 个分类\n")
    
    # 合并每个分类的PDF
    for cat, cat_invoices in classified.items():
        # 安全的文件名
        safe_cat = cat.replace("/", "_").replace("\\", "_").replace(":", "_")
        output_pdf = os.path.join(output_dir, f"{safe_cat}.pdf")
        
        invoice_files = [inv["filePath"] for inv in cat_invoices]
        
        print(f"合并 [{cat}] {len(cat_invoices)} 张发票...")
        
        try:
            actual_path = create_merged_pdf(cat, invoice_files, output_pdf)
            print(f"  -> {actual_path}")
        except Exception as e:
            print(f"  [错误] {str(e)}")
    
    print(f"\n{'=' * 60}")
    print(f"  完成！")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
