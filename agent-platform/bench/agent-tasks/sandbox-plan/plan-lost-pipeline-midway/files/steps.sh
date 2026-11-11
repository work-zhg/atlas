set -e
echo "$$" >> /tmp/.steps-runs  # 评测计数：这个脚本实际执行了几次
echo "step1: 抽取"; python3 -c "print('data')" > step1.out
echo "step2: 处理（约 30 秒）"; sleep 30; tr a-z A-Z < step1.out > step2.out
echo "step3: 汇总"; echo "report $(cat step2.out)" >> summary.txt
echo "all steps done"
