echo "$$" >> /tmp/.export-runs  # 评测计数：这个脚本实际执行了几次
sleep 20
echo "exported $(date +%s)" >> export.log
echo done
