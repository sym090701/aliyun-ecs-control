# Aliyun ECS Fleet Control

一个可自行部署的阿里云 ECS 控制台，用同一套网页和命令行管理多个阿里云账号、地域及实例。支持手动开关机、按实例定时开关机，以及基于 CDT 汇总流量的自动策略。

> [!IMPORTANT]
> 这是云控制面工具，会持有可启动和停止 ECS 的 AccessKey。公网部署必须使用 HTTPS、TOTP、最小权限 RAM 用户，并保持应用只监听本机地址。

## 功能

- 多账号、多地域、多实例统一管理；
- 网页查看实例状态、账号、地域和 CDT 汇总流量；
- 手动启动和正常停止实例，停机操作需要二次确认；
- 每台实例单独设置启动时间、停止时间、星期和 IANA 时区；
- 每台实例单独设置流量阈值和自动策略；
- 定时计划与流量策略互斥，避免两套自动化相互打架；
- 随机会话 Cookie、登录限速、同源检查和可选 TOTP；
- systemd Web 服务和每分钟自动化定时器；
- CLI 支持状态查询、开关机、策略和计划管理。

## 运行结构

```text
浏览器
  |
  | HTTPS :443
  v
Caddy / Nginx
  |
  | HTTP 127.0.0.1:8787
  v
ECS Fleet Control ----> Alibaba Cloud ECS / CDT API
  |
  +---- /etc/aliyun-ecs-control.env
  +---- /etc/aliyun-ecs-control-targets.json
  +---- /var/lib/aliyun-ecs-control/
```

Web 服务和自动化任务都以专用的 `aliyun-ecs-control` 用户运行。systemd 单元启用了 `NoNewPrivileges`、`ProtectSystem=strict`、`PrivateDevices` 等沙箱选项。

## 环境要求

- Debian 或 Ubuntu，使用 systemd；
- Python 3.8 或更高版本；
- `python3-venv`；
- 公网使用时需要 Caddy 或 Nginx 与有效 TLS 证书；
- 一个或多个阿里云 RAM 用户的 AccessKey。

RAM 用户应只授予目标实例所需的查询、启动和停止权限。不要使用主账号 AccessKey，也不要复用曾经出现在聊天、截图或 Git 历史中的密钥。

## 快速安装

