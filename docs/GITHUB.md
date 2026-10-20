# 上传 GitHub 前

GitHub 仓库：[aymanajal/datafly-patch-tool](https://github.com/aymanajal/datafly-patch-tool)，创建为私有仓库。首次提交仅包含脱敏源码、部署模板、文档及测试。

## 已区分的文件

可以发布：源码、脱敏 README、部署模板、开发过程及隔离测试。

不纳入 Git：`config.json`、密码摘要、运行目录、上传的业务 JAR、备份、日志、包含现场信息的截图、真实部署记录。真实地址记录在 `PRIVATE_DEPLOYMENT.md`，已加入 `.gitignore`。不要使用 `git add -f` 强行添加这些文件。

目前没有设置开源许可证。公开仓库前按预期用途选择许可证，或保留未授权状态；不要代替公司发布无权公开的业务代码或补丁。本项目不包含业务 JAR。

## 建议发布步骤

先核对待提交内容：

```bash
git status --short
git diff --check
git check-ignore config.json runtime/example PRIVATE_DEPLOYMENT.md preview.png
```

确认后再暂存项目源码与文档，并查看暂存清单。GitHub 仓库名称可用 `datafly-patch-tool`，描述可用：

> 影像后端 JAR 补丁更新工具，把重复的上传、备份、替换和重启操作整合到网页，支持执行日志与备份回退。

创建仓库及添加远程地址、推送应使用你自己的目标仓库。不要把示例 URL 当成真实远程地址。后续使用 GitHub CLI 创建 PR 时，另行提供明确目标仓库和分支。

## 推荐阅读顺序

README → DEPLOY → docs/OPERATIONS → docs/PROCESS。

GitHub 用的相对 Markdown 链接指向仓库文件。私有现场记录不需要放进公开 README。
