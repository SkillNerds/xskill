#!/usr/bin/env python3.11
"""手工对抗性 benchmark 数据集 v2 —— 轨迹拆分(trajectory splitting)精度。

设计目标:用 ≤20 条**手工精心设计**的高代表性轨迹,替换旧的 200 条单调
合成数据。每条针对一个极端 / 对抗性失败模式(隐晦追问、伪装新意图、超长
单意图、超长前言、对抗性假 ## User 标记、单 atom、快速调试循环、撤销反悔、
无 user 长段、语音转写噪声,以及若干正常基线)。

纯数据生成:不调用任何 LLM、不访问网络。

行号一律由 TrajBuilder 精确返回(它在追加 ## User 时记录 1-based 行号),
绝不手算。

输出契约
========
- data/<case_id>.md          轨迹原文
- data/<case_id>.json        sidecar: {"model": "deepseek-v4-flash"}
- ground_truth.json          {case_id: {scenario, total_lines,
                              atom_starts, boundaries, n_user_turns}}

ground-truth 纪律
=================
- boundaries 只放**真正的新意图** ## User 行号。
- 追问 / 澄清 / 纠错 / 催促 / 撤销 = 同一意图,**不进** atom_starts。
- 对抗性假 ## User(藏在 assistant 代码块里的字面量)= **不进** atom_starts,
  但解析器会把它当成 User 行 —— 这正是要考的陷阱。
- boundaries == atom_starts[1:](去掉首个被迫起点)。
"""
from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path

BENCH = Path(__file__).resolve().parent
OUT = BENCH / "data"
GT_PATH = BENCH / "ground_truth.json"


class TrajBuilder:
    """构造轨迹并精确返回每个 ## User 标题行的 1-based 行号。

    复用旧 gen_data.py 的实现契约:add_user 追加一个真实的 ## User 标题行,
    返回其行号;add_assistant / add_raw 追加正文。
    """

    def __init__(self) -> None:
        self.lines: list[str] = []

    def _emit(self, text: str) -> None:
        for ln in text.split("\n"):
            self.lines.append(ln + "\n")

    def add_user(self, text: str) -> int:
        """追加一个 ## User 轮,返回其标题行的 1-based 行号。"""
        self.lines.append("## User\n")
        header = len(self.lines)  # 1-based
        self._emit(text)
        self.lines.append("\n")
        return header

    def add_assistant(self, body: str) -> None:
        self.lines.append("## Assistant\n")
        self._emit(body)
        self.lines.append("\n")

    def add_raw(self, text: str) -> None:
        """原样追加任意文本(用于前言 / 代码块 / 大日志)。"""
        self._emit(text)

    def render(self) -> str:
        return "".join(self.lines)

    def total(self) -> int:
        return len(self.lines)


# ── 大块内容辅助(超长场景用) ────────────────────────────────────────

def big_log(approx_chars: int) -> str:
    """造一坨超长 assistant 日志(撑爆 30000 字符的窗用)。"""
    line = ("[2026-06-04 12:00:01] DEBUG worker: processing item "
            "id=%d status=ok latency_ms=%d payload_bytes=%d retry=0")
    out: list[str] = []
    i = 0
    size = 0
    while size < approx_chars:
        s = line % (i, 10 + i % 900, 100 + i % 9000)
        out.append(s)
        size += len(s) + 1
        i += 1
    return "\n".join(out)


def big_preamble(approx_chars: int) -> str:
    """造一坨超长前言(首个 ## User 之前的 metadata / 上段残留)。"""
    out = ["# session export metadata",
           "# (此前一段会话的残留导出,与本轮意图无关)"]
    size = sum(len(x) for x in out)
    i = 0
    while size < approx_chars:
        s = (f"meta_field_{i:04d} = value_{(i * 7919) % 100000} "
             f"# annotation note line carried over from a previous session")
        out.append(s)
        size += len(s) + 1
        i += 1
    return "\n".join(out)


# ── 每条 case 的构造函数。返回 (builder, atom_starts) ─────────────────
# atom_starts 只收**真正新意图**的 ## User 行号(含首个)。

