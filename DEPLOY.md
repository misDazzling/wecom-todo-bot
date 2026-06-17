# 部署指南 — 从零到能用（完整步骤）

---

## 总览

一共 **两大阶段**：

| 阶段 | 做什么 | 在哪里操作 |
|------|--------|-----------|
| **A. 企业微信配置** | 注册、创建应用、开通客服、配回调URL | 浏览器：企业微信后台 + 微信客服后台 |
| **B. 服务器部署** | 装 Docker、配 Nginx HTTPS、启动服务 | SSH 连服务器 |

两个阶段没有严格先后顺序，但建议先做服务器部署（让回调 URL 可访问），再去企业微信后台保存配置。

---

## 阶段 A：企业微信配置

### A1. 注册企业微信（5分钟）

1. 打开 https://work.weixin.qq.com/
2. 点「立即注册」→ 选「企业内部应用」
3. 用你的微信扫码，填企业名称（随便填，比如"个人工作室"）
4. **不需要认证**（认证要钱），不认证也能用微信客服的全部功能
5. 注册完成后自动进入管理后台

### A2. 创建自建应用（拿到密钥）

1. 管理后台 → **应用管理** → **自建** → **创建应用**
2. 填写：
   - 应用名称：`待办助手`
   - 应用 Logo：随便传
   - 可见范围：选你自己（以后可以加别人）
3. 创建完成后，记录三个东西：

| 名称 | 在哪里看 | 示例 |
|------|---------|------|
| **企业 ID (CorpID)** | 管理后台 → 我的企业 → 企业信息 最下面 | `ww1234567890abcdef` |
| **AgentID** | 应用管理 → 待办助手 → 页面上直接显示 | `1000003` |
| **Secret** | 应用管理 → 待办助手 → 点「查看」→ 微信扫码后显示 | 一串长字符 |

> 📝 这三个值填入 `.env` 文件：CorpID → `WECOM_CORP_ID`，Secret → `WECOM_KF_APP_SECRET`

### A3. ⚠️ 设置「接收消息服务器 URL」（解锁可信 IP 配置入口）

> **为什么必须做这一步？** 企业微信要求先证明你有一个能收消息的服务器，才允许配置可信 IP。有两个选项：
> - 「可信域名」：需要域名 ICP 备案主体与企业一致，麻烦
> - **「接收消息服务器 URL」**：只需一个公网可达的 URL，简单 —— **选这个**

**操作**：

1. 管理后台 → 应用管理 → 待办助手
2. 往下滚，找到「**接收消息**」板块 → 点击「**设置 API 接收**」
3. 弹出三个输入框：

| 输入框 | 填什么 | 说明 |
|--------|--------|------|
| **URL** | `https://todo.你的域名.com/webhook` | 跟微信客服回调用同一个地址 |
| **Token** | 跟 `WECOM_KF_TOKEN` 填**一样的** | 复用同一套密钥，省事 |
| **EncodingAESKey** | 跟 `WECOM_KF_AES_KEY` 填**一样的** | 同上（43位） |

4. **先别点保存！** 等服务器部署好、`/webhook` 能通之后再回来保存。

> 💡 **原理**：我们的 `/webhook` 端点同时服务两个回调：
> ```
> 你的服务器  /webhook
>       ↑           ↑
>       │           │
> ┌─────┴─────┐ ┌──┴──────────┐
> │ 自建应用回调 │ │ 微信客服回调  │
> │ (配在这里)  │ │ (后面步骤配)  │
> │ 作用：解锁  │ │ 作用：真正    │
> │ 可信IP入口  │ │ 收消息+回复   │
> └────────────┘ └─────────────┘
> ```
> 两个回调的 GET 验证流程完全一样（签名校验 + AES 解密 echostr），所以用同一个 URL + 同一套密钥即可。自建应用这边的 POST 消息我们直接返回 `success` 不处理，真正的业务逻辑走微信客服通道。

### A4. 配置可信 IP（白名单）

> ⚠️ **A3 完成前此处的输入框是灰色的**。A3 保存成功后这里才能编辑。

1. 管理后台 → **我的企业** → **安全与保密**
2. 找到「**API 调用 IP 白名单**」
3. 填入你服务器的 **公网出口 IP**（在服务器上执行 `curl ifconfig.me` 拿到）
4. 保存

```
你的服务器 IP 怎么查？
$ curl ifconfig.me
→ 123.456.789.012  ← 填这个
```

5. **第二个地方也要配**：应用管理 → 待办助手 → **企业可信 IP** → 填同一个 IP → 保存

> 📌 两个位置都要配，一个管全局 API，一个管单个应用 API。

### A5. 开通微信客服

1. 管理后台 → **客户联系** → **微信客服** → 点「开通」
2. 点「创建客服账号」：
   - 客服名称：`待办助手`
   - 其他默认
