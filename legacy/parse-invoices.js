/**
 * 中国增值税发票PDF解析器 - 纯Node.js无依赖
 * 使用zlib原生解压 + 智能文本提取
 */

const fs = require('fs');
const path = require('path');
const zlib = require('zlib');

/**
 * 解析PDF对象树
 */
function parsePDFObjects(buffer) {
    const content = buffer.toString('binary');
    const objects = {};
    
    // 提取所有间接对象
    const objPattern = /(\d+)\s+0\s+obj\s+<<([\s\S]*?)>>\s*endobj/g;
    let match;
    while ((match = objPattern.exec(content)) !== null) {
        objects[match[1]] = {
            dict: match[2],
            raw: match[0]
        };
    }
    
    return objects;
}

/**
 * 解压PDF流
 */
function decompressStream(streamData) {
    try {
        const buf = Buffer.from(streamData, 'binary');
        
        // 尝试多种解压方式
        const methods = [
            () => zlib.inflateSync(buf),
            () => zlib.inflateRawSync(buf),
            () => zlib.gunzipSync(buf),
        ];
        
        for (const method of methods) {
            try {
                return method().toString('utf8');
            } catch (e) {}
        }
    } catch (e) {}
    
    return '';
}

/**
 * 从PDF中提取所有文本
 */
function extractAllText(pdfPath) {
    try {
        const buffer = fs.readFileSync(pdfPath);
        const content = buffer.toString('binary');
        let allText = '';
        
        // 方法1: 提取所有stream并解压
        const streamRegex = /stream\r?\n([\s\S]*?)\r?\nendstream/g;
        let streamMatch;
        while ((streamMatch = streamRegex.exec(content)) !== null) {
            const streamData = streamMatch[1];
            if (streamData.length > 50) {  // 跳过太小的流
                const text = decompressStream(streamData);
                if (text) {
                    allText += text + '\n';
                }
            }
        }
        
        // 方法2: 直接提取括号文本（未压缩的）
        if (allText.length < 100) {
            const parenRegex = /\(([^)]{3,})\)/g;
            let parenMatch;
            while ((parenMatch = parenRegex.exec(content)) !== null) {
                allText += parenMatch[1] + ' ';
            }
        }
        
        // 方法3: 提取Text Block (BT...ET)
        if (allText.length < 100) {
            const btRegex = /BT[\s\S]{10,10000}?ET/g;
            let btMatch;
            while ((btMatch = btRegex.exec(content)) !== null) {
                const textMatches = btMatch[0].match(/\(([^)]*)\)\s*Tj/g);
                if (textMatches) {
                    for (const tm of textMatches) {
                        const m = tm.match(/\(([^)]*)\)/);
                        if (m) allText += m[1] + ' ';
                    }
                }
            }
        }
        
        return allText.trim();
    } catch (e) {
        console.error(`PDF读取错误: ${e.message}`);
        return '';
    }
}

/**
 * 解码PDF文本（处理各种编码）
 */
function decodePDFText(rawText) {
    if (!rawText || rawText.length < 10) return '';
    
    // 检查是否已经是可读文本
    if (/[\u4e00-\u9fff]{2,}/.test(rawText)) {
        return rawText;
    }
    
    // 尝试 UTF-16 BE 解码
    if (rawText.includes('\xFE\xFF') || rawText.includes('\xFF\xFE')) {
        try {
            const bytes = [];
            for (let i = 0; i < rawText.length; i++) {
                const code = rawText.charCodeAt(i);
                if (code > 0 && code < 256) bytes.push(code);
            }
            const decoded = Buffer.from(bytes).toString('utf16le');
            if (/[\u4e00-\u9fff]/.test(decoded)) {
                return decoded;
            }
        } catch (e) {}
    }
    
    // 尝试 GBK 解码
    try {
        const bytes = [];
        for (let i = 0; i < rawText.length; i++) {
            const code = rawText.charCodeAt(i);
            if (code > 0 && code < 256) bytes.push(code);
        }
        
        // 简单GBK映射（仅常用字符）
        const decoded = Buffer.from(bytes).toString('ucs2');
        if (/[\u4e00-\u9fff]/.test(decoded)) {
            return decoded;
        }
    } catch (e) {}
    
    // 返回原始文本
    return rawText;
}

/**
 * 提取金额（针对中国增值税发票优化）
 */
