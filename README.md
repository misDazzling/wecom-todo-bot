# 🤖 微信 AI 待办助手

通过**企业微信官方 API + 微信客服通道**，在**普通微信**里使用的 AI 待办管理机器人。

**零封号风险、24 小时在线、多人隔离使用。**

---

## 功能一览

| 功能 | 怎么用 |
|------|--------|
| 📝 创建待办 | 发 `明天下午开会 #todo` 或转发聊天记录加 `#todo` |
| 📋 查看待办 | 发「查看待办」「今日待办」 |
| ✅ 完成销项 | 发「完成 #1」「done 3」 |
| ❌ 取消待办 | 发「取消 #2」 |
| 📊 每日总结 | 发「今日总结」（也可设自动推送） |
| ⏰ 智能提醒 | 按你设定的间隔推送未完成事项 |
| ⚙️ 个性化设置 | 每人独立：提醒间隔、静默时段、重试次数等 |
| 👥 多人使用 | 扫码即用，数据互相隔离 |
| 🆘 帮助 | 发「帮助」随时查看指令 |

---

## 架构

```
你的微信（普通微信）
    │  扫码进入客服对话
    ▼
企业微信 · 微信客服（官方通道，安全合规）
    │  POST 加密回调
    ▼
你的 4H4G 云服务器
    ├── Nginx (HTTPS)
    ├── FastAPI (Web 服务)
    ├── PostgreSQL (数据存储)
    └── DeepSeek API (AI 理解)
```

---

## 部署步骤

### 前提条件

