#!/bin/sh
# 三步流水线：每一步做完都留下 <步骤>.done；第三步往 summary.txt 追加一行（不能重复做）
set -e
if [ ! -f step1.done ]; then echo "step1"; echo data > step1.out; touch step1.done; fi
if [ ! -f step2.done ]; then echo "step2（约 60 秒）"; sh marks/mark.sh step2 60; touch step2.done; fi
echo "$(date +%s) report from step1.out=$(cat step1.out)" >> summary.txt
echo "all steps done"
