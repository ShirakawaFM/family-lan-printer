#!/usr/bin/env bash
# 家庭打印 · 云服务器 frps 一键安装脚本
# 适用：Ubuntu 22.04 / 24.04（Debian 系类似）
# 运行：sudo bash 02-云服务器-运行这个.sh

set -euo pipefail

FRP_VERSION="${FRP_VERSION:-0.61.0}"
FRP_DIR="/opt/frp"
SERVICE_FILE="/etc/systemd/system/frps.service"

say() { printf '%s\n' "$*"; }
fail() { printf '❌ %s\n' "$*" >&2; exit 1; }

ask_default() {
  local prompt="$1"
  local default="$2"
  local value=""
  read -r -p "$prompt" value || true
  if [ -z "${value// /}" ]; then
    value="$default"
  fi
  printf '%s' "$value"
}

validate_port() {
  local port="$1"
  case "$port" in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ "$port" -ge 1 ] && [ "$port" -le 65535 ]
}

if [ "$(id -u)" -ne 0 ]; then
  fail "请用 root 运行：sudo bash $0"
fi

if ! command -v systemctl >/dev/null 2>&1; then
  fail "没有找到 systemd。这个脚本只支持 Ubuntu/Debian 云服务器。"
fi

if [ -f "$FRP_DIR/frps.toml" ]; then
  say "检测到已有配置：$FRP_DIR/frps.toml"
  read -r -p "是否覆盖并重新配置？(y/N)：" overwrite || true
  case "$overwrite" in
    y|Y|yes|YES) ;;
    *) say "已取消。"; exit 0 ;;
  esac
fi

if [ -z "${BIND_PORT:-}" ]; then
  BIND_PORT="$(ask_default 'frp 控制端口（默认 7000）：' '7000')"
fi
if [ -z "${REMOTE_PORT:-}" ]; then
  REMOTE_PORT="$(ask_default '外网访问端口（默认 6000）：' '6000')"
fi

validate_port "$BIND_PORT" || fail "frp 控制端口不是 1-65535 的数字"
validate_port "$REMOTE_PORT" || fail "外网访问端口不是 1-65535 的数字"
[ "$BIND_PORT" != "$REMOTE_PORT" ] || fail "控制端口和外网访问端口不能相同"

if [ -z "${TOKEN:-}" ]; then
  read -r -p 'frp token（直接回车自动生成）：' TOKEN || true
fi
if [ -z "$TOKEN" ]; then
  TOKEN="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
fi
case "$TOKEN" in
  *'"'*|*'\'*|*' '*) fail "token 不能包含空格、双引号或反斜杠" ;;
esac
if [ "${#TOKEN}" -lt 16 ]; then
  fail "token 太短，至少 16 位。建议直接回车自动生成。"
fi

say ""
say "开始安装 frps ..."

if ! command -v curl >/dev/null 2>&1; then
  say "安装 curl ..."
  apt-get update -y
  apt-get install -y curl ca-certificates
fi
if ! command -v tar >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y tar
fi

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) FRP_ARCH="amd64" ;;
  aarch64|arm64) FRP_ARCH="arm64" ;;
  armv7l|armv7) FRP_ARCH="arm" ;;
  *) fail "暂不支持这个 CPU 架构：$ARCH" ;;
esac

mkdir -p "$FRP_DIR"

if [ ! -x "$FRP_DIR/frps" ]; then
  DOWNLOAD_URL="https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_${FRP_ARCH}.tar.gz"
  TMP_DIR="$(mktemp -d)"
  trap 'rm -rf "$TMP_DIR"' EXIT
  say "下载：$DOWNLOAD_URL"
  if ! curl -fL --retry 3 --connect-timeout 15 -o "$TMP_DIR/frp.tar.gz" "$DOWNLOAD_URL"; then
    fail "下载失败。请手动下载 frp_${FRP_VERSION}_linux_${FRP_ARCH}.tar.gz，解压后把 frps 放到 $FRP_DIR/frps，再重新运行本脚本。"
  fi
  tar -xzf "$TMP_DIR/frp.tar.gz" -C "$TMP_DIR"
  FOUND_FRPS="$(find "$TMP_DIR" -type f -name frps -print -quit)"
  [ -n "$FOUND_FRPS" ] || fail "压缩包里没有找到 frps"
  install -m 0755 "$FOUND_FRPS" "$FRP_DIR/frps"
  rm -rf "$TMP_DIR"
  trap - EXIT
fi

cat > "$FRP_DIR/frps.toml" <<EOF
bindPort = ${BIND_PORT}
auth.method = "token"
auth.token = "${TOKEN}"
log.to = "$FRP_DIR/frps.log"
log.level = "info"
log.maxDays = 7
EOF

if ! id -u frp >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin frp
fi
chown -R frp:frp "$FRP_DIR"
chmod 0640 "$FRP_DIR/frps.toml"

cat > "$SERVICE_FILE" <<'EOF'
[Unit]
Description=frp server for family print
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=frp
Group=frp
WorkingDirectory=/opt/frp
ExecStart=/opt/frp/frps -c /opt/frp/frps.toml
Restart=always
RestartSec=5
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable frps >/dev/null 2>&1
systemctl restart frps

sleep 1
if ! systemctl is-active --quiet frps; then
  say ""
  say "❌ frps 启动失败，最近日志："
  journalctl -u frps -n 30 --no-pager || true
  exit 1
fi

PUBLIC_IP="$(curl -4 -fsS --max-time 5 https://ifconfig.me 2>/dev/null || true)"
if [ -z "$PUBLIC_IP" ]; then
  PUBLIC_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
fi

say ""
say "============================================================"
say "✅ frps 已安装并启动"
say "============================================================"
say "云服务器公网 IP：${PUBLIC_IP:-请用云厂商控制台查看}"
say "frp 控制端口：${BIND_PORT}"
say "外网访问端口：${REMOTE_PORT}"
say "frp token：${TOKEN}"
say ""
say "请把 token 复制到家里的 Windows 脚本，并抄到部署信息填写单。"
say ""
say "还需要在云厂商安全组 / 服务器防火墙放行："
say "  - ${BIND_PORT}/TCP（frp 控制端口）"
say "  - ${REMOTE_PORT}/TCP（外网访问端口）"
say ""
say "如果服务器开了 UFW："
say "  sudo ufw allow ${BIND_PORT}/tcp"
say "  sudo ufw allow ${REMOTE_PORT}/tcp"
say "  sudo ufw reload"
say ""
say "手机访问地址：http://${PUBLIC_IP:-云服务器公网IP}:${REMOTE_PORT}"
say "查看状态：systemctl status frps --no-pager"
say "查看日志：journalctl -u frps -n 50 --no-pager"
say "============================================================"
