# Face 模块：实际能力、后端许可与标识依据

核查日期：2026-10-11。模型下载、商用授权或外部图像服务均未自动启用。

## 本版能力

本版提供 M9a 的部分能力：交互式同意登记、撤销和范围校验；OpenCV 4 前脸 Haar 检测和 IoU 轨迹；逐源帧打码缓存；上传预览；审计；渲染缓存校验。`keep`、`blur` 可运行，`swap`、`anonymize` 返回不可用或更具体的授权错误。没有伪造身份匹配、年龄、NSFW、匿名化有效性或时间稳定性结果。

Haar 会漏掉侧脸、遮挡、微小人脸和不清晰帧。IoU 轨迹不是身份聚类；检测中间最多补三个缺失帧，较长间隔断开轨迹。打码未检测到的区域不会自动消失。`faces plan --default blur --full-frame` 会处理所有入选源帧的整幅画面，代价是损失画面信息。全画面高斯模糊也不能证明人物在上下文中不可识别。

`faces qa` 的 `ok` 为 `null`，状态为 `visual_review_required`。其完整性检查只能证明指定缓存和计划一致；检测召回率、残留身份、年龄、NSFW、遮挡和伪影仍未完成自动验证。没有“人物不可识别”的交付保证。M9a 的完整验收以及 M9b、M9c 尚未达成。

同意记录位于用户私有 `~/.config/jev-remix/consents/`，不复制进运行报告。书面文档真实性由用户确认，哈希只检查文档没有变化。登记必须由用户在终端逐字确认；非 TTY 在文件写入前退出 4。每次需要授权的计划重新核对双角色、撤销、日期、项目、用途、平台和文档哈希。缺少可信身份、年龄、内容或意图判断时不能启用合成。

## 许可核查

源代码许可、模型权重许可和整条依赖链分开核查。表中“候选”不是可用后端。未来商用适配器必须对每个实际权重文件记录下载地址、哈希、许可版本和商用依据；任一必要部件不明确时拒绝商用。

| 部件 | 官方依据与许可 | 本版决策 |
| --- | --- | --- |
| OpenCV 4 库 | 4.5 及以后采用 Apache-2.0。[OpenCV 许可](https://opencv.org/license/) | 用于本地检测、解码和 Gaussian blur；版本限制 `<5`，5.0 的本次运行环境缺少 `CascadeClassifier`。 |
| 前脸 Haar cascade | XML 自带 Intel 的 BSD 风格条款，允许使用及再分发并要求保留声明。[原始 XML](https://raw.githubusercontent.com/opencv/opencv/4.x/data/haarcascades/haarcascade_frontalface_default.xml) | 从已安装 OpenCV 包加载，不重新分发权重；与 Apache 库许可分开展示。 |
| InsightFace 检测、识别、属性模型 | 库代码 MIT，官方预训练模型限非商用研究；另行商业许可单独取得。[官方 README 的 License](https://github.com/deepinsight/insightface/blob/master/python-package/README.md#license) | 不自动下载，不将代码 MIT 推断为权重可商用。 |
| FaceFusion 与 INSwapper 等模型 | 框架 OpenRAIL-AS；官方逐模型清单将 ArcFace、RetinaFace、SCRFD、INSwapper、SimSwap、FairFace 标为 Non-Commercial，HyperSwap 为 ResearchRAIL。[官方许可清单](https://docs.facefusion.io/introduction/licenses) | 仅调研，未接入；框架许可不能替代单个模型许可。 |
| GHOST | 官方仓库代码 Apache-2.0。[作者 LICENSE](https://github.com/ai-forever/ghost/blob/main/LICENSE) | 候选；实际权重和识别依赖未逐项验证，整条链商用状态未知。 |
| CodeFormer 修复 | S-Lab License 1.0 仅许可非商用，商业使用需另行联系。[作者 LICENSE](https://github.com/sczhou/CodeFormer/blob/master/LICENSE) | 不启用。 |
| GFPGAN 修复 | Apache-2.0 主许可另列第三方部件条款，包含受限部件。[作者 LICENSE](https://github.com/TencentARC/GFPGAN/blob/master/LICENSE) | 不启用；不能仅凭主许可为实际权重及所有依赖给出商用结论。 |
| 商业图像 API | 尚未选定服务、数据处理条款、费用和模型许可。 | 没有可调用适配器；`--allow-image-upload` 不会使未知后端可用。 |

`FaceSwapBackend` / `LicenseInfo` 是后续适配器的接口，不代表已有合成能力。blur 的 transfer preview 明确报告本地 CPU、零 API 费用和空上传清单。身份嵌入、授权参考图和书面同意文档不会发给 Jev。

## 标识依据与未完成工作

《人工智能生成合成内容标识办法》第四、五条涉及视频显式标识和文件元数据隐式标识，第十条要求发布者主动声明；本项目设计要求所有合成人脸输出保留两种标识且不提供关闭参数。[网信办原文](https://www.cac.gov.cn/2025-03/14/c_1743654684782215.htm)

GB 45438-2025 官方目录列为现行，实施日期为 2025-09-01。[国家标准目录](https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=F32EA2A561F1886CD8D606513512D547) 下列字段已对照政府发布的全文，而不是从第三方教程猜测。[标准全文，§5.4、§6.1、附录 E](https://www.xiongan.gov.cn/20250617/99e8309670814bdba91b3bcbfaca4e6c/2025061799e8309670814bdba91b3bcbfaca4e6c_38598ff9004470443c9195f1655adb29a0.pdf)

- 视频文字包含 AI/人工智能和生成/合成信息，放在起始画面的边或角；正常播放至少持续 2 秒，文字高度至少为画面最短边的 5%。后续实现必须在最终编码画面测量清晰度、大小和时间，不能只检查一个布尔值。
- 元数据扩展字段名称或关键词包含 `AIGC`，值为 JSON 字符串。附录 E 对象字段为 `Label`、`ContentProducer`、`ProduceID`、`ReservedCode1`、`ContentPropagator`、`PropagateID`、`ReservedCode2`；各值为字符串。合成内容 `Label` 为 `"1"`，首次写入时传播者与生成者、传播编号与制作编号分别一致。
- 最终文件只保留一份该元数据；旁路 `aigc-label.json` 不能代替写入 MP4。需按附录 E 字符约束生成并验证，复核转码后元数据仍在，并把最终核验写入交付报告。

当前 `verify_labels` 只拒绝缺少任一种标识声明的合成计划。它不检测可见文字、元数据字段或规范符合性。渲染器直接拒绝合成模式，因此不会导出声称符合标准的换脸视频。上述最终画面和元数据写入/回读验证是 M9a 尚待实现的部分，不能把这份调研文档当作已经完成标识管线。
