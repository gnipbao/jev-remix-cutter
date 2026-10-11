# 验证记录（2026-10-11）

- Skill frontmatter / 命名 / 未完成脚手架检查通过；全部20份JSON Schema自身校验通过，demo主产物通过对应Schema。
- 初次实现验收的全部88项unittest通过（当时全套6.333秒）。该轮测试仅使用mock Jev、transport stub、合成视频和临时登记册，未上传素材，未消耗真实Jev额度。
- doctor：系统FFmpeg/ffprobe/libass、核心依赖、注册后的便携脚本与editable remix入口均通过。
- 合成demo：8秒 / 240帧，640×360；输出帧数、音轨存在、音频时长、响度、真峰值检查全部通过。实测响度 -14.36 LUFS，真峰值 -1.4 dBTP。
- 120BPM网格16个观察拍，已知切点和动作峰值逐帧验证；变速峰值对齐输出第30帧。
- 独立Agent按SKILL.md前向执行，无上传模式跑通preview、正式MOCK渲染、QA、EDL、delivery；在本地审片页验证视频、镜头联系表及替换/锁定反馈。
- 独立复查修复了旧镜头卡复用、反馈排除未过滤、片单共享预算、全局配置修改后旧计划仍可渲染、匿名处理变体漏带打码、审片预览未刷新等问题，均有验证或回归测试。
- 现有jev-highlight-cutter工作树干净，未修改。
- 发布前 wheel 独立验证：复制源码构建 wheel，安装到隔离目标后隐藏该源码，`remix doctor` 与 120 秒 `init` 通过；问题库、阈值、所有格式/赛道 pack、卡片提示和审片资源可读，契约校验通过，内置 jevkit 的 LICENSE/VERSION 随包保留。该验证未调用真实服务。
- 发布前用户级全局环境验证：118 项测试通过；全局 Skill 入口的 doctor 与 120 秒 init 通过。8 秒合成音轨分析得到 240 帧和 120 BPM；Numba 默认缓存位于任务的 cache/numba，不写入全局包目录，显式 NUMBA_CACHE_DIR 保持原样。

初次实现验收未调用真实 Jev，证据为 E1/E2 加离线合成管线验证。随后一次获授权的 120 秒任务已执行真实 Jev 文字调用；部分返回未通过概率分布契约校验，当前有效 slot_fit 结果未通过自动 cast 硬门槛。因此仍无真实 E3 完整自动选角验收；该任务导演剪辑的本地渲染与音画同步核验独立记录，不能替代 E3。图像 Provider 仍未执行真实调用；未执行 E4 盲测。换脸模块仍是 M9a 部分实现，未执行真实合成人脸和规范最终标识 OCR。

源码验证：`PYTHONPATH=src:_vendor python -m unittest discover -s tests -q`。

公开记录不包含用户身份、原始视频、配乐、真实服务请求或缓存。该真实任务使用的私人产物不能从仓库复现；可独立运行的合成测试与离线验证命令见 README。
