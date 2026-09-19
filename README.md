# astrbot-plugin-nai5-mutsumi

AstrBot 插件：`nai5` / `nai5反推` / `nai5反推漫画` / `nai5本子`（v1.5.0）。

本子批处理：回传 JM zip → **本地** NSFW 过滤（不走 DeepSeek 视觉）→ 逐页 LAYOUT 漫画反推。验收见 `astrbot_plugin_nai5_mutsumi/README.md`。

```bash
git clone --branch v1.5.0 https://github.com/h1neolzr7f/astrbot-plugin-nai5-mutsumi.git
cd astrbot-plugin-nai5-mutsumi
python3 -m unittest discover -s astrbot_plugin_nai5_mutsumi/tests -v
docker cp astrbot_plugin_nai5_mutsumi/. astrbot-nai:/AstrBot/data/plugins/astrbot_plugin_nai5_mutsumi/
docker cp astrbot_plugin_nai5_mutsumi/skill/. astrbot-nai:/AstrBot/data/skills/nai5-mutsumi/
docker restart astrbot-nai
```
