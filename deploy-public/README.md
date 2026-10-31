# 公网部署包

把家庭打印服务通过 frp 映射到公网，让手机 4G/5G 也能访问。

## 先做这两件事

1. 用记事本打开 [`01-先填-部署信息.txt`](01-先填-部署信息.txt)，把所有 `（）` 填好。
2. 按 [`00-先看-公网部署教程.md`](00-先看-公网部署教程.md) 操作。

## 文件说明

| 文件 | 用途 |
| --- | --- |
| `00-先看-公网部署教程.md` | 完整步骤、安全检查、故障排查 |
| `01-先填-部署信息.txt` | 需要你填写的信息单 |
| `server/02-云服务器-运行这个.sh` | 在 Ubuntu 云服务器上运行，自动安装并启动 frps |
| `windows/03-家里电脑-运行这个.ps1` | 在家里 Windows 上运行，自动下载 frpc、写配置、设置开机启动 |
| `server/frps.toml.template` | 手动部署参考 |
| `windows/frpc.toml.template` | 手动部署参考 |
| `optional-https/HTTPS-可选教程.md` | 强烈建议阅读：给公网入口加 HTTPS |

## 最短流程

```text
云服务器：sudo bash 02-云服务器-运行这个.sh
家里电脑：powershell -ExecutionPolicy Bypass -File 03-家里电脑-运行这个.ps1
手机 4G：http://（云服务器公网 IP）:（外网访问端口）
```

> 公网 HTTP 的密码不是加密传输。长期使用请按可选教程加 HTTPS，并务必修改 `ACCESS_PASSWORD`。
