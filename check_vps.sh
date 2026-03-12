#!/bin/bash
# VPS诊断脚本 - 在VPS上运行此脚本检查YuKiKo状态

echo "=== YuKiKo 诊断脚本 ==="
echo ""

# 1. 检查代码版本
echo "1. 检查Git版本:"
cd /opt/YuKiKo && git log --oneline -3
echo ""

# 2. 检查是否有最新代码
echo "2. 检查是否需要更新:"
git fetch origin
git status
echo ""

# 3. 检查配置文件
echo "3. 检查记忆系统配置:"
if [ -f "config.yaml" ]; then
    echo "config.yaml 中的 memory 配置:"
    grep -A 10 "memory:" config.yaml || echo "未找到 memory 配置"
    echo ""
    echo "bot.allow_memory 配置:"
    grep -A 5 "bot:" config.yaml | grep "allow_memory" || echo "未找到 allow_memory 配置"
elif [ -f "config.yml" ]; then
    echo "config.yml 中的 memory 配置:"
    grep -A 10 "memory:" config.yml || echo "未找到 memory 配置"
    echo ""
    echo "bot.allow_memory 配置:"
    grep -A 5 "bot:" config.yml | grep "allow_memory" || echo "未找到 allow_memory 配置"
else
    echo "未找到配置文件"
fi
echo ""

# 4. 检查最新日志
echo "4. 检查最新日志 (最后50行):"
if [ -f "yukiko.log" ]; then
    tail -50 yukiko.log | grep -E "memory|related_memories|search_related|enable_vector" || echo "日志中未找到记忆相关信息"
elif [ -f "logs/yukiko.log" ]; then
    tail -50 logs/yukiko.log | grep -E "memory|related_memories|search_related|enable_vector" || echo "日志中未找到记忆相关信息"
else
    echo "未找到日志文件"
fi
echo ""

# 5. 检查数据库
echo "5. 检查记忆数据库:"
if [ -f "storage/memory.db" ]; then
    echo "数据库文件存在，大小:"
    ls -lh storage/memory.db
    echo ""
    echo "embeddings 表记录数:"
    sqlite3 storage/memory.db "SELECT COUNT(*) FROM embeddings;" 2>/dev/null || echo "无法查询数据库"
elif [ -f "data/memory.db" ]; then
    echo "数据库文件存在，大小:"
    ls -lh data/memory.db
    echo ""
    echo "embeddings 表记录数:"
    sqlite3 data/memory.db "SELECT COUNT(*) FROM embeddings;" 2>/dev/null || echo "无法查询数据库"
else
    echo "未找到记忆数据库文件"
fi
echo ""

# 6. 检查进程状态
echo "6. 检查YuKiKo进程:"
ps aux | grep -i yukiko | grep -v grep || echo "未找到运行中的进程"
echo ""

echo "=== 诊断完成 ==="
echo ""
echo "请将以上输出发送给开发者"
