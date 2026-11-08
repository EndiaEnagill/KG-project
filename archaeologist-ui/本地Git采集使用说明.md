# 本地 Git 采集使用说明

## 已有 mlxtend 数据怎样使用

1. 关闭旧版工作台，双击同目录的 `启动工具.cmd` 重新打开。
2. 点击“打开已有结果”，选择：

   `C:\Users\myshine\Desktop\github\archaeologist-ui\data\rasbt__mlxtend\20261101-185034-cc52d0`

3. 点击“为当前结果补采本地 Git”。不需要重新点击“开始采集与处理”。
4. 等待下载、解析和校验。文件变化解析阶段显示已完成提交数与总数；下载期间显示仍在运行的提示，不能提前计算准确百分比。
5. 完成后自动打开新的结果目录，名称为原任务名加 `-git-随机字符`。

原任务不会修改，不会重新请求 Issue、PR、评论或 Release。需要安装 Git for Windows，并在安装后重新打开工作台；PowerShell 中 `git --version` 应能显示版本。当前实现针对公开 GitHub 仓库，不把界面 Token 写入 Git 地址，也不通过弹出凭据窗口登录私有仓库。

## 新仓库采集

输入仓库链接，保持“API完成后补采本地Git历史”勾选，再开始采集。API阶段完成后自动执行Git补采。若Git阶段失败，API数据仍会保存，任务日志显示原因，之后可打开API结果单独补采。

## 数据保存位置

共享仓库缓存默认放在：

`C:\Users\myshine\Desktop\github\archaeologist-ui\cache\git\rasbt__mlxtend.git`

采用裸仓库，不检出工作目录，不运行目标仓库的Python代码，不更新子模块，不下载LFS外部对象。它保存Git对象和版本历史；同一仓库下次补采使用fetch更新已有缓存。每次输出的数据包独立保存。

| 新增或更新文件 | 含义 |
|---|---|
| `raw/git/refs.json` | 本次冻结的分支、标签、对象SHA及解析到的Commit SHA；标签不等同于GitHub Release |
| `raw/git/commits.jsonl` | 本次读取的提交原始结构记录，包括父提交、作者与提交者姓名邮箱、时间、说明 |
| `raw/git/diffs/<SHA>.json` | 每个提交的修改文件、状态、前后blob SHA、重命名路径及增删行数 |
| `processed/git_refs.json` | 可交给下游使用的引用清单 |
| `processed/nodes/commits.jsonl` | API与Git按SHA合并的提交实体 |
| `processed/nodes/files.jsonl` | 合并后的文件实体，新增历史中出现的路径 |
| `processed/relations.jsonl` | 合并后的图关系，包括新增的PARENT_OF、RENAMED_TO，以及补充的HAS_COMMIT、MODIFIES、MERGED_AS |
| `processed/evidence.jsonl` | Git记录、网页链接及原始文件的证据索引 |
| `processed/file_changes_api.jsonl` | 原API文件变化记录备份，保留已有patch |
| `processed/file_changes.jsonl` | 优先使用成功解析的Git差异，未覆盖提交保留API记录，避免直接拼接导致重复计数 |
| `reports/git_collection.json` | Git范围、数量、耗时、失败、已知SHA本地可用性检查结果 |
| `reports/quality.json` | 更新后的实体关系计数，保留历史API错误，附加Git报告 |
| `manifest.json` | 新任务标识、原任务标识、缓存位置、阶段状态及更新后的processed校验值 |
| `本地Git补采说明.md` | 当前数据包新增内容的简要说明 |

其余Issue、PR、评论、文档、分块、当前参考版本的代码符号等文件从原任务复制。Git补采不会把新分支最新版本的源码符号混入原来冻结的reference_sha快照。

## 关系与版本处理规则

- `父Commit → PARENT_OF → 子Commit`：读取Git提交对象中的父提交。
- `Commit → MODIFIES → File`：普通提交和合并提交均与第一父提交比较；根提交与空树比较。合并提交同时保留全部父子关系，但不会对每个父版本重复统计文件变化。
- `旧File → RENAMED_TO → 新File`：使用Git相似度检测结果，并记录发生的Commit。属于检测结果，不是永久不变的文件身份；路径再次复用时不能仅凭路径推断同一文件。
- `PR → MERGED_AS → Commit`：仅在保存的API合并SHA与本地可用提交准确匹配时补充，不按提交说明猜测。旧版本未保存该属性时，从已存API证据读取。
- Git作者姓名邮箱保存在Commit属性中，不直接按邮箱推断GitHub Developer账号，不新增未经确认的账号归属关系。
- 冻结当前分支和标签可达历史；本地已存在的API已知提交也会加入分析。不会自动逐个请求远端不可达历史SHA。

## 重点查看的结果

首先查看 `reports/git_collection.json` 的 `errors`、`missing_known_commits`、`known_commit_checks`、`commits`、`file_changes`。

之前的11次API错误仍然保留，以便审计。只有本地对象确实存在，才可能补回对应Commit与关联。报告中的 `found=false` 表示这次本地缓存中没有该SHA，不证明该提交永久无法找回，也不能单凭这个结果判断历史被改写。

Git阶段成功不等于整个知识图谱全部完成；原任务是partial时不会直接改为ready。历史函数映射、语义抽取、Release成员关系和首次发布版本推断仍未执行。

本版保存文件差异元数据，不额外导出每个提交的完整文本/二进制补丁；完整历史内容可从Git缓存读取。交接ZIP包含结构化证据，不包含共享Git缓存；若下游还需要离线查询所有历史代码，需另行交付该缓存。

## PowerShell运行方式

```powershell
Set-Location "C:\Users\myshine\Desktop\github\archaeologist-ui"
& "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" .\git_collect.py --source ".\data\rasbt__mlxtend\20261101-185034-cc52d0"
```

如果已经安装普通Python，也可使用 `python .\git_collect.py --source "任务目录"`。

缓存已经存在且希望断网处理时，在命令末尾增加 `--offline`。可使用 `--cache-root "另一个目录"` 指定缓存根目录。重复补采会重新生成交接包；不可变Git对象会复用，但当前版本仍重新解析提交差异。

任务可以取消，原API包不受影响。下载中取消后可从原结果重新补采。正常失败或取消会释放缓存锁；若进程被强制结束，只有确认该仓库没有补采进程时，才手动移除报告所指的 `.lock` 文件。

## 实现参考

提交格式、根提交差异、NUL分隔文件名和重命名检测采用Git官方命令说明：

- [git-log](https://git-scm.com/docs/git-log)
- [git-diff-tree](https://git-scm.com/docs/git-diff-tree)
