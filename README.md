# jev-remix-cutter

独立的 Agent Skill + Python CLI：镜头感知、节拍/原创旁白槽位、Jev 判断、受约束编排、可审片的本地混剪，附原创度和素材授权报告。与 jev-highlight-cutter 不合并；通过 --from-highlight 只读导入。

版本 **0.1.0**，固定使用 `jev-1.13.0`，内置 `jevkit 1.0.0`；仓库为 [gnipbao/jev-remix-cutter](https://github.com/gnipbao/jev-remix-cutter)。这是首版 CPU 实现，完整支持状态见 [coverage.md](docs/coverage.md)。一次获授权的长素材任务已执行真实 Jev 文字调用；部分返回未通过概率分布契约校验，有效 slot_fit 未通过自动 cast 硬门槛，尚无真实 E3 完整自动选角验收。图像 Provider 未执行真实调用。M7/M8 高级合成、匿名化、真实换脸及 E4 盲测仍未完成；mock demo 只验证管线。

## 安装与运行

需要 Python 3.11+、FFmpeg/ffprobe（FFmpeg 含 libass 和 libx264）。已验证的开发解释器为 Python 3.12。先安装这些系统依赖，再克隆仓库；虚拟环境、媒体和运行结果都放在 Skill 目录之外。

```sh
mkdir -p "$HOME/.local/share/jev-skills"
git clone https://github.com/gnipbao/jev-remix-cutter.git "$HOME/.local/share/jev-skills/jev-remix-cutter"
SKILL="$HOME/.local/share/jev-skills/jev-remix-cutter"
python3.12 -m venv "$HOME/.local/share/jev-remix-cutter/venv"
PY="$HOME/.local/share/jev-remix-cutter/venv/bin/python"
"$PY" -m pip install -e "${SKILL}[core]"
"$PY" "$SKILL/scripts/remix.py" doctor --json
```

可直接用该环境的 `remix` 控制台命令，或 `"$PY" "$SKILL/scripts/remix.py"` 独立运行 CLI，不依赖已安装的 jev-highlight-cutter。Codex 用户再执行全局 Skill 注册：

```sh
"$PY" "$SKILL/scripts/install_skill.py" --python "$PY"
python3 "$SKILL/scripts/remix.py" doctor --json
```

注册默认在 `~/.codex/skills/jev-remix-cutter` 建立指向当前仓库的符号链接；设置了 CODEX_HOME 时使用该目录下的 skills。原仓库需保留，更新后符号链接自动指向新内容。安装脚本不覆盖其他已有 Skill，同一来源可重复注册，并只在 `~/.config/jev-remix/runtime.json` 保存解释器路径。scripts/remix.py 可使用登记的解释器，也可通过 JEV_REMIX_PYTHON 指定解释器。

只需独立 CLI 时也可普通安装：`"$PY" -m pip install "${SKILL}[core]"`，或安装自行构建的 wheel：`"$PY" -m pip install '/path/jev_remix_cutter-0.1.0-py3-none-any.whl[core]'`。普通包携带契约、问题库、阈值、格式和审片资源，可在源码目录之外运行 `"$HOME/.local/share/jev-remix-cutter/venv/bin/remix" doctor --json`；wheel 不注册 Codex Skill，注册仍需保留源码仓库并执行上述安装脚本。目前从 GitHub 源码安装，未发布到 PyPI。

在 Codex 中输入 `$jev-remix-cutter` 并提供素材路径、目标时长、音乐或旁白及素材许可，即可调用；Skill 可能在下一次会话中被发现。只有 Python 路径会保存为运行配置，凭据仍由用户环境或私有 key-file 提供。

核心依赖已在本机测试：NumPy2.2.6、OpenCV4.14、soundfile0.14、librosa1.0、PySceneDetect0.7.1、jsonschema4.26、PyYAML6.0。quality/asr extras只装基础库，不等于启用或自动下载专用模型。OpenCV5不兼容当前Haar扫描接口。

音乐分析默认将 Numba 编译缓存放在本次任务的 `cache/numba`，可在全局包目录只读的沙箱里运行；已显式设置 `NUMBA_CACHE_DIR` 时沿用用户配置。

Jev优先读取用户全局 `TYPESAFE_API_KEY`，仅doctor报告有/无；不使用仓库.env。只有全局缺失时才用 `--key-file <私有路径>`。不把凭据放在CLI参数或产物里。

可选图像Provider环境配置：`JEV_REMIX_VLM_ENDPOINT`（完整HTTPS /chat/completions端点）、`JEV_REMIX_VLM_MODEL`、`JEV_REMIX_VLM_API_KEY`。先 `cards --prepare`，获图像上传授权后才 `cards --provider openai-compatible --allow-image-upload`；没有账号可用Agent模式。

## 首次混剪

下列步骤沿用安装时的 `$PY` 与 `$SKILL`，并为每次任务使用新的外部目录，例如 `RUN="$HOME/jev-remix-runs/my-remix-001"`。素材和音乐许可须按真实声明填写；`own` 表示自有、`licensed` 表示已获授权、`unknown` 表示尚未确认，`--self-shot` 只用于确实自行拍摄的素材。music_license=unknown的音乐与许可未知的旁白也会在正式导出前要求确认。

`--target-s` 支持大于 0、最多 300 秒；两分钟混剪用 `--target-s 120`。目标时长按输出帧率取整到最近一帧，音乐或旁白必须覆盖该时长。较长成片增加渲染时间和 Jev 请求量，评分前仍需查看本次请求清单。

沿用原片内嵌配乐、按选中音画区间重编为外部 WAV 时，用 `configure --patch` 声明 `music_from_source_audio: true`。组装保留逐段原声来源审计，正常速度的原配乐仍计原声比例，不计为新增配乐贡献；不同时开启 `preserve_source_audio`，以免重复混音。实际音频区间必须与选中源区间一致，详见 [契约](references/contracts.md)。

```sh
RUN="$HOME/jev-remix-runs/my-remix-001"
"$PY" "$SKILL/scripts/remix.py" init --run "$RUN" --format 16:9 --target-s 30
"$PY" "$SKILL/scripts/remix.py" ingest --run "$RUN" --src /path/video.mp4 --license licensed
"$PY" "$SKILL/scripts/remix.py" cards --run "$RUN" --prepare
# 看 contact/ 联系表，按 references/contracts.md 写 cards.agent.jsonl
"$PY" "$SKILL/scripts/remix.py" cards --run "$RUN" --import /path/cards.agent.jsonl
"$PY" "$SKILL/scripts/remix.py" music --run "$RUN" --audio /path/music.wav
# configure --patch 声明音乐许可、需求；intent模板见 references/intent-card.md
"$PY" "$SKILL/scripts/remix.py" intent --run "$RUN" --validate /path/intent.json
"$PY" "$SKILL/scripts/remix.py" slots --run "$RUN"
"$PY" "$SKILL/scripts/remix.py" score --run "$RUN" --prepare
# 用户已授权本次文字发给TypeSafe后：
"$PY" "$SKILL/scripts/remix.py" score --run "$RUN" --allow-text-upload
"$PY" "$SKILL/scripts/remix.py" cast --run "$RUN"
"$PY" "$SKILL/scripts/remix.py" assemble --run "$RUN"
"$PY" "$SKILL/scripts/remix.py" review --run "$RUN"
"$PY" "$SKILL/scripts/remix.py" render --run "$RUN" --preview
"$PY" "$SKILL/scripts/remix.py" qa --run "$RUN"
"$PY" "$SKILL/scripts/remix.py" render --run "$RUN"
"$PY" "$SKILL/scripts/remix.py" export --run "$RUN" --format edl
```

不允许文字上传时可以准备清单并规划，但不能生成真实Jev判断；`--mock`仅用于明确开发测试，render需`--allow-mock`且文件名标MOCK。

每槽位 need 聚焦可观察的局部视觉作用；音画同步、源区间和正常速度由独立的确定性检查核验，不让仅接收视觉文字的 Jev 证明缺失音频事实。自动 cast 未通过时保留真实评分和失败状态，已有导演剪辑应明确记录其选择方式，不能宣称为自动选角结果或为过线修改当前阈值。实践依据见 [官方 Score 量表建议](https://docs.typesafe.ai/primitives/score) 与 [State 输入范围](https://docs.typesafe.ai/concepts/state)。

## 命令

所有任务命令接受 `--run` 和 `--json`，stdout为一个JSON结果。review --serve启动本地127.0.0.1服务，先打印一条结果，再持续服务；Ctrl+C停止。交互式consent add需要用户阅读摘要和输入确认，不用于机器JSON协议。

| 命令 | 当前行为 |
| --- | --- |
| doctor / init / configure | 环境检查，新运行，校验局部修改及历史 |
| ingest | 视频导入、代理、切分、运动色彩与联系表；highlight区间导入 |
| cards | 联系表上传预览、Agent import、可配置Provider |
| music / music-edit | 起音网格/置信度；可达乐句前缀重剪 |
| intent / slots | 主题和弧线；音乐或用户标注原创旁白槽位 |
| score | prepare、真实Jev、mock；缓存、限并发、部分失败恢复 |
| cast / assemble | beam、角色/许可片单/时长硬约束，生成timeline |
| review / frames | 本地审片、联系表；页面反馈导出 |
| swap / lock / explain | 选已评分备选、锁定/解除、解释 |
| render / qa | 帧映射、效果、混音、ASS、输出复验；未实现QA标unknown/unavailable |
| feedback / variants | 用户反馈；最多10个候选/钩子A/B版本 |
| export / export-profile | EDLv2；用户要求时保存偏好；OTIO未启用 |
| originality / license-catalog | 八项确定性指标、建议阈值、作品共享条件 |
| consent add/list/revoke/verify | 用户交互登记、检查双方scope/expiry/revocation |
| faces scan/match/plan/prepare/apply/qa | 本地keep/blur；match明确unknown，合成人脸后端未启用 |

退出码：0成功、2可恢复失败/不可达/部分失败、3过期、4需授权/授权越界、5缺依赖或未启用后端。

时间：源整数微秒，输出整数帧；变更brief/cards/music/问题库/阈值/指纹使旧结果失效。mock和真实缓存隔离，手改分数会被快照识别。

## 本地验证

```sh
PYTHONPATH="$SKILL/src:$SKILL/_vendor" "$PY" -m unittest discover -s "$SKILL/tests" -q
"$PY" "$SKILL/examples/make_demo.py" --out /tmp/remix-assets --duration-s 8
"$PY" -m pip wheel "$SKILL" --no-deps --wheel-dir /tmp/remix-wheels
```

测试只用合成素材和mock/stub，无真实费用或素材上传。默认不附真实人物照片、模型权重或不明许可音效。原创度提示不保证平台审核；blur检测不保证匿名化效果；双方同意也不替代素材使用权。

`make_demo.py` 仅生成自有几何动画、120 BPM 合成音频与已知时间标注，不调用 Jev。完整测试会在临时目录验证 mock 混剪和音频输出；真实 Jev 使用须先检查 `score --prepare` 清单，再获得本次文字上传授权。更改问题、意图或证据会形成新请求，旧授权和结果不能代替新清单审核。

## 许可证与发布范围

本项目采用 [MIT License](LICENSE)，版权声明为 `Copyright (c) 2026 gnipbao`。内置 `_vendor/jevkit` 源自 [jev-highlight-cutter](https://github.com/gnipbao/jev-highlight-cutter) 的 MIT 实现，保留其 [许可与版权声明](_vendor/jevkit/LICENSE)。第三方依赖使用各自许可证；本仓库许可不授予用户视频、音乐、人物肖像或外部模型权重的使用权。

仓库发布 Skill、源码、问题库、模板、合成测试和设计资料，不包含用户媒体、服务凭据、真实任务缓存或同意登记册。[归档主设计](docs/design.md) 与 [增补设计](docs/design-face-originality.md) 包含计划中功能，实际能力以 SKILL.md 和 coverage.md 为准；私人调研文档链接已移除。真实任务验收仅记录匿名化的技术结论，原素材及运行产物不公开。
