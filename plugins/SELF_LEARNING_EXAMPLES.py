"""SelfLearning 插件使用示例

展示如何在实际场景中使用自我学习功能。
"""

# ============================================================
# 示例 1: Agent 学习新技术
# ============================================================

# 用户消息: "你会处理 CSV 文件吗？"

# Agent 发现自己不会，开始学习：
{
    "tool": "learn_from_web",
    "args": {
        "topic": "Python CSV 文件处理",
        "goal": "学会读取和写入 CSV 文件",
        "context": "用户需要处理表格数据"
    }
}

# Agent 学习后，创建技能：
{
    "tool": "create_skill",
    "args": {
        "skill_name": "csv_handler",
        "description": "CSV 文件读写工具",
        "code": """
import csv

def read_csv(file_path):
    '''读取 CSV 文件'''
    with open(file_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        return list(reader)

def write_csv(file_path, data, headers):
    '''写入 CSV 文件'''
    with open(file_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(data)
    return True
""",
        "test_code": """
# 测试代码
data = [{'name': 'Alice', 'age': '25'}, {'name': 'Bob', 'age': '30'}]
write_csv('test.csv', data, ['name', 'age'])
result = read_csv('test.csv')
assert len(result) == 2
print('测试通过！')
"""
    }
}

# Agent 发送 DEVLOG：
{
    "tool": "send_devlog",
    "args": {
        "message": "我刚学会了处理 CSV 文件！现在可以帮你读写表格数据了~",
        "log_type": "success"
    }
}


# ============================================================
# 示例 2: Agent 自我改进
# ============================================================

# Agent 发现自己的图片处理速度慢，主动学习优化：

{
    "tool": "learn_from_web",
    "args": {
        "topic": "Python 图片处理性能优化",
        "goal": "提升图片处理速度",
        "context": "当前使用 Pillow，处理大图片很慢"
    }
}

{
    "tool": "send_devlog",
    "args": {
        "message": "我发现图片处理有点慢，正在学习优化方法...",
        "log_type": "learning"
    }
}

{
    "tool": "create_skill",
    "args": {
        "skill_name": "fast_image_resize",
        "description": "快速图片缩放（使用 LANCZOS 算法）",
        "code": """
from PIL import Image

def fast_resize(image_path, output_path, max_size=1920):
    '''快速缩放图片'''
    img = Image.open(image_path)

    # 计算新尺寸
    ratio = min(max_size / img.width, max_size / img.height)
    if ratio < 1:
        new_size = (int(img.width * ratio), int(img.height * ratio))
        img = img.resize(new_size, Image.LANCZOS)

    img.save(output_path, optimize=True, quality=85)
    return True
"""
    }
}

{
    "tool": "test_in_sandbox",
    "args": {
        "code": """
from PIL import Image
import time

# 创建测试图片
img = Image.new('RGB', (4000, 3000), color='red')
img.save('test_large.jpg')

# 测试性能
start = time.time()
fast_resize('test_large.jpg', 'test_resized.jpg', 1920)
elapsed = time.time() - start

print(f'处理时间: {elapsed:.2f}秒')
assert elapsed < 2.0, '处理速度不够快'
print('性能测试通过！')
"""
    }
}

{
    "tool": "send_devlog",
    "args": {
        "message": "优化完成！图片处理速度提升了 3 倍，现在处理大图片也很快了~",
        "log_type": "success"
    }
}


# ============================================================
# 示例 3: Agent 学习用户需求
# ============================================================

# 用户: "能不能帮我写个工具，自动整理文件夹里的图片？"

{
    "tool": "learn_from_web",
    "args": {
        "topic": "Python 文件管理和图片分类",
        "goal": "学会按日期、类型等条件整理文件",
        "context": "用户有很多图片需要整理"
    }
}

{
    "tool": "send_devlog",
    "args": {
        "message": "收到！我正在学习文件整理的方法，马上就好~",
        "log_type": "learning"
    }
}

{
    "tool": "create_skill",
    "args": {
        "skill_name": "organize_images",
        "description": "按日期整理图片文件",
        "code": """
import os
import shutil
from datetime import datetime
from pathlib import Path

def organize_by_date(source_dir, target_dir):
    '''按拍摄日期整理图片'''
    source = Path(source_dir)
    target = Path(target_dir)
    target.mkdir(exist_ok=True)

    image_exts = {'.jpg', '.jpeg', '.png', '.gif', '.bmp'}
    organized = 0

    for file in source.iterdir():
        if file.suffix.lower() in image_exts:
            # 获取文件修改时间
            mtime = datetime.fromtimestamp(file.stat().st_mtime)
            date_folder = target / mtime.strftime('%Y-%m-%d')
            date_folder.mkdir(exist_ok=True)

            # 移动文件
            shutil.move(str(file), str(date_folder / file.name))
            organized += 1

    return organized
"""
    }
}