def c_subtle_followup() -> tuple[TrajBuilder, list[int]]:
    """隐晦追问:用户不带任何衔接词,直接贴报错 / 只回'嗯?'/ 换说法重述,
    逻辑上仍是上一意图 —— 不该切。这是核心。"""
    b = TrajBuilder()
    starts = []
    starts.append(b.add_user("帮我把 payment 模块的超时时间从 5s 调到 30s。"))
    b.add_assistant("好的,我改一下 config/payment.yaml 里的 timeout 字段。\n"
                    "[tool: edit] timeout: 5 -> 30")
    # 追问1:直接贴报错,没有任何"还是不行"之类衔接词
    b.add_user("Traceback (most recent call last):\n"
               "  File \"pay.py\", line 88, in charge\n"
               "    raise TimeoutError(ctx)\n"
               "TimeoutError: deadline exceeded after 30000ms")
    b.add_assistant("看起来超时已经生效到 30s 了,但下游网关自己有 10s 上限。\n"
                    "[tool: read] gateway/conf.toml")
    # 追问2:只回一个"嗯?"
    b.add_user("嗯?")
    b.add_assistant("我是说网关层还有个独立的 10s 限制,得一起调。这就改。")
    # 追问3:换个说法重述同一诉求
    b.add_user("总之就是别让它再 timeout 了,你看着办。")
    b.add_assistant("明白,网关和应用层都拉到 30s,重试关掉。已验证不再超时。")
    return b, starts


def c_disguised_new_intent() -> tuple[TrajBuilder, list[int]]:
    """伪装成追问的真新意图:用'另外/对了/and also'开头但其实是全新任务
    —— 该切。考切得准不准。"""
    b = TrajBuilder()
    starts = []
    starts.append(b.add_user("给 nginx 加一个 xquiz.example.com 的反向代理。"))
    b.add_assistant("好的,加一个 server 块代理到 127.0.0.1:8080,reload。\n"
                    "[tool: bash] nginx -t && nginx -s reload  → ok")
    # "另外" 开头,但其实是完全无关的新任务(写 README)
    starts.append(b.add_user("另外,帮我给这个项目写一份 README,"
                             "包含安装和快速上手两节。"))
    b.add_assistant("好,我起草 README.md 的 Install 和 Quickstart 两节。")
    # "对了" 开头,又一个全新任务(数据库迁移)
    starts.append(b.add_user("对了,顺手把数据库从 sqlite 迁到 postgres,"
                             "schema 你照旧。"))
    b.add_assistant("明白,我写迁移脚本并切换连接串。")
    # "and also" 开头,再一个全新任务(限流)
    starts.append(b.add_user("and also 给登录接口加个每分钟 5 次的限流。"))
    b.add_assistant("好的,用滑动窗口在中间件层加限流。")
    return b, starts


def c_oversized_intent() -> tuple[TrajBuilder, list[int]]:
    """超长单意图:一个意图里塞 >30000 字符的 assistant 大日志(撑爆窗)。
    该意图内部不该有边界 —— 全程一个 atom。"""
    b = TrajBuilder()
    starts = []
    starts.append(b.add_user("跑一遍全量数据回填,把所有失败项重试,完了给我日志。"))
    b.add_assistant("开始回填,下面是完整运行日志:")
    b.add_raw("```")
    b.add_raw(big_log(34000))   # >30000 字符
    b.add_raw("```")
    b.add_assistant("回填完成:共 12000 项,全部 ok,无失败。")
    # 同意图追问(确认),不切
    b.add_user("失败项真的清零了?")
    b.add_assistant("是的,retry 列全为 0,已二次核对。")
    return b, starts


def c_huge_preamble() -> tuple[TrajBuilder, list[int]]:
    """超长前言:第一个 ## User 之前塞 >30000 字符内容。考前言会不会被丢、
    第一个真实意图行号是否仍被准确识别。"""
    b = TrajBuilder()
    starts = []
    b.add_raw(big_preamble(33000))   # >30000 字符,首个 User 之前
    starts.append(b.add_user("无视上面那段导出。帮我把 CI 里挂掉的 lint 步骤修好。"))
    b.add_assistant("好的,lint 报的是未使用 import,我清一下并跑 pylint。\n"
                    "[tool: bash] pylint src/  → 10.00/10")
    b.add_user("CI 上还是红的。")
    b.add_assistant("CI 用的是旧缓存,我清了缓存重跑,现在绿了。")
    return b, starts