- 一台云服务器（已安装 Docker + Docker Compose）
- 一个域名（已配置 DNS 指向服务器 IP）
- DeepSeek API Key（[platform.deepseek.com](https://platform.deepseek.com) 注册即送额度）

### 第一步：获取代码

```bash
# 将整个 wecom-todo-bot 目录上传到服务器
scp -r wecom-todo-bot/ user@your-server:/home/user/
cd /home/user/wecom-todo-bot
```

### 第二步：配置环境变量

```bash
cp .env.example .env
nano .env   # 填入真实值
```

必须填写的变量：

| 变量 | 说明 | 从哪里获取 |
|------|------|-----------|
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥 | platform.deepseek.com → API Keys |
| `WECOM_CORP_ID` | 企业 ID | 企业微信管理后台 → 我的企业 |
| `WECOM_KF_TOKEN` | 回调 Token | 微信客服后台 → 开发配置（自定义，≤32位） |
| `WECOM_KF_AES_KEY` | 回调 AES Key | 微信客服后台 → 开发配置（随机生成，43位） |
| `WECOM_KF_APP_SECRET` | 应用 Secret | 企业微信管理后台 → 应用管理 → 自建应用 |
| `PUBLIC_URL` | 你的域名 | 如 `https://todo.your-domain.com` |

### 第三步：配置企业微信

#### 3.1 注册企业微信

去 [work.weixin.qq.com](https://work.weixin.qq.com) 注册，选择"企业内部应用"，不需要认证。

#### 3.2 创建自建应用

1. 管理后台 → 应用管理 → 自建 → 创建应用
2. 记录 **AgentID** 和 **Secret**（写入 `.env` 的 `WECOM_KF_APP_SECRET`）
3. "可见范围" 设为你的企业微信账号

#### 3.3 开通微信客服

1. 管理后台 → 客户联系 → 微信客服 → 开通
2. 创建客服账号
3. **重要**：在"通过 API 管理客服账号" 处勾选你的自建应用

#### 3.4 配置回调 URL

1. 进入 [微信客服后台](https://kf.work.weixin.qq.com/) → 开发配置
2. 开启"使用企业内部接入"
3. 填写回调配置：
   - **URL**: `https://你的域名.com/webhook`
   - **Token**: 自定义（英文/数字，≤32位）
   - **EncodingAESKey**: 点击"随机获取"（固定43位）
4. **先不点保存**（等服务启动后再保存，否则 URL 验证会失败）

#### 3.5 获取客服接入链接/二维码

微信客服后台 → 客服账号 → 接入设置 → 获取二维码 → 下载

用户扫这个码就能在微信里跟你的 AI 对话。

### 第四步：配置 Nginx + HTTPS

```nginx
server {
    listen 443 ssl http2;
    server_name todo.your-domain.com;

    ssl_certificate     /etc/letsencrypt/live/your-domain/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain/privkey.pem;

    location /webhook {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 10s;
    }

    location /health {
        proxy_pass http://127.0.0.1:8000;
    }
}
```

```bash
# 申请免费 HTTPS 证书
certbot certonly --standalone -d todo.your-domain.com

# 重载 Nginx
nginx -t && nginx -s reload
```

### 第五步：启动服务

```bash
docker compose up -d --build
```

等几秒后验证：

```bash
# 健康检查
curl https://你的域名.com/health
# → {"status":"ok","service":"wecom-todo-bot"}

# 全面检查
curl https://你的域名.com/doctor
# → {"status":"healthy","issues":[],"message":"All systems operational"}
```

### 第六步：完成企业微信配置

服务启动成功后，回到微信客服后台 → 点击 **"保存"** 回调配置。

企业微信会向你的 `/webhook` 发送 GET 验证请求，验证通过后配置生效。

### 第七步：扫码测试

1. 用微信扫客服二维码
2. 进入对话，机器人自动发送欢迎语
3. 试试「帮我记一下，明天买牛奶 #todo」
4. 试试「查看待办」
5. 试试「今日总结」

---

## 配置说明

### config.yaml 结构

```yaml
system:       # 系统配置（不可被用户覆盖）
  llm:        # DeepSeek API
  wecom:      # 企业微信密钥
  server:     # 监听地址和域名

defaults:     # 全局默认值（新用户继承，用户可对话修改）
  reminder:   # 提醒策略
  daily_summary:  # 每日总结
  todo_limits:    # 待办上限

constraints:  # 用户可修改的范围（防止不合理设置）
```

### 用户个性化设置

每个用户可以通过对话修改自己的设置，与全局默认值独立：

| 可修改项 | 说明 | 默认值 |
|---------|------|--------|
| 提醒间隔 | 多久提醒一次 | 120分钟 |
| 首次提醒延迟 | 创建后多久首次提醒 | 30分钟 |
| 回复确认 | 提醒后是否要回复"收到" | 需要 |
| 未回复重试次数 | 不回复时重试几次 | 3次 |
| 未回复重试间隔 | 重试间隔 | 30分钟 |
| 静默时段 | 不打扰的时间段 | 22:00~08:00 |
| 每日总结自动发送 | 是否自动推送 | 是 |
| 每日总结时间 | 几点推送 | 21:00 |

---

## 待办状态机

```
PENDING ──timer──► REMINDING ──ack──► ACKNOWLEDGED
   │                   │                    │
   │                   │                    │
   └──── complete ─────┴──── complete ──────┘
                → COMPLETED

   cancel (any state) → CANCELLED
```

---

## 日常维护

```bash
# 查看日志
docker compose logs -f app

# 重启服务
docker compose restart app

# 更新代码后重新构建
docker compose up -d --build

# 数据库备份
docker exec todo-postgres pg_dump -U todo_user wecom_todo > backup.sql
```

---

## 成本

| 项目 | 费用 |
|------|------|
| 企业微信 | 0 元 |
| 云服务器 (4H4G) | 已有 |
| 域名 | ~50 元/年 |
| DeepSeek API | ~5-10 元/月 |
| **月均额外支出** | **≈15 元** |

---

## 常见问题

**Q: 微信客服和普通微信是什么关系？**
A: 微信客服是微信官方提供的客服功能，用户在普通微信里就能进入客服对话窗口，不需要下载任何额外的 App。

**Q: 会封号吗？**
A: 不会。全程走企业微信官方 API，不碰微信客户端，合规合法。

**Q: 能读取我的微信聊天记录吗？**
A: 不能。你需要手动把想要记录的消息**转发**到客服对话窗口。这是有意设计的安全边界。

**Q: 我朋友能用吗？**
A: 能。把客服二维码发给他，扫了就能用。每个人数据完全隔离。

**Q: 微信客服有 48 小时限制？**
A: 用户发消息后 48 小时内可以主动回复（最多 5 条）。提醒通常在这期间内，不影响正常使用。如果用户长期不互动再回来，首次消息会唤醒会话。
