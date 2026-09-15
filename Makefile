.DEFAULT_GOAL := help
UV ?= uv
CONFIG ?= run.toml

.PHONY: help install run start inspect resume continue
help:
	@echo '先修改 run.toml 与 .env，再使用 make run/start/inspect/resume/continue'
install:
	$(UV) sync
# make start     # 启动并在指定节点暂停
# make inspect   # 查看当前状态
# make resume    # 继续到下一个断点
# make continue # 执行剩余全部节点
# make run       # 完整运行
run start inspect resume continue:
	$(UV) run scimetrics --config "$(CONFIG)" --action $@