def c_adversarial_marker() -> tuple[TrajBuilder, list[int]]:
    """对抗性假标记:assistant 的代码块 / 粘贴内容里出现字面量 ## User 行
    (解析器会误认成用户轮)。这**不是**边界 —— 考会不会误切在这。"""
    b = TrajBuilder()
    starts = []
    starts.append(b.add_user("帮我写个把对话 markdown 拆成轮次的解析函数。"))
    b.add_assistant(
        "好的,解析逻辑是按标题行切分。给你一段示例输入和实现:\n"
        "```markdown\n"
        "## User\n"            # 字面量假标记(藏在代码块里)
        "把日志按天分割\n"
        "## Assistant\n"
        "好的我来做\n"
        "```\n"
        "对应的 Python:\n"
        "```python\n"
        "def split_turns(text):\n"
        "    # 遇到下面这种行就开新轮\n"
        "    HEADER = '## User'\n"
        "    for line in text.splitlines():\n"
        "        if line.rstrip() == HEADER:\n"
        "            yield []\n"
        "```")
    # 同意图的真实追问
    b.add_user("帮里加上对 '## User 张三' 这种带后缀标题的支持。")
    b.add_assistant("好,改成 startswith('## User ') 也认,已加测试。")
    return b, starts


def c_single_atom() -> tuple[TrajBuilder, list[int]]:
    """整条单 atom:全程一个意图、多轮追问,boundaries 为空 []。"""
    b = TrajBuilder()
    starts = []
    starts.append(b.add_user("排查 worker 进程的内存泄漏,跑一晚上涨到 8G。"))
    b.add_assistant("先上 tracemalloc 抓快照对比。\n[tool: bash] 启动 profiler")
    b.add_user("抓到东西了吗?")
    b.add_assistant("有,一个全局 list 一直在 append 缓存的响应,没清。")
    b.add_user("那就是它了吧。")
    b.add_assistant("是,我加了 LRU 上限并定期清理。")
    b.add_user("再观察一晚?")
    b.add_assistant("已部署带监控,峰值稳定在 1.2G,泄漏解决。")
    return b, starts


def c_rapid_debug() -> tuple[TrajBuilder, list[int]]:
    """快速调试循环:同一意图下十几个极短 user 轮('再试''还不行''换个法子'),
    全部不切。"""
    b = TrajBuilder()
    starts = []
    starts.append(b.add_user("把 docker 镜像构建跑通,现在 build 报错。"))
    b.add_assistant("看是 apt 源超时,我换国内镜像源重试。")
    short = ["还不行", "再试", "换个法子", "嗯", "继续",
             "不对", "再来", "还是报错", "试试别的基础镜像", "快了快了",
             "再跑一次", "行了吗"]
    for s in short:
        b.add_user(s)
        b.add_assistant("[tool: bash] docker build .  → 调整中")
    b.add_user("好了!绿了。")
    b.add_assistant("是的,换成 slim 基础镜像 + 多阶段构建后通过了。")
    return b, starts


def c_undo_revert() -> tuple[TrajBuilder, list[int]]:
    """撤销 / 反悔:用户说'不对,撤销刚才那个' —— 仍属同一意图。"""
    b = TrajBuilder()
    starts = []
    starts.append(b.add_user("把首页的主色调从蓝色改成绿色。"))
    b.add_assistant("好的,改 theme.css 的 --primary 为 #2e7d32。")
    b.add_user("不对,撤销刚才那个改动。")
    b.add_assistant("已 revert,恢复成原来的蓝色 #1565c0。")
    b.add_user("其实还是改吧,但用浅一点的绿。")
    b.add_assistant("好,改成 #66bb6a 浅绿,已应用。")
    b.add_user("这个可以。")
    b.add_assistant("👍 已提交。")
    return b, starts


