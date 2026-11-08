"""Reconcile the completed mlxtend handoff metadata against files, without recollection."""
import hashlib
import json
import re
import zipfile
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from pipeline import TYPES, now, write
from quality import validate


def refresh(root):
    root=Path(root).resolve()
    def load(name): return json.loads((root/name).read_text(encoding='utf-8'))
    def rows(name):
        return [json.loads(s) for s in (root/name).read_text(encoding='utf-8').splitlines() if s.strip()]
    def local(value):
        return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
    manifest=load('manifest.json');job=load('job.json');quality=load('reports/quality.json');git=load('reports/git_collection.json')
    node_rows=[n for name in TYPES.values() for n in rows('processed/nodes/'+name+'.jsonl')]
    nodes={n['id']:n for n in node_rows};edge_rows=rows('processed/relations.jsonl');edges={e['relation_id']:e for e in edge_rows}
    evidence_rows=rows('processed/evidence.jsonl');evidence={e['evidence_id']:e for e in evidence_rows}
    accepted,accepted_edges,rejected=validate(nodes,edges,evidence)
    if rejected: raise ValueError('发现结构校验错误，停止更新说明：'+str(rejected[0]['reasons']))
    if len(nodes)!=len(node_rows) or len(edges)!=len(edge_rows):raise ValueError('发现重复ID，停止更新说明')
    hashes={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/'processed').rglob('*') if p.is_file()}
    mismatches=[key for key,value in manifest['checksums'].items() if hashes.get(key)!=value]
    if mismatches:raise ValueError('原manifest校验值不匹配：'+str(mismatches))
    missing_refs=[e['raw_ref'] for e in evidence_rows if not (root/e['raw_ref']).is_file()]
    if missing_refs:raise ValueError('存在缺失证据文件：'+str(missing_refs[:3]))
    nc={kind:sum(n['entity_type']==kind for n in node_rows) for kind in TYPES}
    rc=dict(Counter(e['relation'] for e in edge_rows))
    commits=[n for n in node_rows if n['entity_type']=='Commit'];prs=[n for n in node_rows if n['entity_type']=='PullRequest']
    git_commits=rows('raw/git/commits.jsonl');git_shas={c['sha'] for c in git_commits};commit_shas={n['properties']['sha'] for n in commits}
    missing={c['sha'] for c in git['known_commit_checks'] if not c['found']}
    diagnosis=load('reports/error_diagnosis.json')
    changes=rows('processed/file_changes.jsonl');api_changes=rows('processed/file_changes_api.jsonl')
    refs=load('processed/git_refs.json');documents=rows('processed/documents.jsonl');chunks=rows('processed/chunks.jsonl');versions=rows('processed/code_versions.jsonl')
    archive=root/'reports/metadata_before_update.zip'
    if not archive.exists():
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
            for name in ['manifest.json','job.json','reports/quality.json']:
                z.write(root/name,name)
            for p in root.rglob('*.md'): z.write(p,p.relative_to(root))
    updated=now()
    reconciliation={
        'node_total':len(nodes),'relation_total':len(edges),'node_counts':nc,'relation_counts':rc,
        'git_commits_parsed':len(git_shas),'git_commits_already_in_api_nodes':len(git_shas&commit_shas),
        'git_only_commit_nodes':len(git_shas-commit_shas),'api_only_commit_nodes':len(commit_shas-git_shas),
        'known_shas_checked':len(git['known_commit_checks']),'known_shas_missing_locally':len(missing),
        'missing_locally_but_commit_node_preserved':len(missing&commit_shas),
        'missing_locally_without_commit_node':len(missing-commit_shas),
        'merged_prs_with_locally_missing_merge_sha':sum(p['properties'].get('merged') and p['properties'].get('merge_commit_sha') in missing for p in prs),
        'unmerged_prs_with_locally_missing_merge_sha':sum(not p['properties'].get('merged') and p['properties'].get('merge_commit_sha') in missing for p in prs),
        'merged_prs_missing_merge_sha':[p['properties']['number'] for p in prs if p['properties'].get('merged') and not p['properties'].get('merge_commit_sha')],
        'original_api_error_sha_checks':[{'pr':d['pr']['pr'],'sha':d['sha'],'found_in_git':d['sha'] in git_shas} for d in diagnosis],
        'file_changes_total':len(changes),'file_changes_git':sum(c.get('method')=='git' for c in changes),
        'file_changes_api_fallback':sum(c.get('method')!='git' for c in changes),
        'original_api_file_changes':len(api_changes),'available_patches_in_merged_file_changes':sum(c.get('patch_available',False) for c in changes),
        'branch_refs':sum(r['name'].startswith('refs/heads/') for r in refs),'tag_refs':sum(r['name'].startswith('refs/tags/') for r in refs),
        'documents':len(documents),'document_kinds':dict(Counter(d['kind'] for d in documents)),
        'chunks':len(chunks),'code_version_definitions':len(versions),'evidence':len(evidence),
        'git_diff_files':sum(1 for _ in (root/'raw/git/diffs').glob('*.json')),
        'dangling_edges':0,'rejected_records':0,'missing_evidence_files':len(missing_refs),
        'processed_checksums_verified':len(hashes),'processed_files_modified':False,
        'commit_time_min':min(c['properties']['committed_at'] for c in commits),
        'commit_time_max':max(c['properties']['committed_at'] for c in commits),
    }
    audit={'updated_at':updated,'run_id':root.name,'basis':'当前文件实读；未重采API或Git，未修改processed/raw',**reconciliation}
    write(root/'reports/handoff_audit.json',audit)
    api_errors=quality.get('errors',[])
    partial_reasons=['原API阶段11条合并提交HTTP 422仍未补回',
                     '本地Git缺少1408个已知SHA：1301个已有API提交实体继续保留，另107个没有提交实体；不能把该数当作新增HTTP错误',
                     'PR #21缺少merge_commit_sha，不能建立MERGED_AS',
                     '语义抽取、模块功能配置、历史函数映射、Release成员计算尚未完成']
    manifest.update(run_id=root.name,state='partial',metadata_updated_at=updated,
                    collection_scope='API可访问的Issue/PR/评论/Release及提交；原reference_sha源码快照；本地Git冻结分支/tag可达历史及本地已有API已知提交祖先；两种来源合并去重',
                    collection_methods=['github_rest_api','local_git','python_ast'],checksums=hashes,
                    result_summary=reconciliation,partial_reasons=partial_reasons,
                    timestamp_semantics={'created_at':'原API处理结果生成时间，保留原值','git_collected_at':'Git补采包生成时间','metadata_updated_at':'本次元数据与说明更新，不是重新采集时间'})
    manifest['stages'].update(local_git='partial',git_fetch='completed',git_history_parsing='completed',git_coverage='partial')
    manifest['git_scope']={'source':'reports/git_collection.json','offline':git['offline'],'parsed_commits':git['commits'],
                           'diff_policy':git['diff_policy'],'known_shas_checked':len(git['known_commit_checks']),
                           'missing_known_shas':git['missing_known_commits'],'errors':len(git['errors']),
                           'cache_bundled':False,'scope':git['scope']}
    manifest['limitations']=[
        'API与Git均为本次可访问范围；全量模式表示不设人工条数上限，不保证所有历史对象可得',
        '保留原API的11条422及本地未找到的1408个已知SHA；未把缺失提交伪造为实体',
        '数据包不内置共享Git缓存；完整历史源码或补丁查询需要manifest.git_cache对应缓存，且仅覆盖已有对象',
        'Git文件变化普通/合并提交均比较第一父提交，根提交比较空树；重命名为相似度检测结果',
        '不包含Discussion、Actions、Wiki、Release附件、LFS外部对象及子模块内容',
        '代码符号仅解析原reference_sha，未进行历史函数映射',
        '未运行语义抽取、Module/Feature配置及Release成员计算，不自动生成RESOLVES或FIRST_RELEASED_IN',
        '校验覆盖基础字段、类型、日期、已实现关系端点及证据引用，非完整JSON Schema']
    write(root/'manifest.json',manifest)
    job.update(run_id=root.name,parent_run=manifest['parent_run'],canonical_repo=manifest['canonical_repo'],
               schema_version=manifest['schema_version'],contract_version=manifest['contract_version'],
               operation='local_git_supplement',state='partial',metadata_updated_at=updated,
               api_recollected=False,created_at_semantics='原API任务启动时间；保留该值以兼容原任务恢复',
               git_supplement={'completed_at':manifest['git_collected_at'],'cache_path':git['cache_path'],
                               'offline':git['offline'],'elapsed_seconds':git['elapsed_seconds'],
                               'download_completed':True,'history_parsing_completed':True,'coverage':'partial',
                               'report':'reports/git_collection.json'},
               resume_note='仅补采Git请使用git_collect.py --source当前目录或界面补采按钮；恢复已有任务按钮运行API流程，不是Git专用恢复')
    write(root/'job.json',job)
    quality.update(state='partial',node_counts=nc,relation_counts=rc,documents=len(documents),chunks=len(chunks),
                   reconciliation=reconciliation,partial_reasons=partial_reasons,metadata_updated_at=updated,
                   error_count_semantics={'errors':'原API请求失败记录，当前11条；保留审计历史','git_errors':len(git['errors']),
                                          'missing_known_commits':'本地对象缺口，1408个已知SHA，与请求错误是不同统计'})
    write(root/'reports/quality.json',quality)
    node_table='\n'.join(f'| [{name}.jsonl](processed/nodes/{name}.jsonl) | {kind} | {nc[kind]:,} |' for kind,name in TYPES.items())
    relation_table='\n'.join(f'| {kind} | {count:,} |' for kind,count in rc.items())
    diagnosis_table='\n'.join(f"| [#{d['pr']['pr']}]({d['pr']['url']}) | `{d['sha']}` | {d['status']} | {'找到' if d['sha'] in git_shas else '未找到'} |" for d in sorted(diagnosis,key=lambda d:d['pr']['pr']))
    summary=f'''| 指标 | 当前结果 |
|---|---:|
| 实体总数 | {len(nodes):,} |
| 关系总数 | {len(edges):,} |
| Git解析提交 / 合并后的Commit实体 | {len(git_shas):,} / {nc['Commit']:,} |
| 新增PARENT_OF / RENAMED_TO | {rc.get('PARENT_OF',0):,} / {rc.get('RENAMED_TO',0):,} |
| 合并后的文件变化记录 | {len(changes):,} |
| 其中Git记录 / API回退记录 | {reconciliation['file_changes_git']:,} / {reconciliation['file_changes_api_fallback']:,} |
| 文档 / 文本块 | {len(documents):,} / {len(chunks):,} |
| 代码逻辑符号 / 定义位置记录 | {nc['CodeSymbol']:,} / {len(versions):,} |
| 原API错误 / Git处理错误 | {len(api_errors)} / {len(git['errors'])} |
| 本地未找到的已知SHA | {len(missing):,} |
| 隔离记录 / 悬空边 | 0 / 0 |
| 最终状态 | partial |
'''
    gaps='''### 为什么仍是 partial

下载与差异解析均已完成，Git处理错误为0。partial主要表示覆盖缺口与后续阶段尚未完成，不表示Git下载失败。

本次检查3,449个已知SHA，2,041个在本地找到，1,408个未找到。其中1,301个已有API提交实体，仍保留在合并后的3,342个Commit中；其余107个没有Commit实体。不能把1,408解释为新增1,408个请求错误或丢失1,408条已采数据。

按PR观察，有17个已合并PR的合并SHA在本地缺失，另96个未合并PR的非空合并SHA在本地缺失。这里统计的是PR，可能与提交集合交叉，不能与上述SHA分组直接相加。未合并PR的合并SHA不应视为已落地的正式合并结果。已有API的MERGED_AS关系仍保留，当前为531条；没有证据时不补造关系。

原来的11个HTTP 422合并SHA在本地也全部未找到，PR #21仍为merge_commit_sha=null。这不证明历史被重写或对象永久丢失，不能仅凭本次结果确认历史原因。
'''
    readme=f'''# mlxtend API + 本地Git数据包阅读与交接说明

更新日期：2026-11-02。当前任务：`{root.name}`；原API任务：`{manifest['parent_run']}`。

本目录是知识图谱输入数据包，包含API原始缓存、Git历史结构记录、处理后的节点关系及证据；不是图数据库，也不内置完整Git缓存。

## 当前结果

{summary}
Git补采用时{git['elapsed_seconds']}秒（约2分30.5秒），结果生成于北京时间{local(manifest['git_collected_at'])}。原API阶段仍为14,530秒，未重新采集。代码符号仍绑定`{manifest['reference_sha']}`，并未因补采变成全历史函数表。合同版本desktop-5指数据交接格式，不是mlxtend版本；继承记录可保留旧合同版本的来源标记。

{gaps}
## 建议阅读顺序

1. [manifest.json](manifest.json)：范围、阶段、限制、计数和processed校验值。
2. [reports/quality.json](reports/quality.json)、[reports/git_collection.json](reports/git_collection.json)：总体质量与Git详细检查。
3. [reports/handoff_audit.json](reports/handoff_audit.json)：按当前文件重新核对的数量和API/Git覆盖差异。
4. [mlxtend本次数据采集清洗处理报告.md](mlxtend本次数据采集清洗处理报告.md)：完整处理报告。
5. 节点、关系、证据及下表列出的辅助文件。

## 目录和文件用途

| 路径 | 记录内容及注意事项 |
|---|---|
| `job.json` | 原API启动参数及时间、新任务ID、Git补采参数和耗时；不含Token；null数量代表不设人工上限 |
| `manifest.json` | 当前包的身份、来源、处理状态、Git覆盖和文件SHA256 |
| `raw/api/*.json` | 请求URL、fetched_at、status、分页link和原始data；API证据与重处理缓存 |
| `raw/source/*.py` | 按blob SHA命名的冻结版本Python源码，用code_versions查原路径 |
| `raw/git/refs.json` | Git补采时冻结的引用：分支、标签、对象与提交SHA |
| `raw/git/commits.jsonl` | {len(git_shas):,}个本地提交，含父SHA、作者与提交者姓名邮箱、时间、说明 |
| `raw/git/diffs/<SHA>.json` | {reconciliation['git_diff_files']:,}份逐提交差异元数据：基准、路径、状态、blob、增删行数；不含完整补丁 |
| `processed/nodes/*.jsonl` | 下表13类图实体，按稳定ID合并 |
| `processed/relations.jsonl` | {len(edges):,}条图关系及证据、方法和业务属性 |
| `processed/documents.jsonl` | {len(documents):,}份讨论、Release与文档原文及清洗文本 |
| `processed/chunks.jsonl` | {len(chunks):,}个1800 Unicode字符固定块，无重叠；偏移指向raw_text的半开区间 |
| `processed/evidence.jsonl` | {len(evidence):,}条证据；API用raw_ref与json_pointer，Git提交用JSONL记录中的SHA定位，差异指向单份JSON |
| `processed/code_versions.jsonl` | {len(versions):,}个定义位置，含symbol_id、revision_sha、路径、源码引用及行号；对应{nc['CodeSymbol']:,}个逻辑符号 |
| `processed/file_changes.jsonl` | {len(changes):,}条合并文件变化：Git成功解析提交优先，API补足其余提交；Git记录带状态、旧新路径、增删行和证据 |
| `processed/file_changes_api.jsonl` | 原API的{len(api_changes):,}条文件变化备份，保留可得patch；不能与合并文件直接拼接计数 |
| `processed/refs.json` | 原API的分支/tag观察记录 |
| `processed/git_refs.json` | Git冻结引用清单：20分支、35标签；标签不是Release，也未计算Release成员 |
| `processed/source_tree.json` | 原reference_sha源码树；不是全部历史源码树 |
| `semantic/inputs.jsonl` | 每文档一条待语义抽取任务；模型未执行 |
| `semantic/candidates.jsonl` | 预留语义候选，本次为空 |
| `review_queue/items.jsonl` | 结构校验隔离记录，本次为空；不表示已人工审核 |
| `reports/quality.json` | 综合计数、原API错误、原分页及性能、Git报告和本次核对结果 |
| `reports/git_collection.json` | Git原始补采报告：耗时、errors、3,449个SHA逐项检查和范围 |
| `reports/handoff_audit.json` | 本次从文件实读的交接核对报告 |
| `reports/error_diagnosis.json` | 原11个错误的历史API复查证据；未在本次重新发起请求 |
| `reports/错误原因分析.md` | 原11个错误加上本次Git补查结论 |
| `reports/metadata_before_update.zip` | 本次更新前的manifest、job、quality和Markdown快照，仅供审计，旧统计不作当前结果 |
| `state/progress.json` | Git补采结束时的状态，不是图数据库构建进度 |

## 节点清单

| 文件 | 类型 | 当前条数 |
|---|---|---:|
{node_table}

File包含历史路径，不等于当前版本文件数。Issue不等于Problem，PR不等于Solution。Module/Feature未配置、Problem/Solution未抽取，所以对应文件为空。

## 关系清单

| 类型 | 当前条数 |
|---|---:|
{relation_table}

PARENT_OF方向为父提交到子提交；RENAMED_TO为旧路径到新路径，发生版本在properties.commit_sha中，采用Git相似度检测。MODIFIES的本地比较基准为第一父提交，根提交比较空树。REFERENCES不代表RESOLVES，HAS_RELEASE不代表Commit属于该Release。

节点保持`id/entity_type/properties/provenance`封装；关系用source_id、target_id连接节点。导入Neo4j时应展开所需属性，不能把任意嵌套字典原样作为属性；保留稳定ID和证据索引。

## 下游使用与审核

建图优先读取nodes和relations；核实事实时沿evidence_ids回到evidence.raw_ref。Git提交证据文件是JSONL，应按SHA选取对应行，不能当作一个JSON对象读取。

文本清洗仅统一换行、去空字符及首尾空白，保留原文；未执行语义去噪、embedding或LLM。符号只属于冻结版本，不能直接把历史Commit关联到当前函数并宣称其修改了该函数。

Git差异记录patch_available=false表示未导出完整补丁，不是没有文件修改。合并变化文件中仍有{reconciliation['available_patches_in_merged_file_changes']:,}条API回退记录带patch；原API备份另行保留。

共享Git缓存位于`{git['cache_path']}`，未打入数据包。离线查询已有历史源码和完整补丁时需另交付该缓存；1408个本地缺失SHA仍不能靠这个缓存读取。Git作者邮箱不自动当作GitHub账号。

所有processed文件已核对原manifest的SHA256并通过当前结构校验，本次未修改raw或processed。零隔离、零悬空仅说明当前结构规则通过，不代表语义正确性或全历史完整性已证实。
'''
    (root/'README.md').write_text(readme,encoding='utf-8')
    report=f'''# mlxtend 数据采集、清洗与处理报告（Git补采后）

报告更新日期：2026-11-02。任务：`{root.name}`。

## 任务与时间

原API任务`{manifest['parent_run']}`于北京时间{local(job['created_at'])}启动，耗时14,530秒；原结果生成时间为{local(manifest['created_at'])}。本次没有重复API采集，复用原记录并补充本地Git。Git补采于{local(manifest['git_collected_at'])}生成结果，程序记录耗时{git['elapsed_seconds']}秒。Git报告没有单独保存开始时间，不将倒推时间冒充实测开始时间。

业务Schema为1.0，交接合同desktop-5，状态partial。当前记录是本批观察，不是仓库未来实时统计。

## 结果汇总

{summary}
与原API包相比：实体仍为15,409个；关系由63,336增加3,324至66,660，新增来自PARENT_OF和RENAMED_TO。Git解析的2,041个Commit均已存在于API实体中，因此不会重复新增2,041个节点。

## 数据来源与处理流程

1. 输入URL后，API分页获取仓库、Issue、PR、普通评论、Review、行内评论、Release、开发者与提交。过滤Issue端点内PR，按SHA复用提交，按请求URL缓存响应，保留分页证据。
2. 冻结默认分支`{manifest['reference_sha']}`，归档文件树、blob和Python源码；AST解析类、函数、方法并保存行号。
3. 文本同时保留原文与清洗文本，统一换行、移除空字符、去首尾空白；按1800 Unicode字符固定分块，不自动翻译、语义去重或运行目标代码。
4. 本地裸Git缓存fetch分支和标签，不检出工作区、不运行子模块或下载LFS外部对象。冻结20个分支和35个标签，检查API已知SHA是否存在于本地。
5. 解析本地可用提交及祖先，生成父子关系；逐提交比较第一父版本，根提交比较空树，使用相似度检测重命名，记录文件状态、blob和增删行数。二进制文件增删行可为null。
6. 按节点ID、完整SHA和关系ID合并来源。保留API账号作者关系；Git姓名邮箱只作为Commit属性，不推断账号。旧PR合并SHA从归档的API证据补入属性。
7. Git成功解析的提交优先用于file_changes，其他提交保留API回退；原API file_changes另存备份。原Issue、评论、Release、文档和代码符号复用。
8. 校验类型、基础字段、日期、关系方向/端点和证据，写出节点、关系、报告和清单。没有自动导入Neo4j，也没有执行模型抽取。

## 实体与关系

实体计数见[README节点表](README.md#节点清单)；关系计数如下。

| 类型 | 条数 |
|---|---:|
{relation_table}

Commit时间范围为`{reconciliation['commit_time_min']}`至`{reconciliation['commit_time_max']}`，按已采committed_at观察。File是含历史路径的实体；代码符号仍是冻结版本，不是全历史符号。

## API与Git覆盖的差异

{gaps}
合并后的3,342个Commit中，2,041个有本次Git解析记录，1,301个仅保留API来源。Git缺失不等于API数据被删除。本次新增2,345条父子边，仅覆盖本地可解析历史；不能宣称全部3,342个Commit均已有父子结构。

## 文本、代码和差异输出

文档分类：{json.dumps(reconciliation['document_kinds'],ensure_ascii=False)}。共5,020份、5,575个块。CodeSymbol为1,337个，code_versions为1,340次定义；重复限定名被现有ID规则合并，定义位置另存。

file_changes共有42,438条，其中Git32,960条、API回退9,478条；原API备份42,438条。两份文件不能直接相加。Git差异提供元数据，不额外保存完整文本/二进制补丁；API备份保留原有patch。

API网络请求尝试7,950次、缓存复用675次，属于原API阶段；Git补采150.5秒单独记账。Git阶段HTTP交互没有纳入API请求计数，不能把两者混为同一种性能指标。

## 错误与限制

历史API错误11条、Git处理错误0条；零隔离、零悬空，当前结构验证通过。缺口不应伪造补齐，原11个失败SHA见[错误分析](reports/错误原因分析.md)。

尚未执行Module/Feature人工配置、Problem/Solution语义抽取、历史函数映射、Release成员计算或FIRST_RELEASED_IN推断。不能从PR正文引用直接生成RESOLVES，也不能按日期直接证明首次发布。

## 本次文档更新及核查

更新manifest、job、quality、README、本报告、Git补采说明和错误分析；新增handoff_audit.json。原始Git报告、API错误诊断、raw和processed不变。更新前说明与元数据保存在reports/metadata_before_update.zip。

本次核对{len(nodes):,}个唯一节点、{len(edges):,}条唯一关系、{len(evidence):,}条证据，全部raw_ref文件存在，{len(hashes)}个processed文件校验值与更新前一致。此核对不等同于逐条人工核实事实。

图谱同学可先导入确定性节点与关系；检索同学使用documents/chunks；函数历史或Release问题需先补实现对应阶段。完整文件说明见[README](README.md)。
'''
    (root/'mlxtend本次数据采集清洗处理报告.md').write_text(report,encoding='utf-8')
    git_note=f'''# 本地Git补采说明（实际结果）

任务：`{root.name}`。北京时间{local(manifest['git_collected_at'])}生成，耗时{git['elapsed_seconds']}秒。

{summary}
下载与解析已完成；local_git=partial表示已知SHA覆盖不足，并非仍在下载。20分支、35标签；解析2,041个Commit，导出2,041份差异JSON、32,960条文件变化。新增PARENT_OF 2,345条、RENAMED_TO 979条；没有新增独立Commit节点，也未补回原11个失败合并SHA。

{gaps}
## 文件及下游使用

- raw/git/refs.json：冻结引用；raw/git/commits.jsonl：提交与父SHA；raw/git/diffs：逐提交差异元数据。
- processed/git_refs.json：交接引用表；nodes/relations/evidence已合并。file_changes优先Git32,960条，API回退9,478条；file_changes_api另留备份。
- reports/git_collection.json保留原补采过程记录；reports/handoff_audit.json提供当前文件核对；quality和manifest说明整体覆盖。
- 原API数据包未修改，本次没有重爬讨论和评论。reference_sha代码符号不变，没有全历史函数映射。
- 共享缓存：`{git['cache_path']}`。数据包不包含缓存；完整历史源码、补丁需要该缓存及对应对象。当前输出未导出Git完整patch。

见[README](README.md)了解逐文件用途；见[处理报告](mlxtend本次数据采集清洗处理报告.md)了解完整流程。
'''
    (root/'本地Git补采说明.md').write_text(git_note,encoding='utf-8')
    error_note=f'''# 原11条API错误与本次Git补查结果

更新日期：2026-11-02。当前数据包`{root.name}`。

## 原API错误

11条错误均为历史合并结果SHA的Commit接口HTTP 422。此前逐项API复查返回No commit found for SHA，剩余额度充足；原证据保留在error_diagnosis.json。本次没有再次发送API复查请求。

## 本次Git是否补回

| PR | 合并SHA | 历史API复查HTTP | 本地Git |
|---|---|---:|---|
{diagnosis_table}

11个SHA全部未在本次Git缓存中找到，因此对应11条缺失MERGED_AS尚未补回。另PR #21的merge_commit_sha为空，没有进入原11条HTTP错误。当前543个已合并PR、531条MERGED_AS，相差12，仍然成立。

## 1408个本地缺失SHA是另一项统计

Git检查3,449个SHA，其中1,408个本地未找到：1,301个已有Commit实体由API保留，107个未对应实体。这个集合包括不同类型的PR相关SHA，不限于原11个HTTP失败；不能把1408与11相加当作总错误数。

有17个已合并PR的合并SHA本地不可用；其中原11个API也缺失，其余仍可能由API数据提供，不能把这17个PR一律认定没有MERGED_AS。未合并PR另外96个非空合并SHA本地不可用，不代表96次正式合并失败。

本地Git实际处理错误为0。缺口详情见git_collection.json的known_commit_checks，交叉统计见handoff_audit.json。

## 影响与后续

讨论、PR和API已取得的Commit仍可用于建图和检索；本地缺失对象无法由当前缓存提供历史源码、diff和完整父子结构。保留缺失标记，不按最后一个PR提交猜测合并结果，不改写merged状态，不补造Commit。

本次只能确认API或本地对象的可获取性，不能确认强推、迁移、历史重写或对象清理等原因。若需要继续补历史，可针对具体案例寻找其他可信Git副本或额外PR引用，但需要另行采集与验证，不应在本报告中宣称已经完成。
'''
    (root/'reports/错误原因分析.md').write_text(error_note,encoding='utf-8')
    assert all(hashlib.sha256((root/k).read_bytes()).hexdigest()==v for k,v in hashes.items())
    print(json.dumps({'updated':str(root),'nodes':len(nodes),'relations':len(edges),'checksums_verified':len(hashes),'backup':str(archive)},ensure_ascii=False))


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('directory');args=parser.parse_args()
    refresh(args.directory)