3. 创建完成后，往下滚，找到「**通过 API 管理客服账号**」
4. 展开 → 勾选你的自建应用「待办助手」→ 保存

> ⚠️ **这一步是核心**：不勾选的话，后面在微信客服后台配置回调时不会生效。

### A6. 配置微信客服回调 URL

> 🚨 **重要区分**：这个回调 ≠ A3 的自建应用回调。微信客服的回调在**独立的微信客服后台**配置。

1. 打开 https://kf.work.weixin.qq.com/（注意不是管理后台，是客服专用后台）
2. 左侧菜单 → **开发配置** → **回调配置**
3. 开启「**使用企业内部接入**」
4. 填写三个参数：

| 参数 | 填什么 | 说明 |
|------|--------|------|
| **URL** | `https://你的域名.com/webhook` | 这就是你服务器上的回调地址 |
| **Token** | 点「随机获取」或自己编（英文/数字，≤32位） | 记下来 → `.env` 的 `WECOM_KF_TOKEN` |
| **EncodingAESKey** | 点「随机获取」（固定 43 位） | 记下来 → `.env` 的 `WECOM_KF_AES_KEY` |

5. **先不要点保存！** 等服务端部署好、URL 能通之后再回来保存。
6. 保存时企业微信会向你的 URL 发 GET 请求验证，验证通过才算配置成功。

### A7. 获取客服二维码

1. 微信客服后台 → **客服账号** → 点你的「待办助手」
2. 右上角「**接入设置**」→「**获取二维码**」
3. 下载二维码图片

> 📱 用户（或你自己）用**普通微信**扫这个二维码，就能跟 AI 对话了。不需要下载企业微信。

### A7. 关于可信域名（重要！）

**你的场景大概率不需要配可信域名。**

| 你需要什么 | 是否需要可信域名 |
|-----------|:--:|
| 微信客服回调（收消息、发消息） | ❌ 不需要 |
| 调用 sync_msg / send_msg API | ❌ 不需要（只需要可信 IP） |
| OAuth 网页授权（在网页里获取用户身份） | ✅ 需要 |
| 在微信内置浏览器打开网页 | ✅ 需要 |

因为你走的是**微信客服通道**（用户直接在微信客服对话窗口聊天），不需要网页授权，所以**可信域名可以跳过**。省去了域名备案主体必须和企业一致的麻烦。

> 如果以后你想做网页端管理后台，再来配可信域名。那时需要：
> 1. 域名 ICP 备案主体与你注册企业微信的主体一致
> 2. 下载 `WW_verify_xxx.txt` 放在域名根目录
> 3. 在企业微信后台完成验证

---

## 阶段 B：服务器部署

### B1. 环境准备

```bash
# SSH 连上你的 4H4G 服务器

# 1. 安装 Docker（如果还没装）
curl -fsSL https://get.docker.com | sh
systemctl enable docker && systemctl start docker

# 2. 安装 Docker Compose
apt install docker-compose-plugin -y   # Debian/Ubuntu
# 或
yum install docker-compose-plugin -y   # CentOS

# 3. 验证
docker --version
docker compose version
```

### B2. 上传代码

```bash
# 在本地电脑上
cd D:\code\claude_project\wx
scp -r wecom-todo-bot user@你的服务器IP:/home/user/

# 或者用 git
# 在服务器上: git clone <你的仓库地址>
```

### B3. 配置环境变量

```bash
# 在服务器上
cd /home/user/wecom-todo-bot
cp .env.example .env
nano .env
```

填入真实值：

```env
DEEPSEEK_API_KEY=sk-xxxxxxxx          # platform.deepseek.com 注册获取
DEEPSEEK_API_BASE=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-chat

WECOM_CORP_ID=ww1234567890abcdef      # A2 步骤记录的
WECOM_KF_TOKEN=你的Token               # A5 步骤填的那个
WECOM_KF_AES_KEY=你的43位AESKey       # A5 步骤填的那个
WECOM_KF_APP_SECRET=你的应用Secret     # A2 步骤记录的

PUBLIC_URL=https://todo.你的域名.com   # 你的域名
POSTGRES_PASSWORD=设置一个强密码
```

测试配置加载：

```bash
docker compose run --rm app python -c "
from app.config import load_config
c = load_config()
print('Config OK:', c.system.wecom.corp_id[:4], '...')
"
```

### B4. 配置 HTTPS

> ⚠️ 微信客服回调 URL **必须是 HTTPS**，且域名必须公网可达。

```bash
# 1. 安装 Nginx
apt install nginx certbot python3-certbot-nginx -y

# 2. 确保域名 DNS 已指向服务器 IP
# 在本地执行: nslookup todo.你的域名.com

# 3. 申请免费 SSL 证书
certbot certonly --standalone -d todo.你的域名.com
# 证书路径: /etc/letsencrypt/live/todo.你的域名.com/

# 4. 配置 Nginx
nano /etc/nginx/sites-available/wecom-todo
```

