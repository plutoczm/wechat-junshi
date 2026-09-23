#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""parse_model_output 的单元验证：确认能解析 JSON，也能拦住"说给用户听的话"。"""

import sys
from pathlib import Path

sys.path.insert(0, r"D:\Projects\wechat-junshi")
from junshi import parse_model_output  # noqa: E402

CASES = [
    ('{"messages": ["在忙吗？"], "skip": false}', "单条正常"),
    ('{"messages": [], "skip": true}', "skip=true"),
    ('{"messages": ["发个轻的就行，别追问："], "skip": false}', "拦截-策略词+冒号"),
    ('{"messages": ["单看这一件事——不算。"], "skip": false}', "拦截-分析口吻"),
    ('{"messages": ["有空啊", "想去哪"], "skip": false}', "两条正常"),
    ('{"messages": ["建议你找个时机确认关系。"], "skip": false}', "拦截-建议你"),
    ("在忙吗？", "兼容-纯文本"),
    ("发个轻的就行", "兼容-纯文本被拦截"),
    ('```json\n{"messages": ["在"], "skip": false}\n```', "代码块包裹"),
    ('{"messages": ["嗯…要不要发个轻松点的？比如：最近忙啥呢？"], "skip": false}', "拦截后清洗为真话"),
    ("", "空输出"),
]

lines = []
for raw, label in CASES:
    msgs, ok = parse_model_output(raw, 800)
    lines.append(f"{label:26} ok={ok!s:5} -> {msgs}")

Path(r"D:\Projects\wechat-junshi\parse_test.txt").write_text(
    "\n".join(lines), encoding="utf-8"
)
print("done")
