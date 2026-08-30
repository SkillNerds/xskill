// Lightweight i18n adapter for the legacy dashboard SPA.
//
// The Chinese UI copy remains the source locale in index.html/app.js.  This
// module translates known interface strings at the DOM boundary, including
// fragments rendered later by app.js.  Data returned by the API is deliberately
// left untouched unless it matches a known UI template.
(function (global) {
  'use strict';

  const STORAGE_KEY = 'xskill.dashboard.language';
  const SUPPORTED = new Set(['en', 'zh']);
  const ATTRIBUTES = ['title', 'placeholder', 'aria-label'];
  const SKIP = new Set(['SCRIPT', 'STYLE', 'PRE', 'CODE', 'TEXTAREA']);

  const EN = {
    'xskill 控制台': 'xskill Console',
    '控制台': 'Console',
    '总览': 'Overview',
    '技能库': 'Skills',
    '流水线': 'Pipeline',
    '轨迹 & 原子': 'Trajectories & Atoms',
    '用户 & 画像': 'Users & Profiles',
    '灰度 Canary': 'Canary',
    '我的': 'My Dashboard',
    '管理': 'Admin',
    '设置': 'Settings',
    '登录': 'Sign in',
    '退出': 'Sign out',
    'connect --name 后终端打印登录 token': 'After connect --name, the terminal prints a sign-in token',
    '指标口径见 docs/dashboard-metrics.md（卡片 ⓘ 同源）': 'Metric definitions: docs/dashboard-metrics.md (same source as card ⓘ tips)',
    '通知': 'Notifications',
    '加载中…': 'Loading…',
    '暂无数据': 'No data',
    '暂无团队用户（非 team server 或尚无 client 连接）': 'No team users (not a team server or no clients connected yet)',
    '暂无团队动态': 'No team activity yet',
    '暂无推荐记录': 'No recommendation history',
    '暂无轨迹': 'No trajectories',
    '暂无原子': 'No atoms',
    '暂无关联 skill': 'No related skills',
    '无': 'None',
    '轨迹': 'Trajectories',
    '原子': 'Atoms',
    '单轨迹均': 'Average per trajectory',
    '平均 ux': 'Average UX',
    '处理成功率': 'Processing success rate',
    '重试率': 'Retry rate',
    'registry 中已入库的轨迹行数。': 'Number of trajectory rows stored in the registry.',
    '所有轨迹拆出的 AtomTask 总数。': 'Total AtomTasks split from all trajectories.',
    '已成功拆分轨迹的原子数之和 ÷ 已成功拆分轨迹数。': 'Atoms from successfully split trajectories ÷ successfully split trajectories.',
    '所有轨迹的平均用户体验分（1–10）。': 'Average user-experience score across all trajectories (1–10).',
    '处理到 done 的轨迹占比。终态口径：done ÷ (done+error+filtered)，在途轨迹不进分母。': 'Share of trajectories reaching done. Terminal-state definition: done ÷ (done+error+filtered); in-flight trajectories are excluded.',
    '处理中发生过重试的轨迹占比。': 'Share of trajectories retried during processing.',
    '还没有使用打分': 'No usage ratings yet',
    '蒸馏管线': 'Distillation pipeline',
    '轨迹状态实时计数': 'Live trajectory-state counts',
    '待拆分': 'Pending split',
    '拆分中': 'Splitting',
    '聚类分派中': 'Assigning clusters',
    '已完成': 'Completed',
    '错误': 'Errors',
    '冷启动屏障激活中': 'Cold-start barrier active',
    '收集满后统一蒸馏，避免碎片化 skill': 'Distill after the collection fills to avoid fragmented skills',
    '关键比率': 'Key rates',
    '推荐触发率': 'Recommendation trigger rate',
    '原子采纳率': 'Atom adoption rate',
    'canary 晋升率': 'Canary promotion rate',
    '被推荐给用户的 skill 中、随后被实际采用的占比。': 'Share of recommended skills that users subsequently adopted.',
    '拆出的原子中被聚合进某个 skill 的比例。': 'Share of extracted atoms aggregated into a skill.',
    '灰度候选裁决中晋升为正式版的比例 = 晋升 ÷ 已裁决（晋升+拒绝+超时丢弃）。': 'Share of canary decisions promoted to production = promoted ÷ decided (promoted + rejected + timed out).',
    '还没有灰度裁决': 'No canary decisions yet',
    '生态分布': 'Ecosystem distribution',
    '按轨迹来源': 'By trajectory source',
    '用户模型分布': 'User model distribution',
    '成本 & 用量': 'Cost & usage',
    '流水线 LLM/embedding 调用': 'Pipeline LLM/embedding calls',
    '今日': 'Today',
    '累计': 'Total',
    '模型': 'Model',
    '步骤': 'Step',
    '成本': 'Cost',
    '还没有调用记录': 'No calls yet',
    '上游格式变更': 'Upstream format changed',
    '上游地址失效': 'Upstream location unavailable',
    '上游不可达': 'Upstream unreachable',
    '刷新异常': 'Refresh failed',
    '从未': 'never',
    '第三方 · skillhub': 'Third-party · skillhub',
    '自产': 'Native',
    '技能': 'Skill',
    '状态': 'Status',
    '描述': 'Description',
    '版本': 'Version',
    '候选': 'Candidates',
    '检索技能名 / 描述': 'Search skill name / description',
    'baby=cluster 刚建的草稿；main=已正式产出；staging=灰度候选并存。': 'baby = newly clustered draft; main = released; staging = coexisting canary candidate.',
    '.candidates.yml 里待攒分的 atom 数。': 'Atoms still accumulating scores in .candidates.yml.',
    '无匹配技能': 'No matching skills',
    '技能库还是空的': 'The skill library is empty',
    '上一页': 'Previous page',
    '下一页': 'Next page',
    '单 skill 触发率': 'Per-skill trigger rate',
    '去重 (client, skill) 推荐对 → 之后被采用': 'Unique (client, skill) recommendations → later adopted',
    '曝光对': 'Recommended pairs',
    '采用对': 'Adopted pairs',
    '触发率': 'Trigger rate',
    '还没有推荐曝光记录': 'No recommendation impressions yet',
    '展开': 'Expand',
    '收起': 'Collapse',
    '▶ 展开': '▶ Expand',
    '▼ 收起': '▼ Collapse',
    '点选进行中的任务看实时日志': 'Select a running task to view its live log',
    '心跳': 'Heartbeat',
    '模型请求': 'Model requests',
    '未归类原子': 'Unclassified atoms',
    '异常': 'Issues',
    '席位': 'Slots',
    '配额比': 'Quota weight',
    '批量': 'Batch',
    '进行中': 'Running',
    '排队': 'Queued',
    '排队中': 'Queued',
    '完成': 'Completed',
    '失败': 'Failed',
    '空闲': 'Idle',
    '已跑': 'Running for',
    '回流受阻': 'Reverse sync blocked',
    'atom 批': 'Atom batches',
    'A→B 转移': 'A→B transfer',
    '与 SkillEdit 共用席位': 'Shares slots with SkillEdit',
    '与 SkillEdit 共用座位': 'Shares slots with SkillEdit',
    '大模型优先': 'Large-model priority',
    '有 generate 在等时，大模型名额先给它': 'Prioritize large-model capacity when Generate is waiting',
    '点击修改席位': 'Click to edit slots',
    '点击修改配额比': 'Click to edit quota weight',
    '保存': 'Save',
    '取消': 'Cancel',
    '数值': 'Value',
    '增加': 'Increase',
    '减少': 'Decrease',
    '打开轨迹': 'Open trajectory',
    '输入 traj_id（文件名去 .md 后缀）——暂无全轨迹列表端点，也可从技能血缘的贡献原子跳转进入': 'Enter traj_id (filename without .md). There is no full trajectory-list endpoint yet; you can also open one from a contributing atom in skill lineage.',
    '打开': 'Open',
    '生态目录': 'Ecosystem directories',
    '已注册的轨迹来源目录': 'Registered trajectory source directories',
    '生态': 'Ecosystem',
    '已索引': 'Indexed',
    '路径': 'Path',
    '用户连接状态': 'User connection status',
    '用户': 'User',
    '上次活跃': 'Last active',
    '轨迹 · 原子': 'Trajectories · Atoms',
    '主力模型': 'Primary model',
    'client 上报的 xskill 版本;低于 server 当前版本标‘落后’;空=旧 client 未上报。': 'xskill version reported by the client; versions below the server are marked “behind”; blank means an older client did not report it.',
    '在线': 'Online',
    '离线': 'Offline',
    '落后': 'Behind',
    '未上报': 'Not reported',
    '标签云 / 关键词': 'Tag cloud / keywords',
    '来自原子 tags 聚合，字号 ∝ 出现次数 · 悬浮/点击用户行高亮其标签': 'Aggregated from atom tags; size ∝ frequency · hover/click a user row to highlight their tags',
    '灰度分桶分布': 'Canary bucket distribution',
    '使用打分记录按 side 聚合，与灰度裁决同源': 'Usage ratings grouped by side, using the same source as canary decisions',
    '分桶': 'Bucket',
    '使用': 'Usage',
    '还没有灰度使用记录': 'No canary usage records yet',
    '请先登录（左下角）。': 'Please sign in first (bottom left).',
    '推给我': 'Pushed to me',
    'client 截取服务器推送队列前 N 个并安装到 harness': 'The client takes the first N items from the server push queue and installs them into the harness',
    '点击直接编辑': 'Click to edit',
    '安装个数': 'Number to install',
    '增加推送个数': 'Increase pushed count',
    '减少推送个数': 'Decrease pushed count',
    '个 SKILL': 'SKILLS',
    '检索技能': 'Search skills',
    '已屏蔽': 'Blocked',
    '我的贡献去向': 'Where my contributions went',
    '关系图': 'Relationship graph',
    '我上传的 skill': 'Skills I uploaded',
    '点 skill 进详情；右侧可展开用户评分原子': 'Open a skill for details; expand user rating atoms on the right',
    '点左侧旁的「使用」查看谁在用、评分原子': 'Select “Usage” on the left to see users and rating atoms',
    '我贡献的 skill commit': 'Skill commits I contributed',
    '本地改 skill → 线上提交；状态：被吸收 / 灰测中 / 已上线': 'Local skill edit → online commit; states: absorbed / in canary / released',
    '世界消息': 'Team activity',
    '历史推荐': 'Recommendation history',
    '推荐次数': 'Recommendations',
    '实际触发': 'Actual triggers',
    '命中率': 'Hit rate',
    '结论': 'Result',
    '仅 admin。请先以 admin 登录（左下角）。': 'Admin only. Please sign in as admin (bottom left).',
    '用户 × 推送/配置': 'Users × push/configuration',
    '全局 pin:': 'Global pins:',
    'skill 名': 'Skill name',
    '+ 全局 pin': '+ Global pin',
    '当前推送': 'Current push',
    '灰度': 'Canary',
    '轨迹处理': 'Trajectory processing',
    '停推建议': 'Stop-push suggestion',
    '操作': 'Actions',
    '画像聚类': 'Profile clusters',
    '节点=用户(大小∝原子) · 边=兴趣相似度>0.6(粗细∝相似度) · 无边灰点=冷启动 · 悬停边看共同标签/skill': 'Nodes = users (size ∝ atoms) · edges = interest similarity > 0.6 (width ∝ similarity) · isolated gray nodes = cold start · hover edges for shared tags/skills',
    '技能管理': 'Skill management',
    '两段式:先下线观察,再删除;删除需输入 skill 名确认': 'Two steps: retire and observe, then delete; deletion requires typing the skill name',
    '近 30 日使用': '30-day usage',
    'dashboard/canary/recommend/skillhub 段热生效;': 'dashboard/canary/recommend/skillhub sections reload live;',
    'llm/watch_dirs 改动需重启 serve': 'llm/watch_dirs changes require restarting serve',
    '仅校验': 'Validate only',
    '校验并热加载': 'Validate & hot reload',
    '登录 xskill 控制台': 'Sign in to xskill Console',
    '用户名（connect --name）': 'Username (connect --name)',
    'dashboard token / admin 口令': 'Dashboard token / admin password',
    '晋升': 'Promoted',
    '回滚': 'Rolled back',
    '观察中（点黄点看推送对象）': 'Under observation (select the yellow node for recipients)',
    '普通提交': 'Regular commit',
    '进化路径': 'Evolution path',
    '得分趋势': 'Score trend',
    '贡献来源': 'Contribution sources',
    '贡献原子': 'Contributing atoms',
    '版本统计': 'Version statistics',
    '按用户': 'By user',
    '文件目录': 'Files',
    '预览 / diff': 'Preview / diff',
    '还没有提交历史': 'No commit history yet',
    '还没有贡献原子': 'No contributing atoms yet',
    '无 diff': 'No diff',
    '空目录': 'Empty directory',
    '非 git 仓': 'Not a Git repository',
    '加载离线触发评测…': 'Loading offline trigger evaluation…',
    '当前推送对象': 'Current recipients',
    '检索用户': 'Search users',
    '灰度版推送给': 'Canary version pushed to',
    '主干版推送给': 'Main version pushed to',
    '当前': 'Current',
    '样本': 'Samples',
    '首次': 'First',
    '末次': 'Last',
    '离线探针触发率': 'Offline probe trigger rate',
    '是': 'Yes',
    '否': 'No',
    '触发': 'Triggered',
    '未触发': 'Not triggered',
    '通过': 'Passed',
    '未过': 'Failed',
    '重跑': 'Rerun',
    '脚本化（实验性）': 'Scriptify (experimental)',
    '已排队…': 'Queued…',
    '脚本化进行中': 'Scriptification in progress',
    '诱饵清单': 'Decoy list',
    '恢复': 'Restore',
    '已暂停': 'Paused',
    '处理中': 'Processing',
    '暂停轨迹': 'Pause trajectories',
    '恢复轨迹': 'Resume trajectories',
    '配置…': 'Configure…',
    '在役': 'Active',
    '灰度中': 'In canary',
    '已下线': 'Retired',
    '恢复在役': 'Restore active',
    '删除…': 'Delete…',
    '下线': 'Retire',
    '历史曝光': 'Impression history',
    '偏好（pin / 屏蔽）': 'Preferences (pin / block)',
    '代 pin': 'Pin for user',
    '代屏蔽': 'Block for user',
    '校验通过': 'Validation passed',
    '系统通知需 HTTPS 部署': 'System notifications require HTTPS',
    '系统通知已开启': 'System notifications enabled',
    '系统通知被浏览器拒绝': 'System notifications blocked by the browser',
    '开启系统通知': 'Enable system notifications',
    '还没有通知': 'No notifications yet',
    '今天': 'Today',
    '昨天': 'Yesterday',
    '刚刚': 'Just now',
    '还没有任何用户画像': 'No user profiles yet',
    '暂无可投影的原子': 'No atoms available for projection',
    '兴趣画像': 'Interest profile',
    '兴趣点': 'Interest center',
    '技能名': 'Skill name',
    '第三方': 'Third-party',
    '冷启动': 'Cold start',
    '共同标签': 'Shared tags',
    '相似度': 'Similarity',

    // Detail views split some labels around inline counters, so boundary
    // fragments are translated explicitly.
    '总触发': 'Total triggers',
    '次': 'times',
    '· 贡献原子': '· Contributing atoms',
    '个': '',
    '· 血缘平均 ux': '· Lineage average UX',
    'main 提交': 'main commit',
    'staging 提交': 'staging commit',
    '灰度观察中 · staging HEAD': 'Under observation · staging HEAD',
    '点击看推送给谁': 'select to view recipients',
    '· 点击看推送给谁': '· select to view recipients',
    '(无提交说明)': '(no commit message)',
    '点黄点 / main HEAD 看推送给谁；其它节点看 diff': 'Select the yellow node or main HEAD to view recipients; select other nodes for the diff',
    '仅显示最近 30 个节点': 'Showing the latest 30 nodes only',
    '还没有版本触发数据': 'No version trigger data yet',
    '还没有触发记录': 'No trigger records yet',
    '来源模型': 'Source models',
    'ux 日均 · 悬停节点看当日样本数': 'Daily average UX · hover a node for that day’s sample count',
    '点击跳原子详情': 'Select to open atom details',
    '触发 / UX / 去重原子 / 首用': 'Triggers / UX / unique atoms / first use',
    '首用': 'First use',
    '版本（点击看 diff）': 'Versions (select to view diff)',
    '点左侧文件或版本、或进化路径节点查看': 'Select a file, version, or evolution node on the left',
    '非 git 仓，暂无进化路径': 'Not a Git repository; no evolution path',
    'staging 灰度中': 'staging canary',
    '把当前主干技能改得更偏可执行脚本': 'Make the current main skill more executable as a script',
    '按 content_sha 版本聚合': 'Grouped by content_sha version',
    '累计样本': 'Total samples',
    '· 当前版本': '· Current version',
    '三方技能无 git / 无灰度 staging，故无进化路径、晋升与灰度裁决版块。': 'Third-party skills have no Git or canary staging, so evolution, promotion, and canary-decision sections are unavailable.',
    'content_sha 聚合 · 样本 / UX / 首末打分': 'Grouped by content_sha · samples / UX / first and last rating',
    '关联打分原子': 'Related rating atoms',
    '非团队服务器模式，原子内容不可反查（仅评分）': 'Atom content cannot be retrieved outside team-server mode (ratings only)',
    '还没有 ux 打分数据': 'No UX rating data yet',
    '还没有关联打分原子': 'No related rating atoms yet',
    '还没有离线触发评测': 'No offline trigger evaluation yet',
    '无 case（该 skill 还没跑过触发优化）': 'No cases (trigger optimization has not run for this skill)',
    '描述质量信号——真跑代理在语义相关技能清单里抢触发；区别于上方"总触发"的线上真实使用': 'Description-quality signal: a real agent competes for activation among semantically related skills; unlike the live usage shown under “Total triggers” above',
    '诱饵数': 'Decoys',
    'test 触发率': 'Test trigger rate',
    '时间': 'Time',
    '逐 case': 'Per-case results',
    '应触发': 'Expected',
    '实测': 'Actual',
    '判定': 'Result',
    '实验': 'Experiment',
    '点"重跑"用当前描述真跑一轮探针': 'Select “Rerun” to execute a probe with the current description',
    '链表断裂，按位置排序': 'Broken chain; sorted by position',
    '原子时间线': 'Atom timeline',
    '按链表序 pre/post_atom_id · 点击节点查看详情': 'Ordered by pre/post_atom_id chain · select a node for details',
    '该轨迹还没有拆出原子': 'No atoms have been extracted from this trajectory',
    'traj — atom — skill · 贡献边标 weightscore · 点节点跳转': 'traj — atom — skill · contribution edges show weightscore · select a node to navigate',
    '该轨迹的原子尚未进入任何 skill（无贡献边）': 'No atoms from this trajectory have entered a skill yet (no contribution edges)',
    '加载原子…': 'Loading atom…',
    '未进入任何 skill': 'Not included in any skill',
    '已采纳': 'Adopted',
    '候选中': 'Candidate',
    '源已清理': 'Source cleaned up',
    '（原子文件已过期回收，保留记录）': '(expired atom file removed; record retained)',
    '源已清理（轨迹原文已过期回收，保留原子记录）': 'Source cleaned up (expired trajectory text removed; atom record retained)',
    '去向': 'Destinations',
    '行': 'Lines',
    '原文切片（按 offset 行号定位 · 只读）': 'Source excerpt (located by offset line numbers · read-only)',
    '独立只读实例隐藏路径': 'Path hidden in standalone read-only mode',
    '还没有注册目录': 'No registered directories yet',
    '样本不足': 'Insufficient samples',
    '暂无标签（轨迹还没拆出带 tags 的原子）': 'No tags yet (no atoms with tags have been extracted)',

    '已停': 'Stopped',
    'agent-worker 未在运行': 'agent-worker is not running',
    '配额排队': 'Quota queue',
    '未知技能': 'Unknown skill',
    '未知生态': 'Unknown ecosystem',
    '本批': 'This batch',
    '该席位任务已结束': 'This slot task has finished',
    '关闭': 'Close',
    '日志加载中…': 'Loading log…',
    'Cluster 批没有独立日志文件（逐轮 trace 在 split/edit 任务上）': 'Cluster batches have no separate log file (per-round traces are on split/edit tasks)',
    '该任务暂无日志文件': 'No log file for this task yet',
    '（日志为空）': '(log is empty)',
    '…（仅显示日志尾部）': '…(showing only the end of the log)',
    '这一栏同时拆几条轨迹。占满后新轨迹先等。下一轮扫描生效，不用重启。': 'How many trajectories this column splits concurrently. New trajectories wait when full. Takes effect on the next scan; no restart needed.',
    '这一栏同时归几批原子。占满后新批次先等。下一轮扫描生效，不用重启。': 'How many atom batches this column clusters concurrently. New batches wait when full. Takes effect on the next scan; no restart needed.',
    'SkillEdit 和 Generate 共用这些座位。占满后新任务先等。下一轮扫描生效，不用重启。': 'SkillEdit and Generate share these slots. New tasks wait when full. Takes effect on the next scan; no restart needed.',
    '拆分、归类、编辑共用大模型并发。数字越大越先拿到空闲名额。有 Generate 在等时仍先给它。': 'Split, cluster, and edit share large-model concurrency. Higher values get free capacity sooner; Generate still takes priority while waiting.',
    '请填写大于 0 的整数': 'Enter an integer greater than 0',
    '保存中…': 'Saving…',
    '没保存上：': 'Could not save: ',
    '流水线状态读取失败：': 'Could not read pipeline status: ',
    '跑…': 'Running…',
    '已触发': 'Triggered',
    '诱饵清单:': 'Decoy list:',
    '空': 'Empty',

    '上传': 'Upload',
    '主要贡献人': 'Primary contributor',
    '最近': 'Latest',
    '最多': 'Top',
    '采纳': 'adopted',
    '还没有上传过 skill': 'No skills uploaded yet',
    '打开技能详情': 'Open skill details',
    '查看使用情况': 'View usage',
    '无原子明细': 'No atom details',
    '近 30 天暂无使用': 'No usage in the last 30 days',
    '点评分徽章进原子页': 'Select a rating badge to open the atom page',
    '被吸收': 'Absorbed',
    '灰测中': 'In canary',
    '被吸收到': 'Absorbed into',
    '已上线': 'Released',
    '还没有线上提交的 skill commit': 'No online skill commits yet',
    '本地改 → 线上提交': 'Local edit → online submission',
    '高价值': 'High value',
    '正常': 'Normal',
    '不再推送': 'Stop pushing',
    '暂无已安装 skill': 'No installed skills',
    '安装个数为 0：服务器仍可能推送，但本机不装 harness': 'Install count is 0: the server may still push, but this client will not install into the harness',
    '个（不安装）': '(install none)',
    '被采纳': 'Adopted',
    '进入 skill': 'Entered skills',
    '移除全局 pin': 'Remove global pin',
    '暂无 client': 'No clients',
    '暂无 skill': 'No skills',
    '暂无当前推送记录': 'No current push records',
    '槽': 'slots',
    '收起历史': 'Collapse history',
    '无变更': 'no changes',
    '好评': 'Positive',
    '差劲': 'Poor',
    '一般': 'Average',
    '超时丢弃': 'Timed out',
    '匿名': 'Anonymous',
    '均分': 'Average',
    '看 diff': 'View diff',
    '修改意见': 'Edit suggestion',
    '灰度裁决': 'Canary decision',
    '全局': 'Global',
    '加载修改意见 diff…': 'Loading edit-suggestion diff…',
    '（约几秒后自动刷新）': '(refreshing automatically in a few seconds)',
    '簇': 'Cluster',
    'SKILL:技能名': 'SKILL: name',
    '2D 投影 · 悬停原子预览,点击跳详情': '2D projection · hover to preview an atom; select for details',
    '画像更新于': 'Profile updated',
    '· skill 向量索引缺失,不显示 ▲(不现算)': '· skill vector index missing; ▲ hidden (not computed on demand)',
    '共同 skill': 'Shared skills',
    '冷启动(无相似用户)': 'Cold start (no similar users)',
    '点节点看该用户画像散点': 'Select a node to view the user profile projection',
    '暂停后仍会接收并保存轨迹，恢复后自动补处理。可填写暂停原因：': 'Trajectories will still be received and saved while paused, then processed after resuming. Optional pause reason:',
    '恢复该用户的轨迹处理？暂停期间积压的轨迹将在下一轮自动处理。': 'Resume trajectory processing for this user? Trajectories queued while paused will be processed automatically on the next run.'
  };

  const PATTERNS = [
    [/^(\d+) 份使用打分$/, '$1 usage ratings'],
    [/^filtered (\d+) 条不进分母$/, 'filtered $1 excluded from denominator'],
    [/^(\d+)\/(\d+) 已裁决$/, '$1/$2 decided'],
    [/^候选孵化进度 · weightscore 满 (.+) 触发蒸馏$/, 'Candidate incubation · distill when weightscore reaches $1'],
    [/^(\d+) 个原子贡献$/, function (_text, count) { return count + (count === '1' ? ' contributing atom' : ' contributing atoms'); }],
    [/^(\d+) · (\d+) 原子$/, '$1 · $2 atoms'],
    [/^(\d+) 原子$/, '$1 atoms'],
    [/^(\d+) 条$/, '$1 items'],
    [/^(\d+) 个$/, '$1 items'],
    [/^(\d+) 次$/, '$1 times'],
    [/^(\d+) 人$/, '$1 users'],
    [/^共 (\d+) 个(.*)$/, '$1 total$2'],
    [/^匹配 (\d+) 个(.*)$/, '$1 matches$2'],
    [/^第 (\d+) \/ (\d+) 页 · 共 (\d+) 个$/, 'Page $1 / $2 · $3 total'],
    [/^‹ 上一页$/, '‹ Previous'],
    [/^下一页 ›$/, 'Next ›'],
    [/^在线 (\d+) \/ (\d+)$/, '$1 / $2 online'],
    [/^(\d+(?:\.\d+)?[smhd])前$/, '$1 ago'],
    [/^席位 (\d+)$/, 'Slots $1'],
    [/^席位 (\d+) · 空闲$/, 'Slot $1 · idle'],
    [/^席位 (\d+) · 已跑 (.+)$/, 'Slot $1 · running for $2'],
    [/^配额比 (.+)$/, 'Quota weight $1'],
    [/^批量 (\d+)$/, 'Batch $1'],
    [/^(.+)（暂只读）$/, '$1 (read-only for now)'],
    [/^进行中 (\d+)$/, '$1 running'],
    [/^排队 (\d+)$/, '$1 queued'],
    [/^完成 (\d+)$/, '$1 completed'],
    [/^失败 (\d+)$/, '$1 failed'],
    [/^本批 (\d+) 个原子$/, '$1 atoms in this batch'],
    [/^未归类 (\d+) · 排队 (\d+)$/, '$1 unclassified · $2 queued'],
    [/^(.+sessions) · (.+) 已采纳$/, '$1 · $2 adopted'],
    [/^(.+sessions) · (.+) 候选中$/, '$1 · $2 candidate'],
    [/^(.+sessions) · 排队中$/, '$1 · queued'],
    [/^(.+sessions) · (.+)$/, '$1 · $2'],
    [/^(.+) · 席位 (\d+) · 已跑 (.+)$/, '$1 · slot $2 · running for $3'],
    [/^价格表 (.+) 未刷新 · (.+)，沿用旧价$/, 'Price table not refreshed for $1 · $2; using previous prices'],
    [/^加载 (.+) …$/, 'Loading $1…'],
    [/^加载 (.+) 的画像…$/, 'Loading $1 profile…'],
    [/^(.+) 的画像散点计算中…（约几秒后自动刷新）$/, '$1 profile projection is being computed… (refreshing automatically in a few seconds)'],
    [/^(.+) 的画像散点计算中…$/, '$1 profile projection is being computed…'],
    [/^(.+) 的兴趣画像$/, '$1 interest profile'],
    [/^显示 (\d+)\/(\d+) 个原子（按兴趣中心分层抽样）$/, 'Showing $1/$2 atoms (stratified by interest center)'],
    [/^(\d+) 个原子点$/, '$1 atom points'],
    [/^画像更新于 (.+)$/, 'Profile updated $1'],
    [/^触发 (\d+) 次$/, '$1 triggers'],
    [/^总触发 (\d+) 次$/, '$1 total triggers'],
    [/^贡献原子 (\d+) 个$/, '$1 contributing atoms'],
    [/^次\s+· 贡献原子$/, 'times · Contributing atoms'],
    [/^(\d+) 个原子$/, '$1 atoms'],
    [/^main HEAD · 点击看推送给谁$/, 'main HEAD · select to view recipients'],
    [/^staging HEAD · 点击看推送给谁$/, 'staging HEAD · select to view recipients'],
    [/^晋升(.+)$/, 'Promoted$1'],
    [/^回滚(.+)$/, 'Rolled back$1'],
    [/^(.+)（weightscore (.+) · 已采纳）$/, '$1 (weightscore $2 · adopted)'],
    [/^(.+)（weightscore (.+) · 候选中）$/, '$1 (weightscore $2 · candidate)'],
    [/^（weightscore (.+) · 已采纳）$/, '(weightscore $1 · adopted)'],
    [/^（weightscore (.+) · 候选中）$/, '(weightscore $1 · candidate)'],
    [/^（截取 (\d+)\/(\d+) 字符）$/, '(showing $1/$2 characters)'],
    [/^行 (\d+) – (\d+)$/, 'Lines $1–$2'],
    [/^轨迹加载失败：(.+)$/, 'Could not load trajectory: $1'],
    [/^原子加载失败:(.+)$/, 'Could not load atom: $1'],
    [/^日志读取失败：(.+)$/, 'Could not read log: $1'],
    [/^流水线状态读取失败：(.+)$/, 'Could not read pipeline status: $1'],
    [/^没保存上：(.+)$/, 'Could not save: $1'],
    [/^删除不可逆：skill 目录与 git 历史将被移除。\n请输入 skill 名确认: (.+)$/, 'Deletion is irreversible: the skill directory and Git history will be removed.\nType the skill name to confirm: $1'],
    [/^(.+) 条历史裁决无法定位到节点$/, '$1 historical decisions could not be mapped to nodes'],
    [/^(\d+) 份 · 点击看当日原子$/, '$1 ratings · select to view that day’s atoms'],
    [/^(.+)：无逐条记录$/, '$1: no individual records'],
    [/^实验 (.+) · 点"重跑"用当前描述真跑一轮探针$/, 'Experiment $1 · select “Rerun” to execute a probe with the current description'],
    [/^(.+) · (.+) · (\d+) 份打分$/, '$1 · $2 · $3 ratings'],
    [/^当前无人被推送此灰度版本$/, 'No one currently receives this canary version'],
    [/^当前无人被推送此主干版本$/, 'No one currently receives this main version'],
    [/^无 staging · 仅 main (\d+)$/, 'No staging · main only $1'],
    [/^近 (\d+) 天暂无使用$/, 'No usage in the last $1 days'],
    [/^近 (\d+) 天$/, 'Last $1 days'],
    [/^使用 (\d+)$/, '$1 uses'],
    [/^用户 (\d+)$/, '$1 users'],
    [/^均分 (.+)$/, 'Average $1'],
    [/^我触发 (\d+) 次$/, 'I triggered $1 times'],
    [/^已装 (\d+) · 服务器推送 (\d+)$/, '$1 installed · $2 pushed by server'],
    [/^匹配 (\d+) \/ (\d+) · 已装 (\d+) \/ 服务器 (\d+)$/, '$1 / $2 matches · $3 installed / $4 on server'],
    [/^服务器 skill_slots=(\d+) · client 截取安装$/, 'Server skill_slots=$1 · client installs a prefix'],
    [/^已保存：不安装$/, 'Saved: install none'],
    [/^已保存$/, 'Saved'],
    [/^保存失败$/, 'Save failed'],
    [/^(.+) 的当前推送$/, '$1 current push'],
    [/^(.+) 的当前推送 (\d+) 槽 · pinned=(\d+) blocked=(\d+)$/, '$1 current push · $2 slots · pinned=$3 blocked=$4'],
    [/^(\d+) 槽 · pinned=(\d+) blocked=(\d+)$/, '$1 slots · pinned=$2 blocked=$3'],
    [/^历史曝光加载中…$/, 'Loading impression history…'],
    [/^暂无历史曝光$/, 'No impression history'],
    [/^按首次曝光时间倒序 · (.+)$/, 'Newest first-impression first · $1'],
    [/^(\d+) 分钟前$/, '$1 minutes ago'],
    [/^(\d+) 小时前$/, '$1 hours ago'],
    [/^(.+) 触发了$/, '$1 triggered'],
    [/^均分 (.+) · (.+) 原子$/, 'Average $1 · $2 atoms'],
    [/^(.+) 手改了$/, '$1 edited'],
    [/^给 (.+)$/, 'for $1'],
    [/^(.+) pin 了$/, '$1 pinned'],
    [/^簇 (.+)$/, 'Cluster $1'],
    [/^兴趣点 · (.+)$/, 'Interest center · $1'],
    [/^第三方 SKILL:(.+) · 触发 (.+) 次$/, 'Third-party SKILL: $1 · $2 triggers'],
    [/^SKILL:(.+) · 触发 (.+) 次$/, 'SKILL: $1 · $2 triggers'],
    [/^显示 (.+)\/(.+) 个原子（按兴趣中心分层抽样）$/, 'Showing $1/$2 atoms (stratified by interest center)'],
    [/^(.+) 原子点$/, '$1 atom points'],
    [/^画像更新于 (.+) · (.+)$/, 'Profile updated $1 · $2'],
    [/^相似度 (.+) · 共同标签:(.+)$/, 'Similarity $1 · shared tags: $2'],
    [/^相似度 (.+) · 共同 skill:(.+)$/, 'Similarity $1 · shared skills: $2'],
    [/^(.+) · (.+) 原子 · 冷启动\(无相似用户\)$/, '$1 · $2 atoms · cold start (no similar users)'],
    [/^相似度 (.+)$/, 'Similarity $1'],
    [/^点节点看该用户画像散点 · 边阈值 (.+)$/, 'Select a node to view the user profile projection · edge threshold $1']
  ];

  const textOriginal = new WeakMap();
  const attributeOriginal = new WeakMap();
  let currentLanguage = 'zh';
  let observer = null;

  function translateText(value, language) {
    if (language !== 'en' || typeof value !== 'string' || !value) return value;
    const match = value.match(/^(\s*)([\s\S]*?)(\s*)$/);
    const leading = match[1];
    const core = match[2];
    const trailing = match[3];
    let translated = EN[core];
    if (translated == null) {
      for (const [pattern, replacement] of PATTERNS) {
        if (pattern.test(core)) {
          translated = core.replace(pattern, replacement);
          break;
        }
      }
    }
    return translated == null ? value : leading + translated + trailing;
  }

  function localizeTextNode(node, refreshOriginal) {
    let original = textOriginal.get(node);
    if (original == null) {
      original = node.nodeValue;
      textOriginal.set(node, original);
    } else if (refreshOriginal) {
      const expected = translateText(original, currentLanguage);
      if (node.nodeValue !== expected) {
        original = node.nodeValue;
        textOriginal.set(node, original);
      }
    }
    const next = translateText(original, currentLanguage);
    if (node.nodeValue !== next) node.nodeValue = next;
  }

  function localizeAttribute(element, name, refreshOriginal) {
    if (!element.hasAttribute(name)) return;
    let originals = attributeOriginal.get(element);
    if (!originals) {
      originals = new Map();
      attributeOriginal.set(element, originals);
    }
    let original = originals.get(name);
    const value = element.getAttribute(name);
    if (original == null) {
      original = value;
      originals.set(name, original);
    } else if (refreshOriginal) {
      const expected = translateText(original, currentLanguage);
      if (value !== expected) {
        original = value;
        originals.set(name, original);
      }
    }
    const next = translateText(original, currentLanguage);
    if (value !== next) element.setAttribute(name, next);
  }

  function applyTree(root) {
    if (!root) return;
    if (root.nodeType === 3) {
      const parent = root.parentElement;
      if (!parent || !SKIP.has(parent.tagName)) localizeTextNode(root);
      return;
    }
    if (root.nodeType !== 1 || SKIP.has(root.tagName)) return;
    for (const name of ATTRIBUTES) localizeAttribute(root, name);
    for (const child of root.childNodes) applyTree(child);
  }

  function updateSwitch() {
    if (!global.document) return;
    for (const button of global.document.querySelectorAll('[data-language]')) {
      const active = button.dataset.language === currentLanguage;
      button.setAttribute('aria-pressed', String(active));
      button.classList.toggle('text-teal-700', active);
      button.classList.toggle('font-semibold', active);
      button.classList.toggle('text-slate-400', !active);
    }
  }

  function setLanguage(language, options) {
    const next = SUPPORTED.has(language) ? language : 'zh';
    currentLanguage = next;
    if (global.document) {
      global.document.documentElement.lang = next === 'en' ? 'en' : 'zh-CN';
      applyTree(global.document.body);
      global.document.title = next === 'en' ? 'xskill Console' : 'xskill 控制台';
      updateSwitch();
    }
    if (!options || options.persist !== false) {
      try { global.localStorage.setItem(STORAGE_KEY, next); } catch (_error) { /* storage can be disabled */ }
    }
    if (global.document && typeof global.CustomEvent === 'function') {
      global.document.dispatchEvent(new global.CustomEvent('xskill:languagechange', { detail: { language: next } }));
    }
    return next;
  }

  function init() {
    if (!global.document || !global.document.body) return;
    let saved = 'zh';
    try { saved = global.localStorage.getItem(STORAGE_KEY) || 'zh'; } catch (_error) { /* storage can be disabled */ }
    setLanguage(saved, { persist: false });
    global.document.addEventListener('click', function (event) {
      const button = event.target.closest && event.target.closest('[data-language]');
      if (button) setLanguage(button.dataset.language);
    });
    observer = new global.MutationObserver(function (mutations) {
      for (const mutation of mutations) {
        if (mutation.type === 'characterData') localizeTextNode(mutation.target, true);
        else if (mutation.type === 'attributes') localizeAttribute(mutation.target, mutation.attributeName, true);
        else for (const node of mutation.addedNodes) applyTree(node);
      }
    });
    observer.observe(global.document.body, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: true,
      attributeFilter: ATTRIBUTES
    });
  }

  global.XSkillI18n = {
    get language() { return currentLanguage; },
    setLanguage,
    translateText,
    storageKey: STORAGE_KEY
  };
  init();
})(window);
