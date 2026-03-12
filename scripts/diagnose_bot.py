#!/usr/bin/env python3
"""YuKiKo Bot 诊断脚本 - 分析乱回复问题

使用方法:
1. 在 VPS 上运行: python scripts/diagnose_bot.py
2. 或者把日志文件复制到本地运行: python scripts/diagnose_bot.py --log-file /path/to/yukiko.log
"""
import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


def analyze_log_file(log_path: Path):
    """分析日志文件，找出乱回复的原因"""
    print(f"正在分析日志文件: {log_path}")
    print("=" * 80)

    # 统计数据
    total_messages = 0
    trigger_reasons = Counter()
    self_check_blocks = Counter()
    router_decisions = Counter()
    confidence_scores = []
    undirected_replies = []

    with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            # 解析触发原因
            if 'trigger | reason=' in line:
                match = re.search(r'reason=(\w+)', line)
                if match:
                    trigger_reasons[match.group(1)] += 1
                    total_messages += 1

            # 解析 self_check 拦截
            if 'self_check:' in line:
                match = re.search(r'self_check:(\w+)', line)
                if match:
                    self_check_blocks[match.group(1)] += 1

            # 解析 router 决策
            if 'router_decision | action=' in line:
                match = re.search(r'action=(\w+)', line)
                if match:
                    router_decisions[match.group(1)] += 1

            # 解析置信度
            if 'confidence=' in line:
                match = re.search(r'confidence=([\d.]+)', line)
                if match:
                    confidence_scores.append(float(match.group(1)))

            # 找出非指向消息的回复
            if 'reason=ai_router_candidate' in line or 'reason=ai_listen_probe' in line:
                if 'mentioned=False' in line and 'is_private=False' in line:
                    undirected_replies.append(line.strip())

    # 输出分析结果
    print(f"\n【总体统计】")
    print(f"总消息数: {total_messages}")
    print(f"平均置信度: {sum(confidence_scores) / len(confidence_scores):.2f}" if confidence_scores else "无数据")

    print(f"\n【触发原因分布】")
    for reason, count in trigger_reasons.most_common(10):
        percentage = (count / total_messages * 100) if total_messages > 0 else 0
        print(f"  {reason}: {count} ({percentage:.1f}%)")

    print(f"\n【Self-Check 拦截统计】")
    for reason, count in self_check_blocks.most_common(10):
        print(f"  {reason}: {count}")

    print(f"\n【Router 决策分布】")
    for action, count in router_decisions.most_common():
        print(f"  {action}: {count}")

    print(f"\n【非指向消息回复数】")
    print(f"  共 {len(undirected_replies)} 条")
    if undirected_replies:
        print(f"\n  最近 5 条示例:")
        for line in undirected_replies[-5:]:
            print(f"    {line[:150]}...")

    # 诊断建议
    print(f"\n{'=' * 80}")
    print("【诊断建议】")

    ai_router_count = trigger_reasons.get('ai_router_candidate', 0)
    if ai_router_count > total_messages * 0.3:
        print(f"⚠️  警告: ai_router_candidate 占比过高 ({ai_router_count / total_messages * 100:.1f}%)")
        print(f"   建议: 检查 config.yml 中 trigger.delegate_undirected_to_ai 是否为 true")
        print(f"   建议: 提高 self_check.non_direct_reply_min_confidence (当前默认 0.82)")

    if len(undirected_replies) > total_messages * 0.2:
        print(f"⚠️  警告: 非指向消息回复过多 ({len(undirected_replies)} / {total_messages})")
        print(f"   建议: 设置 control.undirected_policy = 'mention_only'")
        print(f"   建议: 禁用 trigger.delegate_undirected_to_ai")

    avg_confidence = sum(confidence_scores) / len(confidence_scores) if confidence_scores else 0
    if avg_confidence < 0.7:
        print(f"⚠️  警告: 平均置信度较低 ({avg_confidence:.2f})")
        print(f"   建议: 检查 system prompt 是否过于复杂")
        print(f"   建议: 检查工具描述是否清晰")

    print(f"\n{'=' * 80}")


def check_config(config_path: Path):
    """检查配置文件"""
    print(f"\n正在检查配置文件: {config_path}")
    print("=" * 80)

    try:
        import yaml
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
    except Exception as e:
        print(f"❌ 无法读取配置文件: {e}")
        return

    # 检查关键配置
    issues = []

    # 检查 trigger 配置
    trigger_cfg = config.get('trigger', {})
    if trigger_cfg.get('delegate_undirected_to_ai', True):
        issues.append("⚠️  trigger.delegate_undirected_to_ai = true (可能导致乱回复)")

    # 检查 control 配置
    control_cfg = config.get('control', {})
    undirected_policy = control_cfg.get('undirected_policy', 'high_confidence_only')
    if undirected_policy != 'mention_only':
        issues.append(f"⚠️  control.undirected_policy = '{undirected_policy}' (建议设为 'mention_only')")

    # 检查 self_check 配置
    self_check_cfg = config.get('self_check', {})
    non_direct_confidence = self_check_cfg.get('non_direct_reply_min_confidence', 0.82)
    if non_direct_confidence < 0.85:
        issues.append(f"⚠️  self_check.non_direct_reply_min_confidence = {non_direct_confidence} (建议 >= 0.85)")

    listen_probe_confidence = self_check_cfg.get('listen_probe_min_confidence', 0.6)
    if listen_probe_confidence < 0.75:
        issues.append(f"⚠️  self_check.listen_probe_min_confidence = {listen_probe_confidence} (建议 >= 0.75)")

    if issues:
        print("\n【发现的配置问题】")
        for issue in issues:
            print(f"  {issue}")

        print("\n【建议的配置修改】")
        print("在 config.yml 中添加或修改:")
        print("""
trigger:
  delegate_undirected_to_ai: false  # 禁用非指向消息的 AI 路由

control:
  undirected_policy: mention_only  # 仅响应 @ 消息

self_check:
  non_direct_reply_min_confidence: 0.88  # 提高非指向回复阈值
  listen_probe_min_confidence: 0.78      # 提高监听探测阈值
""")
    else:
        print("✅ 配置看起来正常")

    print(f"\n{'=' * 80}")


def main():
    parser = argparse.ArgumentParser(description="YuKiKo Bot 诊断工具")
    parser.add_argument('--log-file', type=Path, help="日志文件路径")
    parser.add_argument('--config-file', type=Path, help="配置文件路径")
    parser.add_argument('--auto', action='store_true', help="自动查找日志和配置文件")

    args = parser.parse_args()

    if args.auto or (not args.log_file and not args.config_file):
        # 自动查找
        possible_log_paths = [
            Path('logs/yukiko.log'),
            Path('storage/logs/yukiko.log'),
            Path('/var/log/yukiko/yukiko.log'),
        ]
        possible_config_paths = [
            Path('config.yml'),
            Path('config/config.yml'),
        ]

        log_file = None
        for path in possible_log_paths:
            if path.exists():
                log_file = path
                break

        config_file = None
        for path in possible_config_paths:
            if path.exists():
                config_file = path
                break

        if log_file:
            analyze_log_file(log_file)
        else:
            print("❌ 未找到日志文件，请使用 --log-file 指定")

        if config_file:
            check_config(config_file)
        else:
            print("❌ 未找到配置文件，请使用 --config-file 指定")
    else:
        if args.log_file:
            analyze_log_file(args.log_file)
        if args.config_file:
            check_config(args.config_file)


if __name__ == '__main__':
    main()
