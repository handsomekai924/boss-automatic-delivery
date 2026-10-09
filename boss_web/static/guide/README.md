# 引导截图放这里

`boss_web/static/js/views/llm.js` 的「AI 设置」引导卡会按下面的文件名找图。
**图不是必需品**——文件不存在时前端会自动把 `<img>` 摘掉，引导退化成纯文字，
功能不受影响。

| 文件名 | 内容 |
|---|---|
| `deepseek-login.png` | 开放平台登录页（手机号 / 微信扫码那一屏） |
| `deepseek-recharge.png` | 左侧菜单里的「充值」入口 |
| `deepseek-api-keys.png` | 「API keys」页面，右上角「创建 API key」按钮要看得见 |
| `deepseek-copy-key.png` | 创建成功弹窗里的「复制」按钮 |

要求：

- **每张压到 150KB 以内**——整目录会随 static 一起打进 exe，仓库 `images/`
  里那些 0.2–1.2MB 的成品图不要往这儿放。
- 只截**需要点击的那一块**（侧边菜单 + 按钮），别整屏截图，缩小后看不清。
- 涉及账号信息的地方先打码，别把真实手机号 / Key 截进去。
