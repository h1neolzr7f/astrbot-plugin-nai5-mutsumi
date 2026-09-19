# astrbot-plugin-nai5-mutsumi

AstrBot 插件：`nai5` / `nai5反推` / `nai5反推漫画` / `nai5本子`。

## 稳定快照

- **Tag `v1.4.23-stable`**：线上用着舒服的漫画反推 LAYOUT 版。此 tag **不会被本分支覆盖或删除**。
- `nai5本子` 只在独立分支 `feat/nai5-honzi` 增量开发；合并前漫画反推行为与 tag 一致。
- 回滚线上：`git checkout v1.4.23-stable` 后按下面 `docker cp` 部署即可（不必回滚本分支）。

```bash
# 稳妥回滚（推荐）
git clone --branch v1.4.23-stable https://github.com/h1neolzr7f/astrbot-plugin-nai5-mutsumi.git
docker cp astrbot_plugin_nai5_mutsumi/. astrbot-nai:/AstrBot/data/plugins/astrbot_plugin_nai5_mutsumi/
docker cp astrbot_plugin_nai5_mutsumi/skill/. astrbot-nai:/AstrBot/data/skills/nai5-mutsumi/

# 本分支（含 nai5本子，基线即上面的 tag）
# git clone --branch feat/nai5-honzi https://github.com/h1neolzr7f/astrbot-plugin-nai5-mutsumi.git
# python3 -m unittest discover -s astrbot_plugin_nai5_mutsumi/tests -v
```
