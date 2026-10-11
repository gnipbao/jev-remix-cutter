---
name: jev-remix-cutter
description: >-
  将多段已有视频按音乐节拍或带时间标注的原创旁白重新组织成混剪、AMV、角色向短片或预告风视频。
  用于“按这首歌卡点”“做高燃混剪”“重组我的素材”等请求，提供镜头卡、Jev 判断、可替换编排、
  本地渲染与原创度报告。单条长访谈或直播挑原声高光使用 jev-highlight-cutter。
  当前支持本地打码；匿名化与双方授权换脸的执行后端尚未启用。
license: MIT
metadata:
  version: "0.1.0"
  jevkit: "1.0.0"
---

# jev-remix-cutter

你负责需求、意图卡和实际看画面；Jev 负责文字判断；CLI 负责计算与渲染。先读 [当前能力与限制](docs/coverage.md)，不要把尚未实现的效果承诺给用户。

CLI 为 `python /<skill>/scripts/remix.py`，可用外部虚拟环境中的 Python。不要把环境或任务产物放进 Skill 目录。

## 流程

1. 运行 `doctor --json`。优先检查用户全局 `TYPESAFE_API_KEY` 是否存在；存在就不用 `--key-file`。缺失时仅询问私有 key-file 路径，不索要密钥内容。
2. 确定目标时长、画幅、音乐或旁白和素材许可。用户已有说明时直接沿用。当前输出时长上限为 300 秒，两分钟用 `--target-s 120`。`init --run "$RUN" --target-s 30 --format 9:16`。用 `configure --patch` 写简洁需求、用途、音乐许可。
3. `ingest --src ... --license own|licensed|unknown`。许可由用户声明。自有自拍增加 `--self-shot`。可用 `--from-highlight <plan.json>` 只读导入高光区间。
4. `cards --prepare`，查看生成的联系表。读 [镜头卡契约](references/contracts.md)，按实际看到的画面写 JSONL，`cards --import`。不清楚的字段写 `uncertain`。
   可选 `--provider openai-compatible`：先看图像上传清单，授权后才带 `--allow-image-upload`。Provider 环境配置见 README。
5. `music --audio ...`。低置信度不能宣称自动卡点准确；审听后可显式接受，或换音乐。`music-edit` 只在可达到的乐句边界裁剪。
6. 读 [意图卡](references/intent-card.md) 和 [包](references/tracks-and-formats.md)。写新主题、弧线、钩子、避免项，`intent --validate`，再 `slots`。旁白需用户原创音轨与显式句级时间，CLI 不伪造转录。
7. `score --prepare`，读完整清单与请求数。只有本次运行的文字上传已获授权才 `score --allow-text-upload`。重试复用缓存；从不手填 Jev 分数。开发可用 `--mock`，必须明确标为测试。
8. `cast`，读 `gaps.json`。缺素材或不满足时，调整需求或补素材，不将缺口隐藏。`assemble`。
9. `review` 或 `review --serve`；查看钩子、drop、结尾联系表。用 `swap`、`lock` 或导出反馈再 `feedback`；改选角后重跑 `assemble`。
10. `originality` → `render --preview` → `qa`，参考 [审片检查](references/review-checklist.md)。正式 `render` 自动复核授权片单、指纹、时效和闪烁安全。`unknown` 素材需本次正式输出确认才能使用 `--confirm-unknown-license`。
11. `export --format edl`。交付成片、EDL、delivery.json，明确 mock、授权未知、缺口、未解决警告。变体仅用于 A/B 测试。只在用户要求时 `export-profile`。

## 必要边界

- 镜头卡必须注明真实视觉证据和来源；不能从文件名、台词猜人物、运动或情绪。Jev 只接收离散词档，无原始数值特征。
- 文字和图像授权分开、限定本次清单和运行。准备清单不读取密钥、不发请求。不打印、写入产物或日志任何凭据；不读取 `.env`。
- 不提供消重、去水印或仿声；想增加作品贡献时用 `originality`。原创度提示阈值不是平台规则，不能保证版权合规或审核通过。
- 人脸需读 [授权和人脸限制](references/compliance.md)。不代用户执行 `consent add` 或修改真实登记册。当前仅执行 keep/blur；检测漏检需视觉复查，严格隐私优先全帧打码。匿名化和真实换脸均在后端、许可、身份、年龄、色情、欺骗筛查及标识验证就绪前拒绝执行。
- 下游过期时按提示重建，不能删除依赖快照绕过检查。Mock 输出只能在明确测试目的下带 `--allow-mock`，文件名有 MOCK，不能当成真实 Jev 结果交付。

按需读取：[契约](references/contracts.md)用于手写 JSON；[手法](references/techniques.md)用于效果调整；[合规](references/compliance.md)用于上传、许可、原创度和人脸请求。
