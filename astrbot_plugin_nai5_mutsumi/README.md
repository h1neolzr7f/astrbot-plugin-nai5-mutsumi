# NAI5 睦画（astrbot_plugin_nai5_mutsumi）

指令：`nai5` / `nai5反推` / `nai5反推漫画` / **`nai5本子`**。DeepSeek 视觉仅用于反推写提示词；本子滤页不走云端。

**1.5.0**（分支 `feat/nai5-honzi`，基线 `v1.4.23-stable`）：`nai5本子` 批处理。漫画反推主路径未改；回滚见仓库根 README / `CHANGE.md`。

```bash
python3 -m unittest discover -s tests -v
```

Skill 在 `skill/`。整目录可 docker cp 进 AstrBot。

## nai5本子

```
nai5本子 角色替换为博士和特蕾西娅
nai5本子进度
nai5本子取消
```

1. 回传 bot 已从 JM 下好的 zip（密码默认 `dickding`）。发包对齐 JM-Cosmos：合并转发只放文字，zip 走普通 `File`，生成图走普通图片消息。
2. **本地**去掉明显不过审页/区域（NudeNet / OpenCV / 启发式）。日志带 `cloud=never`。
3. 保留页排队，一页一页走现有漫画反推管线（可带换角要求）。

### 过滤为什么不碰 DeepSeek

审查若走 DeepSeek 视觉会把本子页送进云端 API。实现里 `honzi_filter.assert_local_backend` 直接拒绝 `deepseek` / `openai` / `http…`。过滤只读本地像素或本机 ONNX。

### 权重与目录

| 配置 | 含义 | 默认 |
| --- | --- | --- |
| `honzi_jm_download_dir` | JM zip 根目录 | 空：探测 `jm_cosmos2/downloads` 等，或环境变量 `JM_DOWNLOAD_DIR` |
| `honzi_zip_password` | 解压/提示密码 | `dickding` |
| `honzi_nsfw_backend` | `auto` / `nudenet` / `opencv` / `heuristic` | `auto` |
| `honzi_nudenet_model_path` | NudeNet ONNX 绝对路径 | 空则 `NUDENET_MODEL_PATH`、`~/.NudeNet/*.onnx`、插件 `nudenet/detector.onnx` |
| `honzi_nsfw_threshold` | NudeNet 整页剔除阈值 | `0.6` |
| `honzi_max_pages` | 单次最多页 | `40` |

可选依赖（容器里已有 Pillow 即可跑启发式；有 OpenCV 更好）：

```text
nudenet
opencv-python-headless
```

把 `detector_v2_default_checkpoint.onnx` 放到上面任一路径即可启用 NudeNet，无需出网审查。

### 验收步骤

1. `python3 -m unittest discover -s tests -v`：指令解析、过滤 mock、队列状态机、zip 选取（两本不同 ID）通过。
2. 部署插件后 `jm <任意ID>` 下载一本（不要写死样例）。确认目录里有 `*_dickding.zip`。
3. 同一会话发 `nai5本子 角色替换为博士和特蕾西娅`（或引用带本子 ID 的消息）。
4. 应先收到文字说明 + **普通文件** zip；随后「本地滤页中（…不走 DeepSeek 视觉）」；再逐页出图。
5. 中途 `nai5本子进度` 能看到页码；`nai5本子取消` 在本页结束后停下。
6. AstrBot 日志检索 `honzi filter` / `cloud=never`，不应出现把滤页请求打到 DeepSeek 的记录。
7. 群额度用尽后本子页静默改 4.5；好感不足且要求 NSFW 时嘲讽拒绝，与 `nai5` 一致。

## 漫画反推（1.4.23-stable）

`nai5反推漫画` 与 tag `v1.4.23-stable` 相同：LAYOUT→A1–E5、slots.who 对齐、漫画关 thinking。本子页只是复用该管线，不改几何映射。