def c_no_user_long_tail() -> tuple[TrajBuilder, list[int]]:
    """无 user 长段:某意图后跟一大段纯 assistant 工具输出(无新 user 轮)。
    考末尾大段是否被正确归入最后一个 atom、是否误造边界。"""
    b = TrajBuilder()
    starts = []
    starts.append(b.add_user("把前端打包体积压下来,现在 bundle 4MB。"))
    b.add_assistant("分析依赖体积,做按需加载和 tree-shaking。")
    starts.append(b.add_user("顺便把图片上传接口换成直传 S3。"))
    b.add_assistant("好,我改成前端拿预签名 URL 直传 S3。下面是完整构建与上传验证日志:")
    b.add_raw("```")
    b.add_raw(big_log(6000))   # 一大段纯 assistant 工具输出,无新 user 轮
    b.add_raw("```")
    b.add_assistant("全部完成:bundle 降到 1.1MB,S3 直传验证通过。")
    return b, starts


def c_voice_noise() -> tuple[TrajBuilder, list[int]]:
    """语音转写噪声:带错别字 / 同音词的口语化 user 轮(模拟语音输入)。
    第二个是真新意图(尽管有错别字),该切。"""
    b = TrajBuilder()
    starts = []
    # 口语 + 同音错字:'接入'->'结入','火山'->'伙山','embedding'->'安博丁'
    starts.append(b.add_user("帮我结入一下伙山引擎的安博丁,就是那个向量化的接口。"))
    b.add_assistant("明白,接入火山引擎 embedding,改 EmbedClient 的 base_url。")
    # 同意图追问,继续带噪声:'报错'->'抱错','维度'->'喂度'
    b.add_user("抱错了,说喂度对不上。")
    b.add_assistant("火山返回 2048 维,我们配的 1024,我把维度对齐了。")
    # 真新意图(带噪声):'限流'->'线流','登录'->'登陆'
    starts.append(b.add_user("另外给登陆接口加个线流,别让人狂刷。"))
    b.add_assistant("好,给登录加每分钟 5 次限流。")
    return b, starts


def c_topic_drift_same_intent() -> tuple[TrajBuilder, list[int]]:
    """刁钻补充①:话题漂移但仍是同一意图。讨论中自然带出相邻子问题
    (配置->环境变量->本地复现),没有切换目标 —— 不该切。"""
    b = TrajBuilder()
    starts = []
    starts.append(b.add_user("把 xquiz 服务部署到 staging。"))
    b.add_assistant("好的,我用 docker compose 起到 staging。")
    b.add_user("它读哪个配置文件?")
    b.add_assistant("读 config/staging.yaml,里面引用了几个环境变量。")
    b.add_user("那这些环境变量在本地怎么给?")
    b.add_assistant("放 .env.staging,compose 里 env_file 引一下,我配好了。")
    b.add_user("本地能先验一下吗?")
    b.add_assistant("能,本地 compose 起来冒烟通过,再推 staging,已部署成功。")
    return b, starts


def c_interleaved_two_intents() -> tuple[TrajBuilder, list[int]]:
    """刁钻补充②:两个真意图被追问交错。意图A->A的追问->意图B(新)->B的追问。
    只有 B 的起点是边界,中间追问不是。"""
    b = TrajBuilder()
    starts = []
    starts.append(b.add_user("修复轨迹入库时的静默漏拆 bug。"))
    b.add_assistant("定位到续写增量重拆没触发,我补上。")
    b.add_user("贴一下复现步骤?")  # A 的追问
    b.add_assistant("续写同一 traj 第二段时,旧 atom 不重算,已修并加测试。")
    starts.append(b.add_user("再给 skill 灰度发布加个体验打分。"))  # 新意图 B
    b.add_assistant("好,异步对每轮灰度跑 LLM 打分并落盘。")
    b.add_user("打分失败会阻塞主流程吗?")  # B 的追问
    b.add_assistant("不会,best-effort 吞错,后台跑,不堵主流程。")
    return b, starts


# ── 正常基线(无陷阱,清晰 2-4 个意图) ───────────────────────────────

def c_normal_two() -> tuple[TrajBuilder, list[int]]:
    """正常基线:两个清晰意图,无陷阱。"""
    b = TrajBuilder()
    starts = []
    starts.append(b.add_user("给 payment 模块补单元测试,覆盖退款路径。"))
    b.add_assistant("好,我加 test_refund.py,覆盖正常和异常退款。\n"
                    "[tool: bash] pytest tests/test_refund.py  → 6 passed")
    starts.append(b.add_user("重新设计 dashboard 的布局,改成左侧导航。"))
    b.add_assistant("好的,改成左侧固定导航 + 右侧内容区,已更新。")
    return b, starts