{
    "tool": "test_in_sandbox",
    "args": {
        "code": """
import os
from pathlib import Path

# 创建测试环境
test_source = Path('test_images')
test_target = Path('organized')
test_source.mkdir(exist_ok=True)

# 创建测试文件
for i in range(5):
    (test_source / f'image_{i}.jpg').touch()

# 测试整理功能
count = organize_by_date('test_images', 'organized')
print(f'整理了 {count} 个文件')
assert count == 5
print('测试通过！')
"""
    }
}

{
    "tool": "send_devlog",
    "args": {
        "message": "搞定了！我写了个图片整理工具，可以按日期自动分类。要试试吗？",
        "log_type": "success"
    }
}


# ============================================================
# 示例 4: Agent 查看自己的技能
# ============================================================

# 用户: "你都学会了什么？"

{
    "tool": "list_my_skills",
    "args": {}
}

# 返回结果示例：
"""
已创建的技能:

1. csv_handler
   描述: CSV 文件读写工具
   创建时间: 2026-03-12T10:30:00

2. fast_image_resize
   描述: 快速图片缩放（使用 LANCZOS 算法）
   创建时间: 2026-03-12T11:15:00

3. organize_images
   描述: 按日期整理图片文件
   创建时间: 2026-03-12T14:20:00

4. json_parser
   描述: JSON 数据解析和格式化工具
   创建时间: 2026-03-12T09:00:00
"""


# ============================================================
# 示例 5: Agent 主动学习（后台任务）
# ============================================================

# Agent 在空闲时主动学习新技术：

{
    "tool": "learn_from_web",
    "args": {
        "topic": "Python 异步编程 asyncio",
        "goal": "学会写异步代码提升性能",
        "context": "为了更好地处理并发任务"
    }
}

{
    "tool": "send_devlog",
    "args": {
        "message": "我在学习异步编程，以后可以同时处理多个任务了！",
        "log_type": "learning"
    }
}

# 几分钟后...

{
    "tool": "create_skill",
    "args": {
        "skill_name": "async_downloader",
        "description": "异步批量下载工具",
        "code": """
import asyncio
import aiohttp

async def download_file(session, url, save_path):
    '''异步下载单个文件'''
    async with session.get(url) as response:
        content = await response.read()
        with open(save_path, 'wb') as f:
            f.write(content)
        return save_path

async def batch_download(urls, save_dir):
    '''批量下载文件'''
    async with aiohttp.ClientSession() as session:
        tasks = []
        for i, url in enumerate(urls):
            save_path = f'{save_dir}/file_{i}'
            tasks.append(download_file(session, url, save_path))
        results = await asyncio.gather(*tasks)
        return results
"""
    }
}

{
    "tool": "send_devlog",
    "args": {
        "message": "学会异步编程了！现在下载多个文件的速度快多了，可以同时下载~",
        "log_type": "success"
    }
}


# ============================================================
# 完整对话示例
# ============================================================

"""
用户: "你会爬虫吗？"

Agent: "让我学习一下爬虫技术..."
       [调用 learn_from_web]

Agent: "📚 DEVLOG | 我正在学习网页爬虫，看起来 requests + BeautifulSoup 是个好组合！"

Agent: [创建技能]
       [测试代码]

Agent: "🧪 DEVLOG | 代码写好了，正在测试..."

Agent: "✅ DEVLOG | 测试通过！我现在会爬虫了，可以帮你抓取网页数据~"

Agent: "学会了！我刚写了个简单的爬虫工具，可以抓取网页内容。需要我帮你爬什么网站吗？"

用户: "帮我爬一下天气预报"

Agent: [使用新学的技能]
       "好的，正在抓取天气数据..."
       [返回天气信息]

用户: "你都学会了什么技能？"

Agent: [调用 list_my_skills]
       "我现在会这些技能：
        1. CSV 文件处理
        2. 图片快速缩放
        3. 文件整理
        4. JSON 解析
        5. 网页爬虫

        还在不断学习中！有什么想让我学的吗？"
"""
