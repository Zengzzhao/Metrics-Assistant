.DEFAULT_GOAL := help
UV ?= uv
CONFIG ?= run.toml

.PHONY: help install run start inspect resume continue replay
help:
	@echo '知识问答：make qa-install，然后分别运行 make qa-backend 和 make qa-frontend'
	@echo 'Neo4j 入库：make neo4j-import RESULT=outputs/result.json SOURCE=data/paper.md PAPER_ID=paper-001'
	@echo '先修改 run.toml 与 .env，再使用 make run/start/inspect/resume/continue/replay'
install:
	$(UV) sync
# make start     # 启动并在指定节点暂停
# make inspect   # 查看当前状态
# make resume    # 继续到下一个断点
# make continue # 执行剩余全部节点
# make run       # 完整运行
run start inspect resume continue replay:
	$(UV) run ie --config "$(CONFIG)" --action $@

# make neo4j-import RESULT=outputs/result.json SOURCE="data/paper.md" PAPER_ID=paper-001
RESULT ?= outputs/result.json
SOURCE ?=
PAPER_ID ?=
.PHONY: neo4j-import
neo4j-import:
	$(UV) run neo4j-import --result "$(RESULT)" --source "$(SOURCE)" $(if $(PAPER_ID),--paper-id "$(PAPER_ID)")

.PHONY: qa-backend qa-frontend qa-install qa-build
qa-backend:
	$(UV) run qa-backend
qa-install:
	$(UV) sync
	pnpm --dir src/app/frontend install --frozen-lockfile
qa-frontend:
	pnpm --dir src/app/frontend run dev
qa-build:
	pnpm --dir src/app/frontend run build
