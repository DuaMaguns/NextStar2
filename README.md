# 🚀 StarPath Navigator · 星途领航员

> 🌌 「同学，你的未来是哪颗星球？」
>
> 一个基于 AI 的大学生职业规划 & 学习路线生成器 —— 把迷茫的大学四年，变成一场有地图的星际探险 🗺️✨

[![Python](https://img.shields.io/badge/Python-Flask-3776AB?logo=python&logoColor=white)](#-技术栈)
[![Frontend](https://img.shields.io/badge/Frontend-原生JS·零框架-F7DF1E?logo=javascript&logoColor=black)](#-技术栈)
[![AI](https://img.shields.io/badge/AI-DeepSeek-8A2BE2)](#-配置-ai-钥匙)
[![License](https://img.shields.io/badge/License-仅供学习-green)](#-许可协议)

---

## 🤔 这是什么？

还在对着「毕业后我能干嘛」发呆吗？😵‍💫

**StarPath Navigator** 会把你的专业、爱好、MBTI 丢给 AI 🧠，然后——

- 🪐 给你一片**职业星图**：你是恒星，职业是环绕你的行星
- 🌳 给你一棵**学习路线树**：从「Hello World」一路长到「Offer 拿到手软」
- 📋 给你一份**规划报告**：学什么、学多久、用啥资源，安排得明明白白

一句话：**输入你的现状，输出你的未来航线。** 🛰️

## 🌐 在线体验

| 实例 | 地址 | 说明 |
|------|------|------|
| 🏠 主站 | [duasweb.xyz/nextstar](https://duasweb.xyz/nextstar) | 完整版，功能全家桶 🍱 |
| 🪞 镜像 | [duasweb.xyz/zhihunextstar](https://duasweb.xyz/zhihunextstar) | 知乎风精简版，支持知乎账号登录 🔵 |

## ✨ 功能亮点

- 🪐 **行星可视化** —— 行星大小 = 兴趣适配度，轨道距离 = 实现难度，一眼看出「真爱」和「远方的梦」
- 🌳 **三阶段学习路线树** —— 基础 🌱 → 进阶 🌿 → 高级 🌲，层层递进不迷路
- 🔍 **悬浮即详情** —— 鼠标放上去，学习内容、难度、时长、推荐资源全都有
- 🔭 **缩放 + 拖拽** —— 20% ~ 500% 自由缩放，宇宙任你平移
- 🖥️ **全屏模式** —— 一键沉浸，把整个银河铺满屏幕
- 🧠 **AI 质检流水线** —— 生成的路线会经过多轮「自我审查」，不合格就打回重写，绝不含糊 💪
- 🔵 **知乎登录**（镜像站）—— 知乎账号一键授权，还能生成「知乎体」职业感悟 ✍️
- 💾 **本地存储** —— 数据都在你自己的浏览器里，隐私妥妥的 🔒

## 🎮 星际之旅（使用流程）

```
📝 填写档案        🪐 挑选星球         💭 探索内心          🌳 生成路线         📋 查看报告
年级/专业/特长  →  10+ 职业行星    →  回答灵魂拷问  →    AI 绘制路线树  →   资源汇总 & 再出发
MBTI/爱好          点击最亮的星        为什么选择它？        基础→进阶→实战      换个星球也OK
```

1. **📝 填写基本信息** —— 年级、专业大类、专业名称是必填三件套，特长 / MBTI / 爱好随心填
2. **🪐 选择职业星球** —— AI 推荐 10~12 个职业，大的是真爱，远的是挑战
3. **💭 探索内心星球** —— 四个走心问题，帮 AI 更懂你
4. **🌳 生成学习路线** —— 说说你的基础与目标，剩下的交给 AI
5. **📋 查看规划报告** —— 学习资源大汇总，还能「去别的星球看看」🔁

## 🛠 技术栈

| 层 | 技术 | 备注 |
|----|------|------|
| 🎨 前端 | HTML + CSS + 原生 JavaScript | 单文件应用，零框架，纯手搓 💪 |
| ⚙️ 后端 | Python Flask | 单文件起步，部署时自动拆包 📦 |
| 🧠 AI | DeepSeek API | 职业推荐 + 路线生成 + 质量审查 |
| 🔐 登录 | 知乎 OAuth 2.0 | 镜像站专属 🔵 |
| 🚢 部署 | Gunicorn + Nginx | 双实例子路径反代 |

## 🚀 快速开始

### 🪟 Windows

双击 `start.bat`，脚本自动三连：

1. 📦 创建虚拟环境
2. 📥 安装依赖（flask、requests）
3. 🎬 启动服务

然后打开 👉 http://localhost:5000

### 🐧 Linux / 🍎 macOS

```bash
cd college-planner
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

同样打开 👉 http://localhost:5000，开冲！🏄

## 🔑 配置 AI 钥匙

在 `app.py` 里填入你的 DeepSeek API Key：

```python
DEEPSEEK_API_KEY = "your-api-key-here"
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
```

> ⚠️ **安全提示**：生产环境请把 Key 放环境变量里，硬编码一时爽，泄露火葬场 🔥

## 📡 API 接口速览

| 接口 | 方法 | 干嘛的 |
|------|------|--------|
| `/` | GET | 🎬 落地页（海报） |
| `/app` | GET | 🏠 主应用页面 |
| `/api/recommend` | POST | 🪐 推荐职业星球 |
| `/api/generate` | POST | 🌳 生成学习路线图 |
| `/api/learning_path` | POST | 🧭 学习路线（多阶段流水线版） |
| `/api/zhihu_feelings` | POST | ✍️ 生成「知乎体」职业感悟 |
| `/api/auth/zhihu/*` | GET | 🔵 知乎 OAuth 登录全家桶 |
| `/api/contact` | POST | 📮 联系我们 |

<details>
<summary>📖 展开看请求参数细节</summary>

**POST /api/recommend** —— 推荐职业

| 字段 | 说明 | 必填 |
|------|------|------|
| grade | 年级 | ✅ |
| major_category | 专业大类 | ✅ |
| major_name | 专业名称 | ✅ |
| specialty | 个人特长 | ⬜ |
| mbti | MBTI 人格 | ⬜ |
| hobbies | 爱好 | ⬜ |
| desired_career | 期望职业 | ⬜ |

**POST /api/generate** —— 生成路线（在上面参数基础上）

| 字段 | 说明 | 必填 |
|------|------|------|
| career_name | 目标职业 | ✅ |
| custom_questions | 自定义问题答案 | ⬜ |

返回结构：`nodes`（节点：起点 🟤 / 分支 🟣 / 终点 🟢）+ `connections`（连线：难度、时长）。

</details>

## 🗂 目录结构

```
college-planner/
├── 🐍 app.py              # Flask 后端（本地开发唯一源码）
├── 🦇 start.bat           # Windows 一键启动
├── 📄 requirements.txt    # Python 依赖（就俩，很轻）
├── 🎨 static/
│   └── index.html         # 前端单文件应用（HTML+CSS+JS 一体）
├── 🚀 _deploy/            # 部署脚本与双实例差异覆盖层
└── 📦 venv/               # Python 虚拟环境
```

## 🌳 路线图长啥样？

```
        🟤 起点（现在的你）
          │
    ┌─────┴─────┐
    🌱 基础学习四件套（语法/算法/数据库/Git）
          │
        🟣 职业分支点（你选的那颗星）
          │
    🌿 进阶学习（框架/工具链）
          │
    🌲 高级学习（架构/性能/原理）
          │
    ⚔️ 实战项目（简历上的硬货）
          │
    💼 求职准备（作品集/面试/简历）
          │
        🟢 终点（细分职业 · 未来的你）🎉
```

每个节点都标注了 📊 难度、⏱️ 时长、⭐ 重要程度，悬浮还能看到推荐学习资源 📚

## ❓ 常见问题

**Q1: 启动报「端口被占用」？😤**
👉 改 `app.py` 最后一行，把 `port=5000` 换成别的（比如 5001）。

**Q2: 访问 5000 显示「服务不可用」？😱**
👉 看终端日志三连查：虚拟环境建了吗？依赖装了吗？端口被占了吗？

**Q3: 路线图节点显示不全？🧐**
👉 `Ctrl + R` 刷新重新生成，宇宙偶尔也需要重启 🌌

**Q4: AI 返回的节点太少？🤏**
👉 别慌，系统会自动启用备用数据兜底，保证路线完整 🛟

## 🔐 安全说明

- ✅ AI 生成内容全部 HTML 转义，XSS 退散 🛡️
- ⚠️ 生产环境务必 `debug=False`
- ⚠️ API Key 请走环境变量，别提交进仓库 🙅

## 🌍 浏览器兼容性

Chrome / Edge 90+ ✅ ｜ Firefox 88+ ✅ ｜ Safari 14+ ✅

## 📜 许可协议

仅供学习和个人使用 🎓 拿去商用的话……请先请我喝杯咖啡 ☕

---

<div align="center">

🌟 **愿每颗迷茫的星球，都能找到自己的轨道** 🌟

Made with ❤️ and ☕ | 如果帮到了你，就给个 Star 吧 ⭐

</div>