function extractAmount(text) {
    if (!text || text.length < 10) return 0;
    
    // 优先查找"价税合计"后面的金额
    const priceTotalIdx = text.indexOf('价税合计');
    if (priceTotalIdx >= 0) {
        const afterText = text.substring(priceTotalIdx, priceTotalIdx + 200);
        const amountMatch = afterText.match(/[\d,]+\.\d{2}/);
        if (amountMatch) {
            const amount = parseFloat(amountMatch[0].replace(/,/g, ''));
            if (amount > 0 && amount < 100000000) return amount;
        }
    }
    
    // 查找"小写"后面的金额
    const lowerIdx = text.indexOf('小写');
    if (lowerIdx >= 0) {
        const afterText = text.substring(lowerIdx, lowerIdx + 200);
        const amountMatch = afterText.match(/[\d,]+\.\d{2}/);
        if (amountMatch) {
            const amount = parseFloat(amountMatch[0].replace(/,/g, ''));
            if (amount > 0 && amount < 100000000) return amount;
        }
    }
    
    // 查找 ¥ 或 ￥ 后面的金额
    const yenMatch = text.match(/[¥￥]\s*([\d,]+\.\d{2})/);
    if (yenMatch) {
        const amount = parseFloat(yenMatch[1].replace(/,/g, ''));
        if (amount > 0 && amount < 100000000) return amount;
    }
    
    // 查找所有金额格式，取合理的最大值
    const allAmounts = text.match(/\d{1,3}(?:,\d{3})*\.\d{2}/g);
    if (allAmounts) {
        const valid = allAmounts
            .map(a => parseFloat(a.replace(/,/g, '')))
            .filter(a => a > 1 && a < 100000000);
        
        if (valid.length > 0) {
            // 通常价税合计是较大的数字
            return Math.max(...valid);
        }
    }
    
    return 0;
}

/**
 * 提取货物/服务名称
 */
function extractItemName(text) {
    if (!text) return '';
    
    // 匹配 "*XXX*" 格式
    const starMatch = text.match(/\*([^*]+)\*/);
    if (starMatch && starMatch[1].length > 1 && starMatch[1].length < 50) {
        return starMatch[1].trim();
    }
    
    // 匹配"货物或应税劳务、服务名称"后面的内容
    const goodsMatch = text.match(/货物或应税劳务[\s\S]*?名称[\s:：]*([^\s\n\r]{2,50})/);
    if (goodsMatch && goodsMatch[1].length > 1) {
        return goodsMatch[1].trim();
    }
    
    // 匹配"项目名称"
    const projectMatch = text.match(/项目[\s\S]*?名称?[\s:：]*([^\s\n\r]{2,50})/);
    if (projectMatch && projectMatch[1].length > 1) {
        return projectMatch[1].trim();
    }
    
    return '';
}

/**
 * 提取发票号码
 */
function extractInvoiceNumber(text) {
    if (!text) return '';
    
    const match = text.match(/发票号码[\s:：]*(\d{8,20})/);
    if (match) return match[1];
    
    const noMatch = text.match(/No[\s.:]*(\d{8,20})/i);
    if (noMatch) return noMatch[1];
    
    return '';
}

/**
 * 提取开票日期
 */
function extractDate(text) {
    if (!text) return '';
    
    const match = text.match(/(\d{4})[-年](\d{1,2})[-月](\d{1,2})[日]?/);
    if (match) {
        return `${match[1]}-${parseInt(match[2]).toString().padStart(2,'0')}-${parseInt(match[3]).toString().padStart(2,'0')}`;
    }
    
    return '';
}

/**
 * 提取销售方名称
 */
function extractSeller(text) {
    if (!text) return '';
    
    const match = text.match(/销售方[\s\S]*?名称[\s:：]*([^\s税号地址]{2,50}?)(?:\s{2,}|纳税人识别号|地址|$)/);
    if (match) return match[1].trim();
    
    return '';
}

/**
 * 分类规则（精确匹配）
 */
const CATEGORIES = {
    "差旅费": ["住宿", "酒店", "宾馆", "差旅", "机票", "火车票"],
    "餐饮费": ["餐饮", "餐费"],
    "汽车费用": ["加油", "油费", "停车费", "过路费", "过桥费", "高速公路", "通行费"],
    "福利费": ["化妆品", "粮油", "服装", "鞋帽", "药品", "福利"],
    "业务招待费": ["礼品", "烟酒", "茶叶", "奢侈", "娱乐", "旅游"],
    "办公费用": ["办公", "文具", "打印", "纸张", "电脑", "手机", "电信", "网络", "快递", "桶装水"],
    "广告费用": ["广告", "宣传", "会务", "标签", "瓶身"],
    "培训费": ["培训", "学费", "教育"],
    "材料设备": ["材料", "原料", "设备", "泵", "膜", "五金", "工具", "配件", "机械", "仪器", "超滤", "热缩", "风机", "吨桶", "硅藻土", "制冷机"],
};

