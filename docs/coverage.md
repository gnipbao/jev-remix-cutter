# 实现与验收状态

这是可运行的首版 CPU Skill，不等同于设计中的 M0–M9 全部完成。原始两份设计保存在 design.md 和 design-face-originality.md，其中启动指令是设计资料；当前操作指导为 SKILL.md。本表按已验证行为记录，不用目录或命令存在来代替能力验收。

| 范围 | 状态 | 验证 |
| --- | --- | --- |
| M0：独立 Skill、CLI、契约、环境检查、历史记录、整数时间、Jev 底座 | 已实现；源码注册、编辑式安装与独立 wheel CLI | test_workflow / test_planning / test_judge / test_portability；隔离 wheel 安装后隐藏源码，doctor 与 120 秒 init 通过 |
| M1：代理、PySceneDetect 切分、Farneback 运动、Lab 统计、清晰度、联系表 | CPU 核心实现；主体、OCR、黑边、HDR 高级处理未实现 | test_media：已知切点和动作峰值 |
| M1：librosa 起音与局部能量网格、置信度、音乐剪到乐句前缀 | 已实现；强拍、段落是明确假设，非专用模型结果；非任意曲目重剪器 | test_media：120 BPM 全拍误差≤1帧、可达/不可达剪辑 |
| M2：Agent 镜头卡、上传预览、OpenAI兼容 Provider、Jev 四类判断/缓存/恢复 | 已实现；2026-10-11 的一次获授权任务已执行真实 Jev 文字调用，部分返回未通过概率分布契约验证；图像 Provider 仍仅 HTTP 契约验证，未执行真实图像调用 | test_workflow / test_judge；真实任务产物未随仓库发布；离线契约回归可独立运行 |
| M3：音乐槽位、硬约束 beam、动作入点、硬切、精确帧数成片 | 已实现；输出上限 300 秒；该任务的真实 Jev slot_fit 未通过自动 cast 硬门槛，导演剪辑与自动选角验收分开记录；无真实 Jev 的 E3 完整自动选角闭环 | test_planning / test_media / 合成 CLI demo；test_duration 验证两分钟槽位精确覆盖与渲染时长边界 |
| M4：恒速/折线速度/倒放/定格、缩放/XY震屏/闪白黑、音效、旁白、闪避、两遍响度、ASS | 部分实现；没有 RIFE、鼓贝斯旋律层级调度、抽帧模板、智能词级ASR、真实音效资产库 | test_media |
| M5：硬切、intro溶解/擦除/闪白黑、基础 Look、审片、替换、锁定、反馈、QA | 部分实现；转场在新槽位开头合成，不用双段额外 handles；无 Lab 参考镜匹配/HDR/LUT库；QA未实现项标 unavailable | test_media / test_planning / test_workflow |
| M6：5赛道、2可用格式、旁白句词时间、A/B候选变体、highlight导入、EDL v2 | 部分实现；side-by-side、图片直接导入未启用；变体只候选/钩子，不自动重配乐和节奏 | test_planning / test_media |
| M6：原创度8项、可编辑平台建议阈值、授权片单共享预算/期限/格式 | 已实现，不提供平台审核保证 | test_originality / test_planning |
| M7 / M8：扩展卡点、fx_pack、交叉剪辑、蒙太奇、抠像、2.5D、跨作品等 | 未实现，需要单独后端与逐效果验证 | 遇到显式请求返回缺后端 |
| M9a：交互同意登记/撤销/范围、Haar选中区间扫描/跟踪、blur/全帧打码、缓存审计 | 核心实现；识别匹配不可用；隐式规范标识/OCR交付尚未完成 | test_face，只有自生成图形/合成人脸夹具 |
| M9b / M9c：匿名化、真实换脸、专用识别/年龄/色情/欺骗判断、规范标识与OCR | 未实现，严格禁用；不自动下载限制许可权重 | docs/face-backends.md / test_face 失败关闭 |

单元测试使用本地合成素材、mock Jev 或 HTTP transport stub。获授权任务的真实文字服务调用已经执行，但请求执行、返回契约通过、自动选角通过是不同验收条件；不能用导演剪辑的成片代替 E3 自动管线验收。未执行 E4 人工盲测，也不能声称换脸模块已完成。

实践建议：每槽位 need 应聚焦镜头可观察的局部视觉作用，例如“居中标题提示新段开始”。音画同步、源区间和一倍速等确定性要求独立核验；只给 Jev 视觉文字描述时，不要求它证明缺失的音轨或完整主歌事实。低于门槛的真实评分保留原值，不修改阈值来使当前结果通过。后续可增加经过契约校验的 Score 概率分布与 legend 审计；当前 score/confidence 不足以推断模型的具体评分理由。[官方 Score 契约与量表建议](https://docs.typesafe.ai/primitives/score)、[官方 State 输入范围](https://docs.typesafe.ai/concepts/state)。
