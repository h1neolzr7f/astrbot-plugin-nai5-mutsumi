# NAI5 睦画（astrbot_plugin_nai5_mutsumi）

指令：`nai5` / `nai5反推` / `nai5反推漫画`。DeepSeek 视觉 → PROMPT/CHARACTER/UC/SIZE → ppnai NovelAI。

**1.4.21**：漫画反推用 `LAYOUT` 几何中间表示映射 A1–E5，不再把「字母当行」remap 当主方案。说明与验收见仓库根 `README.md` 与本目录 `CHANGE.md`。

```bash
python3 -m unittest discover -s tests -v
```

Skill 在 `skill/`（含 `references/坐标约定.md`）。整目录可 docker cp 进 AstrBot。
