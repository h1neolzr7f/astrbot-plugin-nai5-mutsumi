# 1.5.1

滤页误伤修复：OpenCV/启发式硬 `drop` 门槛抬高 + 收窄下腹 ROI + YCbCr/连通域二次确认；可疑页改 `censor`（黑块）优先于整页扔。缺 NudeNet 时用户文案标「OpenCV 降级」。`honzi_max_pages` 截断与全 drop 文案可读；发图失败不再 silently 计成功。

# 1.5.0

基线：`v1.4.23-stable`（线上舒服的漫画反推）。本版本只在独立分支增量加 `nai5本子`，**不改、不删该 tag**。`nai5反推` / `nai5反推漫画` / LAYOUT（含 slots.who 对齐、漫画关 thinking）与 1.4.23 一致。

回滚线上到稳定漫画反推：

```bash
git clone --branch v1.4.23-stable https://github.com/h1neolzr7f/astrbot-plugin-nai5-mutsumi.git
docker cp astrbot_plugin_nai5_mutsumi/. astrbot-nai:/AstrBot/data/plugins/astrbot_plugin_nai5_mutsumi/
docker cp astrbot_plugin_nai5_mutsumi/skill/. astrbot-nai:/AstrBot/data/skills/nai5-mutsumi/
```

`nai5本子 <要求>`：漫画本子批处理反推。

流程：解析指令 → 找回 JM zip（会话最近 / 引用里的本子 ID 或文件）→ **普通 File 回传安装包**（密码 `dickding`，聊天记录只放字，图不进 Nodes）→ **本地**滤页 → 保留页按序走现有 `nai5反推漫画`（LAYOUT→A1–E5，可带换角）→ 逐页普通消息出图。

进度：`nai5本子进度`；取消：`nai5本子取消`。一会话一本。

## 本地 NSFW（不走云端视觉）

用户明确不要用 DeepSeek / 任何云端视觉做审查（避免弄脏 API）。

选用 **NudeNet ONNX（可选，本机权重）→ OpenCV HSV 皮肤 → Pillow YCbCr 启发式** 的降级链：

- 外生殖器/肛门高置信：整页剔除
- 裸胸/臀：局部涂黑后保留
- 配置了 `deepseek` / `openai` / `http…` 等后端会直接拒绝启动过滤

权重：`honzi_nudenet_model_path` 或环境变量 `NUDENET_MODEL_PATH`，或 `~/.NudeNet/*.onnx`。缺权重自动降级，**不会为审查去拉云端模型**。

## 假设与取舍

- 不写死某一本 album；按 hint / 会话 index / `download_dir` mtime 选 zip
- JM `auto_delete_after_send` 若已删包，需引用仍带 ID 的消息或保证目录里还有 zip
- 漫画反推 LLM **复用** `_llm_prompt(..., manga_reverse=True)`，不复制 Skill 解析
- 好感门禁、群 NAI5 额度、opus_free（边长≤1024、steps≤28）与现网一致；每页成功计一次额度
- 全局出图锁按页持有，便于取消与其它 `nai5` 插队等待

# 1.4.21

漫画/图片反推识别：LAYOUT 几何中间表示 + 与 ppnai 互逆的 A1–E5 映射；对白摘要过滤；弱分镜/弱姿势修复。

旧 `_remap_manga_vertical_positions` 不再用「vertical/五格」关键词当主方案，仅无 LAYOUT 且轴对调高置信时兜底。详见仓库 README。
