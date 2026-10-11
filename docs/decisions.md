# 工程决策

- 独立仓库和 Skill，保持现有 highlight 实现不变。设计允许非维护方在 remix 内按 MIT 约定重写/复制 jevkit；保留原许可。共享底座副本 1.0.0，不把重依赖带入 highlight。
- 原始文档中的里程碑、启动 prompt 是设计来源，不等同于本次用户直接要求逐字执行的指令。实际完成状态在 coverage.md，不宣称路线图全完成。
- 本地实测 Python 3.12、NumPy 2.2.6、OpenCV 4.14、librosa 1.0.0、PySceneDetect 0.7.1。OpenCV 5 无 CascadeClassifier，约束为4.x。PySceneDetect 0.7 默认依赖 opencv-python；为避免与 headless 包重复覆盖 cv2，使用其标准包。没有为依赖改动现有 highlight。
- 原生起音/能量检测不等同于音乐重拍和段落识别。保留算法名称、置信度、assumed 标记；低置信度默认要求审听确认，不合成没观察到的拍点。参考 [PySceneDetect](https://www.scenedetect.com/docs/latest/api/detectors.html)、[OpenCV光流](https://docs.opencv.org/4.13.0/dc/d6b/group__video__track.html)。
- Renderer 先确保源时间/输出帧、效果可计算，CPU档 nearest/blend，不冒充RIFE。v1转场在新槽位开头合成前镜尾部以保持输出帧数，不支持额外handles的转场长度语义。音频保留配乐和源音轨的左右声道，单声道旁白与音效居中；纯单声道任务仍输出单声道。输出两遍 loudnorm，AAC预留0.5dB再严格复验真峰值。
- Jev 固定 `jev-1.13.0`；准备清单不读凭据；env优先key-file。Choice保留 selected option、完整概率和confidence，依据 [TypeSafe Choice 官方契约](https://docs.typesafe.ai/primitives/choice)。测试只用mock/transport stub，不因存在密钥自动消费额度。
- 图像 Provider 使用明确配置的HTTPS Chat Completions兼容端点，拒绝带凭据URL及重定向；只发联系表。参考 [官方API契约](https://developers.openai.com/api/reference/resources/chat)。无Provider账号时仍能完整使用本地Agent卡片模式。
- 原创度计时使用区间并集，授权片单按作品总预算，不逐来源重复授予；数字不代表版权结论。音轨许可未知时不自动计作新贡献。
- 人脸先完成可本地验证的登记检查和打码；Haar无身份/年龄/色情能力，不把unknown伪装成通过；真实合成未启用。标准原文和模型许可决策在 [face-backends.md](face-backends.md)，不是根据设计里的法规转述直接实现。
- Codex Skill 从保留的源码仓库注册，运行解释器放在独立虚拟环境；portable scripts/remix.py 支持外部解释器。普通 pip 和 wheel 包内置 CLI 所需资源，可脱离源码运行，但不注册 Codex Skill。模型权重、音效、LUT资产不随包自动下载。
