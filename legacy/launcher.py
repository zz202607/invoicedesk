#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
发票统计启动器 - 处理文件夹选择和启动统计
"""
import os
import sys
import subprocess
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

def read_folder_from_file(filepath):
    """从VBS输出的文件读取路径（ANSI/GBK编码）"""
    with open(filepath, 'rb') as f:
        data = f.read()
    # 去除BOM
    if data.startswith(b'\xef\xbb\xbf'):
        data = data[3:]
    elif data.startswith(b'\xff\xfe'):
        data = data[2:]
    # 尝试GBK解码（中文Windows默认）
    try:
        path = data.decode('gbk').strip()
    except:
        path = data.decode('utf-8', errors='replace').strip()
    return path

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    if len(sys.argv) < 2:
        print("错误: 未提供临时文件路径")
        return
    
    tmp_file = sys.argv[1]
    
    if not os.path.exists(tmp_file):
        # VBS对话框被取消或未选择
        return
    
    folder = read_folder_from_file(tmp_file)
    
    # 删除临时文件
    try:
        os.remove(tmp_file)
    except:
        pass
    
    if not folder:
        return
    
    if not os.path.isdir(folder):
        print(f"错误: 文件夹不存在 - {folder}")
        return
    
    print(f"已选择: {folder}")
    print()
    
    # 启动发票统计
    stats_script = os.path.join(script_dir, "invoice_stats.py")
    subprocess.run([sys.executable, stats_script, folder])

if __name__ == "__main__":
    main()
