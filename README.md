# Aliyun ECS Fleet Control

一个可自行部署的阿里云 ECS 控制台，用同一套网页和命令行管理多个阿里云账号、地域及实例。支持手动开关机、按实例定时开关机，以及基于 CDT 汇总流量的自动策略。

> [!IMPORTANT]
> 这是云控制面工具，会持有可启动和停止 ECS 的 AccessKey。公网部署必须使用 HTTPS、TOTP、最小权限 RAM 用户，并保持应用只监听本机地址。

## 功能

- 多账号、多地域、多实例统一管理；
- 网页添加、更新和删除账号与实例，敏感变更需要 TOTP 复验；
- 网页查看实例状态、账号、地域和 CDT 汇总流量；
- 手动启动和正常停止实例，停机操作需要二次确认；
- 每台实例按星期逐日设置独立的启动时间、停止时间和 IANA 时区，并可批量调整所选日期的时间；
- 每台实例可在主页面手动调整流量阈值，并单独启停自动策略；
- 定时计划与流量策略可独立使用或联合运行，策略记录按来源和结果筛选；每台实例保留最近五天记录；
- Passkey 登录（WebAuthn）、多设备 Passkey 管理和 TOTP 恢复登录；
- 六位 TOTP 登录、随机会话 Cookie、登录限速和同源检查；
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
  +---- /etc/aliyun-ecs-control.key
  +---- /var/lib/aliyun-ecs-control/config.json
```

Web 服务和自动化任务都以专用的 `aliyun-ecs-control` 用户运行。systemd 单元启用了 `NoNewPrivileges`、`ProtectSystem=strict`、`PrivateDevices` 等沙箱选项。

## 环境要求

- Debian 或 Ubuntu，使用 systemd；
- Python 3.10 或更高版本；
- `python3-venv`；
- 公网使用时需要 Caddy 或 Nginx 与有效 TLS 证书；
- 一个或多个阿里云 RAM 用户的 AccessKey。

RAM 用户应只授予目标实例所需的查询、启动和停止权限。不要使用主账号 AccessKey，也不要复用曾经出现在聊天、截图或 Git 历史中的密钥。

## 快速安装

从 [Releases](https://github.com/sym090701/aliyun-ecs-control/releases) 下载源码包和校验文件：

```bash
sha256sum -c aliyun-ecs-control-2.2.1.sha256
tar -xzf aliyun-ecs-control-2.2.1.tar.gz
cd aliyun-ecs-control-2.2.1
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

## 网页管理账号与实例

登录后打开“管理”，可以添加多个阿里云内地或国际账号，并把不同地域的 ECS 实例绑定到对应账号。添加、替换、删除和凭据测试均要求再次输入当前 TOTP。

AccessKey Secret 只在提交时进入服务端，网页和读取 API 不会返回原值。托管配置默认加密写入 `/var/lib/aliyun-ecs-control/config.json`，权限为 `aliyun-ecs-control:aliyun-ecs-control 0600`；Fernet 主密钥保存在 `/etc/aliyun-ecs-control.key`，权限为 `root:aliyun-ecs-control 0640`。加密主要降低配置文件或备份被单独泄露的风险，不能替代最小权限 RAM 策略。

`/etc/aliyun-ecs-control-targets.json` 是安装和升级时的引导配置。首次启动 2.2.0 或更高版本时，程序会读取它并写入加密托管配置，原文件不会被网页覆盖。后续网页变更以托管配置为准。

## 引导配置格式

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

`accounts` 可以配置阿里云内地和国际账号；每个 `instances` 条目通过 `account` 引用对应凭证。迁移后的引导配置及其备份权限为 `root:root 0600`。

## 定时开关机

选择实例后，可按周一至周日逐日独立配置：

- 每一天的 `start`、`stop`：24 小时制 `HH:MM`；未勾选的日期不会执行；
- `timezone`：网页提供 `UTC-12:00` 至 `UTC+14:00` 的固定偏移下拉框（含常见半小时和 45 分钟偏移）；旧的 IANA 时区配置仍兼容；
- `enabled`：是否执行计划。

systemd timer 每分钟检查一次。同一实例、日期、分钟和动作只处理一次；实例已经处于目标状态时不会重复提交云端请求。控制服务离线后不会补做已经过期的动作。

每一行代表运行时段的启动日。跨午夜计划如周一 `22:00-08:00` 会在周一晚启动，并在周二早上停止。保存已启用的计划时至少需要勾选一天；保存为暂停状态时可以暂时取消所有日期。

网页计划表中的“选择”仅用于批量修改时间，不会改变该日是否启用；“启用”列才决定该日是否执行。可一键选择全部、工作日或周末，批量填入开机和关机时间后，仍需点击“保存计划”才会写入服务器。

定时计划与流量守护同时启用时，定时计划定义允许运行窗口：窗口外保持停止；窗口内流量低于阈值时保持运行，达到阈值时正常停止。窗口外不会因为低流量重新启动。流量查询失败时保持当前电源状态，不提交启动或停止请求。记录会分别标记为“流量守护”或“开关机计划”，不再使用“联合策略”分类；网页可按来源和成功、未执行、失败筛选并分页浏览五天历史。自动化启用期间，手动操作可能在下一分钟被策略纠正。

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
6. 配置 `TOTP_SECRET`；网页登录仅接受当前六位动态验证码；
7. 保持 `/etc/aliyun-ecs-control.key` 仅对 root 和服务组可读；
8. Passkey 凭据保存在加密的 `PASSKEY_PATH` 文件中；
9. 使用最小权限 RAM 用户；
10. 可在反向代理前增加 Cloudflare Access 或 VPN。

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

- `aliyun-ecs-control-2.2.1.tar.gz`：包含安装脚本、systemd 单元、反向代理示例和测试；
- `aliyun_ecs_control-2.2.1-py3-none-any.whl`：Python wheel；
- `aliyun-ecs-control-2.2.1.sha256`：上述两个文件的 SHA-256 校验值。

## 许可证

本仓库当前未声明开源许可证。代码公开可见不代表自动授予复制、修改或再分发权利；发布者后续可按实际需求补充许可证。
