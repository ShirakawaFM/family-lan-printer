# 可选：给公网入口加 HTTPS

> 这一步是高级可选项。没有域名可以先跳过，但公网 HTTP 的密码不是加密传输，长期使用强烈建议加 HTTPS。

## 前提

- 已经完成 `00-先看-公网部署教程.md` 的第 1、2、3 步；
- 有一个域名，例如 `print.example.com`；
- 域名 DNS 的 A 记录已经指向 `（云服务器公网 IP）`；
- 云服务器安全组放行 `80/TCP` 和 `443/TCP`。

## 第 1 步：让 frps 只监听本机

编辑服务器上的 `/opt/frp/frps.toml`，在文件末尾加一行：

```toml
proxyBindAddr = "127.0.0.1"
```

然后重启：

```bash
sudo systemctl restart frps
```

这样外网访问端口 `（外网访问端口，默认 6000）` 只在本机监听，公网只能通过 Caddy 的 HTTPS 进来。

> 加完这一行后，原来的 `http://（云服务器公网 IP）:（外网访问端口）` 就不能从公网访问了，这是正常的。

## 第 2 步：安装 Caddy

在云服务器执行：

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy
```

## 第 3 步：写 Caddyfile

把 `optional-https/Caddyfile.template` 里的 `（）` 替换成真实值，保存到：

```text
/etc/caddy/Caddyfile
```

内容示例：

```caddyfile
（你的域名） {
    reverse_proxy 127.0.0.1:（外网访问端口，默认 6000）
}
```

检查并重载：

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

## 第 4 步：手机测试

手机用 4G/5G 打开：

```text
https://（你的域名）
```

看到登录页、输入密码、能打印，就完成了。

## 常见问题

### 证书申请失败

检查：

- 域名 A 记录是否已经生效：`ping （你的域名）`；
- 云服务器安全组是否放行 80/443；
- 80 端口有没有被 Nginx/Apache 占用；
- 看日志：`sudo journalctl -u caddy -n 80 --no-pager`。

### 页面 502 Bad Gateway

说明 Caddy 正常，但本机 frp 端口没通：

- `systemctl status frps` 是否正常；
- `/opt/frp/frps.toml` 的 `proxyBindAddr` 是否是 `127.0.0.1`；
- `（外网访问端口）` 是否和家里 frpc 配置一致；
- 家里 frpc 是否在线。

### 想退回 HTTP

删除 `proxyBindAddr = "127.0.0.1"`，重启 frps；停掉 Caddy。然后手机继续用 `http://（云服务器公网 IP）:（外网访问端口）`。

## 安全提醒

- HTTPS 只保护传输过程，不替代强密码；
- 仍然要修改 `ACCESS_PASSWORD`，不要用 `123456`；
- 不要把域名、端口、密码发到公开群聊；
- 不需要公网时，执行 `sudo systemctl disable --now frps caddy` 关闭入口。