function categorize(itemName, fileName) {
    const text = `${itemName} ${fileName}`;
    
    for (const [category, keywords] of Object.entries(CATEGORIES)) {
        for (const kw of keywords) {
            if (text.includes(kw)) {
                return category;
            }
        }
    }
    
    // 默认分类
    if (text.includes('顺丰') || text.includes('快递') || text.includes('电信')) return "办公费用";
    if (text.includes('实验室') || text.includes('制冷')) return "材料设备";
    
    return "其他费用";
}

/**
 * 解析单个PDF发票
 */
function parseInvoice(pdfPath) {
    const fileName = path.basename(pdfPath);
    const rawText = extractAllText(pdfPath);
    const text = decodePDFText(rawText);
    
    const result = {
        filePath: pdfPath,
        fileName: fileName,
        textLength: text.length,
        amount: 0,
        itemName: '',
        invoiceNumber: '',
        date: '',
        seller: '',
        category: '',
        parseStatus: '未提取',
    };
    
    if (text && text.length > 10) {
        result.amount = extractAmount(text);
        result.itemName = extractItemName(text);
        result.invoiceNumber = extractInvoiceNumber(text);
        result.date = extractDate(text);
        result.seller = extractSeller(text);
        result.parseStatus = result.amount > 0 ? '成功' : '未提取到金额';
    }
    
    // 备用：从文件名提取金额
    if (result.amount === 0) {
        const nameMatch = fileName.match(/(\d+(?:,\d{3})*(?:\.\d{1,2})?)\s*$/);
        if (nameMatch) {
            result.amount = parseFloat(nameMatch[1].replace(/,/g, ''));
            if (result.parseStatus === '未提取') {
                result.parseStatus = '从文件名提取';
            }
        }
    }
    
    // 备用：从文件名提取物品名
    if (!result.itemName) {
        const namePart = fileName.replace(/[\d,_\-\.]+$/, '').replace(/^[_\d]+/, '');
        result.itemName = namePart.trim() || fileName.replace('.pdf', '');
    }
    
    result.category = categorize(result.itemName, fileName);
    
    return { result, text };
}

// ============ 主程序 ============
const folder = process.argv[2];
if (!folder) {
    console.error('用法: node parse-invoices.js <文件夹路径>');
    process.exit(1);
}

function scanPDFs(dir) {
    let results = [];
    const list = fs.readdirSync(dir);
    
    for (const file of list) {
        const filePath = path.join(dir, file);
        const stat = fs.statSync(filePath);
        
        if (stat.isDirectory()) {
            results = results.concat(scanPDFs(filePath));
        } else if (file.toLowerCase().endsWith('.pdf')) {
            results.push(filePath);
        }
    }
    
    return results;
}

const pdfFiles = scanPDFs(folder);
console.log(`\n找到 ${pdfFiles.length} 个PDF文件\n`);

const invoices = [];
const allTexts = {};

for (let i = 0; i < pdfFiles.length; i++) {
    const pdf = pdfFiles[i];
    const { result, text } = parseInvoice(pdf);
    invoices.push(result);
    allTexts[result.fileName] = text;
    
    const status = result.amount > 0 ? `¥${result.amount.toFixed(2)}` : result.parseStatus;
    console.log(`[${i + 1}/${pdfFiles.length}] ${result.fileName}`);
    console.log(`  -> ${result.category} | ${status} | ${result.itemName}`);
    if (result.date) console.log(`  -> 日期: ${result.date}`);
    if (result.invoiceNumber) console.log(`  -> 号码: ${result.invoiceNumber}`);
    if (result.seller) console.log(`  -> 销售方: ${result.seller}`);
    if (!result.amount && text.length < 50) {
        console.log(`  -> [文本提取失败, length=${text.length}]`);
    }
    console.log('');
}

// 输出JSON
const outputDir = path.join(folder, '分类合并结果');
if (!fs.existsSync(outputDir)) {
    fs.mkdirSync(outputDir, { recursive: true });
}
const outputFile = path.join(outputDir, 'invoices.json');
fs.writeFileSync(outputFile, JSON.stringify(invoices, null, 2), 'utf8');

// 也保存所有提取的文本（用于调试）
const textFile = path.join(outputDir, 'extracted_texts.json');
fs.writeFileSync(textFile, JSON.stringify(allTexts, null, 2), 'utf8');

const totalAmount = invoices.reduce((sum, inv) => sum + inv.amount, 0);
const validCount = invoices.filter(inv => inv.amount > 0).length;

console.log(`\n总计: ${invoices.length} 张发票, 成功 ${validCount} 张, 总金额 ¥${totalAmount.toFixed(2)}`);
console.log(`JSON已保存至: ${outputFile}`);
console.log(`文本已保存至: ${textFile}`);
