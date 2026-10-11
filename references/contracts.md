# 数据契约

权威机器契约在 `src/jev_remix/contracts/*.schema.json`。额外字段用来保持 IR 兼容，不意味着渲染器支持；渲染器遇到不支持的效果会报错。

源区间 start_us/end_us 或 in_us/out_us 是整数微秒、半开区间。输出 start_f/end_f 是整数帧、半开区间。音频节拍先取整至输出帧。源 fps 只是探测信息，不用源帧编号代替可变帧率时间戳。

`media.audio_channels` 记录实测音轨声道。音乐或保留的素材音轨具有至少两声道时，混音输出为立体声并保持左右独立；多声道输入由 FFmpeg 降至两声道。单声道旁白及合成音效居中，全部输入为单声道时输出仍为单声道。验收记录实际 `audio_channels`，混音审计记录 `channels`、各输入声道与增益；时长、响度和真峰值规范不变。

文字 `overlays` 可选 `position`（`center` / `bottom`，默认底部）、`font_size`（16–128 的整数、以目标画面像素计）和 `font_family`（最多 64 个英文字母、数字、空格、下划线或连字符）；默认样式使用 Arial。预览随画幅缩放指定字号，最低 16；系统缺少指定字体时由 FFmpeg 选择可用字体。ASS 控制符不能放入字体名。重排已有歌词可标记 `contribution: "caption"`，它不会获得原创观点贡献分。

brief.target_seconds 必须大于 0 且不超过 300；目标按 fps 四舍五入到整数帧。两分钟在 30 fps 下为 3600 帧，在 60 fps 下为 7200 帧。渲染器独立复核相同的 300 秒上限。

每个镜头卡是一行 JSON：

```json
{"shot_id":"src01#s0001","words":{"画面":"中景，主体从左向右行走","人物":["未知人物"],"情绪":"uncertain","叙事功能":"行动"},"provenance":{"mode":"agent","frames":["contact/src01-s0001.jpg"],"uncertain":["情绪"]}}
```

frames 必须引用本次运行实际存在且属于该镜头的联系表；不要照抄示例路径，先查看 shots.jsonl。命令会合并已标注镜头；变更镜头卡会使旧分数过期。分数必须由 score 命令产生。

反馈契约：

```json
{"version":1,"actions":[{"type":"swap","slot":"hook.01","rank":1},{"type":"lock","slot":"end.01","locked":true}]}
```

slot 以 slots.json/cast.json 中的实际 ID 为准。exclude 会修改 brief 并要求重新规划。运行 history 保留旧版本。

原创度只统计确定声明的新贡献：own+self_shot 的素材；authored_by=user/agent_user 的旁白；实际渲染的分析/观点文字；替换原声的 own/licensed 音乐。镜像、速度、滤镜不会被计作新贡献。授权片单约束作用于同一作品所有关联来源的总使用时长。

保留原片内嵌配乐并按同源音画区间重编为外部音频时，brief 配置 `music_from_source_audio: true`。这声明音乐文件来自选中镜头的同一源区间；组装后 audio.replaces_source_audio 为 false，audio.items 逐段记录 `source/in_us/out_us/start_f/end_f/source_audio/unaltered`。unaltered 是布尔值，只有正向恒定一倍速且源输出时长一致时为 true，表示保留原声时间关系。该数组只做来源审计，渲染仅混合 audio.path 一次，不重复混合源片；因此不能同时启用每镜头 source_audio。组装不自动核验外部音频波形，必须由实际制作记录支持同源声明。原配乐重编不计作替换配乐的新贡献，正常速度的来源映射计入 source_audio_ratio。