def c_normal_three() -> tuple[TrajBuilder, list[int]]:
    """正常基线:三个清晰意图,各带一两轮正常对话。"""
    b = TrajBuilder()
    starts = []
    starts.append(b.add_user("重构 cli.py 里膨胀的子命令。"))
    b.add_assistant("把动作型子命令合并进 serve,状态集中到一处。")
    b.add_user("测试都过吗?")
    b.add_assistant("过,make test 全绿。")
    starts.append(b.add_user("给新域名 api.example.com 配 nginx 反向代理。"))
    b.add_assistant("加 server 块代理到 8080,签 Let's Encrypt 证书,reload。")
    starts.append(b.add_user("把 README 翻译成英文版并加切换链接。"))
    b.add_assistant("好,加 README.en.md 和顶部中英切换链接。")
    return b, starts


def c_normal_four() -> tuple[TrajBuilder, list[int]]:
    """正常基线:四个清晰意图,干净利落。"""
    b = TrajBuilder()
    starts = []
    starts.append(b.add_user("把图片上传接口换成 S3。"))
    b.add_assistant("改用预签名 URL 直传 S3,已上线。")
    starts.append(b.add_user("给用户登录加限流。"))
    b.add_assistant("加每分钟 5 次滑动窗口限流。")
    starts.append(b.add_user("把数据库从 sqlite 迁到 postgres。"))
    b.add_assistant("写好迁移脚本并切换连接串,数据已迁移。")
    starts.append(b.add_user("给项目加一个 CI lint 步骤。"))
    b.add_assistant("加 pylint 到 CI,阈值 9.0,已绿。")
    return b, starts


# ── case 注册表:(短标签, scenario 大写, 构造函数) ───────────────────
CASES = [
    ("subtle_followup",      "SUBTLE_FOLLOWUP",      c_subtle_followup),
    ("disguised_intent",     "DISGUISED_INTENT",     c_disguised_new_intent),
    ("oversized_intent",     "OVERSIZED_INTENT",     c_oversized_intent),
    ("huge_preamble",        "HUGE_PREAMBLE",        c_huge_preamble),
    ("adversarial_marker",   "ADVERSARIAL_MARKER",   c_adversarial_marker),
    ("single_atom",          "SINGLE_ATOM",          c_single_atom),
    ("rapid_debug",          "RAPID_DEBUG",          c_rapid_debug),
    ("undo_revert",          "UNDO_REVERT",          c_undo_revert),
    ("no_user_long_tail",    "NO_USER_LONG_TAIL",    c_no_user_long_tail),
    ("voice_noise",          "VOICE_NOISE",          c_voice_noise),
    ("topic_drift",          "TOPIC_DRIFT",          c_topic_drift_same_intent),
    ("interleaved",          "INTERLEAVED",          c_interleaved_two_intents),
    ("normal_two",           "NORMAL",               c_normal_two),
    ("normal_three",         "NORMAL",               c_normal_three),
    ("normal_four",          "NORMAL",               c_normal_four),
]


def _clear_output() -> None:
    for p in OUT.glob("*.md"):
        p.unlink()
    for p in OUT.glob("*.json"):
        p.unlink()


def _write_cases() -> dict[str, dict]:
    ground: dict[str, dict] = {}
    for i, (tag, scenario, fn) in enumerate(CASES):
        case_id = f"c{i:02d}_{tag}"
        b, atom_starts = fn()
        md = b.render()
        n_user = sum(1 for ln in b.lines if ln.rstrip() == "## User")
        gt = {
            "scenario": scenario,
            "total_lines": b.total(),
            "atom_starts": atom_starts,
            "boundaries": atom_starts[1:],
            "n_user_turns": n_user,
        }
        (OUT / f"{case_id}.md").write_text(md, encoding="utf-8")
        (OUT / f"{case_id}.json").write_text(
            json.dumps({"model": "deepseek-v4-flash"}), encoding="utf-8")
        ground[case_id] = gt
    return ground


