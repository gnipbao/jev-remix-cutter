# 意图卡

意图卡是导演声明，不是模型推断。至少写明 theme、arc、hook、avoid。不能只把“燃”作为所有镜头的目标。先确定观众应该感受到的变化，再选镜头。

```json
{
  "version": 1,
  "theme": "从犹豫到主动行动",
  "theme_is_original": true,
  "arc": ["迟疑", "决定", "行动", "回响"],
  "hook": "先给出行动的结果，再回到决定前的一刻",
  "threads": ["角色的选择"],
  "montage": [],
  "counterpoint": [],
  "look_override": [],
  "avoid": ["误导剧情", "未经观察的动作描述"]
}
```

theme_is_original 只能在用户确实提出新主题时标记；它不是版权结论。高级蒙太奇、对位和 Look 段落例外保留为意图，不会在当前版本中自动执行。当前旁白模式需 narration.path、fingerprint 由 CLI 记录、sentences 中每句 start_f/end_f/text；区间应连续覆盖目标时间且不得重叠。

带歌词或口型的素材可在 `slot_overrides` 中约束选镜头和原时间，防止峰值对齐改变同步：

```json
{
  "slot_overrides": {
    "slot001": {
      "allowed_shots": ["src001#s001"],
      "source_range": {"in_us": 1000000, "out_us": 3000000}
    }
  },
  "music_slots": [
    {"start_f": 0, "end_f": 60, "need": "第一句歌词对应画面", "section": "开场", "mood": "克制"},
    {"start_f": 60, "end_f": 120, "need": "第二句歌词对应画面", "section": "推进", "mood": "紧张"}
  ]
}
```

以上示例对应 30 fps、4 秒目标；`music_slots` 优先于自动节拍分槽，必须连续覆盖全部目标帧。仍须先分析实际剪辑后音轨，并通过音频指纹、低置信度复核和上传授权门禁。`beat_anchor_f` 只在槽起点与实测节拍帧完全相同时生成，不能自行声明；非节拍边界会明确提示复核。

`allowed_shots` 必须是非空、无重复且已存在的 shot ID 列表。`source_range` 必须完整落在候选镜头内，长度应与输出槽在 1x 下相等（整数微秒取整容差 2 微秒）；禁止曲线变速、倒放或定格。指定区间仍需 Jev 打分并满足语义、人物、排除、锁和素材授权约束。过期锁与指定区间冲突会报错，不能静默改写同步区间。