Nginx 配置内容：

```nginx
server {
    listen 80;
    server_name todo.你的域名.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name todo.你的域名.com;

    ssl_certificate     /etc/letsencrypt/live/todo.你的域名.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/todo.你的域名.com/privkey.pem;

    # 微信客服回调（最关键）
    location /webhook {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 5s;    # 微信要求5秒内响应
    }

    # 健康检查
    location /health {
        proxy_pass http://127.0.0.1:8000;
    }

    location /doctor {
        proxy_pass http://127.0.0.1:8000;
    }
}
```

```bash
# 5. 启用站点
ln -s /etc/nginx/sites-available/wecom-todo /etc/nginx/sites-enabled/
rm /etc/nginx/sites-enabled/default   # 删除默认站点
nginx -t && systemctl reload nginx
```

### B5. 启动服务

```bash
cd /home/user/wecom-todo-bot

# 构建并启动
docker compose up -d --build

# 查看日志确认启动成功
docker compose logs -f app
# 看到: "WeChat Todo Bot is ready" 就 OK 了

# 验证
curl https://todo.你的域名.com/health
# → {"status":"ok","service":"wecom-todo-bot"}

curl https://todo.你的域名.com/doctor
# → {"status":"healthy","issues":[],"message":"All systems operational"}
```

### B6. 回到企业微信后台保存回调配置

服务跑起来之后，回到 [微信客服后台](https://kf.work.weixin.qq.com/) → 开发配置 → 回调配置 → 点**保存**。

企业微信会向 `https://todo.你的域名.com/webhook` 发 GET 请求验证。如果看到"保存成功"，说明你的解密逻辑正确、URL 通了。

> 如果保存失败：
> - 检查防火墙是否开放 443 端口
> - 检查 Nginx 是否在监听 443
> - 检查 Docker 容器是否在运行 (`docker ps`)
> - 查看 app 日志：`docker compose logs app | tail -50`

---

## B7. 扫码测试

1. 用普通微信扫 A6 步骤拿到的二维码
2. 进入客服对话窗口
3. 机器人自动发欢迎语
4. 测试：
   ```
   你: 明天下午3点开会 #todo
   AI: ✅ 已记录 #1
        事项：明天下午3点开会
        当前活跃待办：1 条

   你: 查看待办
   AI: 📋 当前待办 (1项)：
         #1 明天下午3点开会

   你: 今日总结
   AI: 📊 今日总结...

   你: 完成 #1
   AI: ✅ #1 已完成：明天下午3点开会
        还剩 0 件事待完成
   ```

---

## 附加：把二维码发给朋友用

1. 管理后台 → 应用管理 → 待办助手 → **可见范围** → 添加朋友的企微账号
2. 朋友用微信扫同一个客服二维码
3. 朋友的数据跟你完全隔离，设置也是独立的

---

## 配置检查清单

部署完成后逐一核对：

| # | 检查项 | 验证方式 |
|---|--------|---------|
| 1 | Docker 运行中 | `docker ps` 看到 2 个容器 |
| 2 | Nginx HTTPS 正常 | 浏览器打开 `https://域名.com/health` 返回 OK |
| 3 | Doctor 无报错 | `curl https://域名.com/doctor` 返回 healthy |
| 4 | 可信 IP 已配置 | 管理后台两处都检查 |
| 5 | 微信客服 API 已授权 | 管理后台 → 微信客服 → 通过API管理 → 已勾选 |
| 6 | 回调 URL 保存成功 | 微信客服后台 → 回调配置 → 已保存无报错 |
| 7 | 扫码能对话 | 微信扫客服码进入对话窗口 |
| 8 | 创建待办正常 | 发 #todo 消息能收到确认 |
| 9 | DeepSeek 扣费正常 | platform.deepseek.com 查看调用记录 |

---

## 常见问题

**Q: 回调 URL 保存时报"openapi 回调地址请求不通过"？**
A: 检查：① 服务是否启动 ② Nginx 是否反代到 8000 ③ 域名 HTTPS 是否正常 ④ 企业微信服务器是否能访问到你的域名（防火墙/安全组是否开放 443）

**Q: 发消息后没收到回复？**
A: 检查 `docker compose logs app` 里的错误日志。常见原因：① access_token 获取失败（检查 CorpID/Secret）② send_msg API 调用失败（检查可信 IP）③ intent 分类异常

**Q: sync_msg 拉不到消息？**
A: 确认：① 微信客服后台回调配置已保存 ② 回调能收到 POST 事件 ③ 事件类型是 `kf_msg_or_event` ④ open_kfid 正确