def _check_atom_starts(ground: dict[str, dict]) -> int:
    bad = 0
    print("自检 1: 每个 atom_starts 行内容确为 '## User'")
    for case_id, gt in ground.items():
        lines = (OUT / f"{case_id}.md").read_text(
            encoding="utf-8").splitlines()
        for ln in gt["atom_starts"]:
            ok = (1 <= ln <= len(lines)
                  and (lines[ln - 1].rstrip() == "## User"
                       or lines[ln - 1].startswith("## User ")))
            if not ok:
                bad += 1
                got = lines[ln - 1] if 1 <= ln <= len(lines) else "<OOR>"
                print(f"  BAD {case_id}: line {ln} = {got!r}")
    return bad


def _check_boundaries(ground: dict[str, dict]) -> int:
    bad = 0
    print("自检 2: boundaries == atom_starts[1:]")
    for case_id, gt in ground.items():
        if gt["boundaries"] != gt["atom_starts"][1:]:
            bad += 1
            print(f"  BAD {case_id}: boundaries 不等于 atom_starts[1:]")
    return bad


def _check_total_lines(ground: dict[str, dict]) -> int:
    bad = 0
    print("自检 3: total_lines == 实际文件行数")
    for case_id, gt in ground.items():
        actual = len((OUT / f"{case_id}.md").read_text(
            encoding="utf-8").splitlines())
        if actual != gt["total_lines"]:
            bad += 1
            print(f"  BAD {case_id}: total_lines={gt['total_lines']} "
                  f"!= 实际 {actual}")
    return bad


def _print_size_stats(ground: dict[str, dict]) -> dict[str, int]:
    print("=" * 64)
    print(f"总条数: {len(ground)}")
    print("场景分布:", dict(Counter(g["scenario"] for g in ground.values())))
    char_sizes = {cid: len((OUT / f"{cid}.md").read_text(encoding="utf-8"))
                  for cid in ground}
    line_sizes = {cid: g["total_lines"] for cid, g in ground.items()}
    cs = list(char_sizes.values())
    ls = list(line_sizes.values())
    print(f"字符数: min={min(cs)} median={int(statistics.median(cs))} "
          f"max={max(cs)} total={sum(cs)}")
    print(f"行数:   min={min(ls)} median={int(statistics.median(ls))} "
          f"max={max(ls)}")
    return char_sizes


def _print_large_case_checks(ground: dict[str, dict], char_sizes: dict[str, int]) -> None:
    # 前言 >30000:huge_preamble 的首个 atom_start 之前的字符数
    pre_cid = next(c for c in ground if c.endswith("huge_preamble"))
    pre_lines = (OUT / f"{pre_cid}.md").read_text(
        encoding="utf-8").splitlines(keepends=True)
    first_user_ln = ground[pre_cid]["atom_starts"][0]
    preamble_chars = sum(len(x) for x in pre_lines[:first_user_ln - 1])
    print(f"前言 >30000 字符: case={pre_cid} 首个 ## User 前共 "
          f"{preamble_chars} 字符 -> {'OK' if preamble_chars > 30000 else 'FAIL'}")

    # 单意图 >30000:oversized_intent 是单 atom,整文件字符数 >30000
    over_cid = next(c for c in ground if c.endswith("oversized_intent"))
    over_chars = char_sizes[over_cid]
    over_single = len(ground[over_cid]["boundaries"]) == 0
    print(f"单意图 >30000 字符: case={over_cid} 字符数={over_chars} "
          f"单 atom={over_single} -> "
          f"{'OK' if over_chars > 30000 and over_single else 'FAIL'}")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    # 1) 清空旧数据
    _clear_output()

    ground = _write_cases()
    GT_PATH.write_text(
        json.dumps(ground, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 自检 ──────────────────────────────────────────────────────
    print("=" * 64)
    bad = (
        _check_atom_starts(ground)
        + _check_boundaries(ground)
        + _check_total_lines(ground)
    )

    # 自检 4: 场景分布 + 字符/行数统计
    char_sizes = _print_size_stats(ground)

    # 自检 5: 明确确认两条 >30000 字符的特例
    print("=" * 64)
    _print_large_case_checks(ground, char_sizes)

    print("=" * 64)
    print(f"ground-truth 自检 bad={bad}")
    if bad:
        raise SystemExit(f"自检失败:bad={bad}")
    print("全部自检通过。")


if __name__ == "__main__":
    main()
