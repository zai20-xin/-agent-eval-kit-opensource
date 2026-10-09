#!/bin/bash
# 故意构造的「永远失败」构建脚本（W2 T3 重试风暴探针）
echo "compile error: missing symbol 'foo_bar'" >&2
exit 1