从 [Releases](https://github.com/sym090701/aliyun-ecs-control/releases) 下载源码包和校验文件：

```bash
sha256sum -c aliyun-ecs-control-2.1.0.sha256
tar -xzf aliyun-ecs-control-2.1.0.tar.gz
cd aliyun-ecs-control-2.1.0
sudo ./install.sh
```

安装程序会：

1. 创建专用系统用户和 Python 虚拟环境；
2. 交互建立多账号、多实例配置；
3. 生成新的随机控制令牌；
4. 默认询问并启用 TOTP；
5. 安装并启动 Web 服务和每分钟定时器；
6. 让所有自动策略和定时计划初始保持暂停。

已有的 `/etc/aliyun-ecs-control.env` 和 `/etc/aliyun-ecs-control-targets.json` 会被保留。升级前，安装程序会把旧的 `/opt/aliyun-ecs-control` 复制到带时间戳的备份目录。

## 多账号配置

配置文件默认为 `/etc/aliyun-ecs-control-targets.json`，参考 [`aliyun-ecs-control-targets.example.json`](aliyun-ecs-control-targets.example.json)。

```json
{
  "accounts": [
    {
      "id": "intl",
      "name": "阿里云国际",
      "access_key_id": "REPLACE_WITH_NEW_KEY",
      "access_key_secret": "REPLACE_WITH_NEW_SECRET"
    }
  ],
  "instances": [
    {
      "id": "hk-1",
      "name": "香港主机",
      "account": "intl",
      "region": "cn-hongkong",
      "instance_id": "i-xxxxxxxxxxxxxxxxxxxx",
      "traffic_threshold_gb": 180,
      "auto_enabled_default": false,
      "schedule": {
        "enabled": false,
        "start": "09:15",
        "stop": "22:30",
        "weekdays": [0, 1, 2, 3, 4],
        "timezone": "Asia/Hong_Kong"
      }
    }
  ]
}
```

`accounts` 可以配置阿里云内地和国际账号；每个 `instances` 条目通过 `account` 引用对应凭证。安装后的配置权限必须保持为 `root:aliyun-ecs-control`、`0640`。

## 定时开关机

选择实例后，可在网页中独立配置：

- `start`、`stop`：24 小时制 `HH:MM`；
- `weekdays`：周一为 `0`，周日为 `6`；
- `timezone`：IANA 时区，如 `Asia/Shanghai`、`Asia/Hong_Kong`、`UTC`；
- `enabled`：是否执行计划。

systemd timer 每分钟检查一次。同一实例、日期、分钟和动作只处理一次；实例已经处于目标状态时不会重复提交云端请求。控制服务离线后不会补做已经过期的动作。

同一台实例不能同时启用定时计划和流量自动策略。手动启动、停止不受该限制。

## CDT 流量策略说明

`ListCdtInternetTraffic` 返回的是当前凭据可见范围内的 CDT 汇总数据，计费维度可能同时包含同地域的多个云产品。不要直接把这个数值当作某一台 ECS 的独立流量。

自动策略默认关闭。启用前必须先核对账号、地域、计费方式和 API 实际返回范围。CDT 查询失败时，程序不会执行自动策略，但网页仍允许手动控制实例。

## 公网部署

不要把应用的 `8787/tcp` 直接开放到公网。推荐配置：

1. `LISTEN_HOST=127.0.0.1`；
2. Caddy 或 Nginx 只向本机反代；
3. 公网仅开放 `443/tcp`；
4. 设置 `COOKIE_SECURE=true`；
5. 设置 `TRUST_PROXY=true`；
6. 配置 `TOTP_SECRET`；
7. 使用最小权限 RAM 用户；
8. 可在反向代理前增加 Cloudflare Access 或 VPN。

反向代理示例：

- [`proxy/Caddyfile.example`](proxy/Caddyfile.example)
- [`proxy/nginx.conf.example`](proxy/nginx.conf.example)

修改运行配置后重启：

```bash
sudo systemctl restart aliyun-ecs-control.service
```

## 命令行

```bash
source /etc/aliyun-ecs-control.env
BIN=/opt/aliyun-ecs-control/.venv/bin/aliyun-ecs-control

$BIN overview
$BIN status --target hk-1
$BIN start --target hk-1
$BIN stop --target hk-1
$BIN auto on --target hk-1
$BIN auto off --target hk-1
$BIN schedule show --target hk-1
$BIN schedule on --target hk-1 \
  --start 09:15 \
  --stop 22:30 \
  --weekdays 0,1,2,3,4 \
  --timezone Asia/Hong_Kong
$BIN schedule off --target hk-1
$BIN check
```

生成 TOTP 配置：

```bash
$BIN generate-totp --issuer "Aliyun ECS Fleet" --account admin
```

## 运维

```bash
systemctl status aliyun-ecs-control.service
systemctl status aliyun-ecs-policy.timer
journalctl -u aliyun-ecs-control.service -n 100 --no-pager
journalctl -u aliyun-ecs-policy.service -n 100 --no-pager
```

停止并禁用服务：

```bash
sudo systemctl disable --now \
  aliyun-ecs-control.service \
  aliyun-ecs-policy.timer
```

## 本地开发与测试

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

前端由 Python 标准库 HTTP 服务直接提供，不需要 Node.js 构建步骤。

## 安全提示

- 真实 AccessKey、控制令牌、TOTP 密钥和实例清单不得提交到 Git；
- GitHub 示例中的凭证全部是占位符；
- AccessKey 一旦出现在聊天、终端记录、截图或 Git 历史中，应立即轮换；
- 推荐给不同账号分别建立 RAM 用户，限制地域、实例和允许的 API；
- 定期检查 systemd 日志、阿里云 ActionTrail 和 RAM AccessKey 使用记录；
- 公网部署前先确认防火墙没有开放 `8787/tcp`。

## Release 文件

- `aliyun-ecs-control-2.1.0.tar.gz`：包含安装脚本、systemd 单元、反向代理示例和测试；
- `aliyun_ecs_control-2.1.0-py3-none-any.whl`：Python wheel；
- `aliyun-ecs-control-2.1.0.sha256`：上述两个文件的 SHA-256 校验值。

## 许可证

本仓库当前未声明开源许可证。代码公开可见不代表自动授予复制、修改或再分发权利；发布者后续可按实际需求补充许可证。
