# 东南大学校园网：登录与负载均衡脚本

本目录说明仓库中公开的脚本怎样对应现场方案。它们是路由器现有 `seu-balancer` 的整理版：保留两条上行、2:1 连接分配、认证与健康检测逻辑，补上单账号配置检查、显式失败处理和清理入口，并把现场自动重启射频的行为改为默认关闭。

**发布版只做了离线验证，尚未替换到正在使用的路由器。** 脚本用于东南大学校园网的 Portal 认证与多上行管理；无线接口与 LAN 配置按主 README 先行准备。

## 文件对应关系

| 文件 | 用途 |
|---|---|
| [`seu-balancer.py`](../scripts/seu-balancer.py) | 两条校园上行的自动认证、探测、策略路由与 IPv4 按连接负载均衡 |
| [`seu-login.py`](../scripts/seu-login.py) | 复用上面脚本的登录逻辑，手动检查某一条上行，不启动长期循环 |
| [`seu-balancer.init`](../scripts/seu-balancer.init) | OpenWrt procd 服务示例 |
| [`cleanup-seu-balancer.sh`](../scripts/cleanup-seu-balancer.sh) | 停用服务，清理预留路由与 nftables 表，并恢复本版本记录的 `rp_filter` |
| [`accounts.example.json`](../config/accounts.example.json) | 账号文件格式；只有占位内容 |

## 适用范围与前提

- OpenWrt、Python 3.9 以上、`ubus`、`ip-full`、nftables、curl 和 CA 证书。本文现场环境为 OpenWrt 25.12.5。
- 已经创建命名为 `wan1`、`wan2` 的上行接口，并配置好无线 STA、DHCP、LAN→WAN 转发和出口 masquerade。脚本不替你创建这些配置。
- 两条上行必须实际可关联东大校园 AP、可获得地址；账号具有相应使用授权，支持所需的同时接入。
- 本版本认证逻辑针对东南大学当前使用的 Portal 页面标记与参数，认证入口是 `w.seu.edu.cn`。校园认证系统变化时，登录函数需要更新。
- Portal 主机 `w.seu.edu.cn`、地址 `10.80.128.2`、HTTPS 443／802 与探测服务器地址来自现场实现，并集中体现在 `curl()`、`authenticate()` 等函数中。它们不是你的设备地址或账号信息，但也不是永久不变的协议约定。
- 仅处理 IPv4。IPv6、透明代理的本机出站、其他网络命名空间不属于已经实现的均衡范围。

依赖安装示例，适用于使用 apk 的 OpenWrt；其他版本先核对包名与包管理器：

```sh
apk update
apk add python3 curl ca-bundle ip-full nftables
```

不要仅为安装这些脚本升级系统或复制不匹配当前内核的模块。

## 必须先确认的预留资源

脚本会修改以下资源，请先确保它们没有被其他策略路由管理器占用：

| 资源 | 本版本使用范围 |
|---|---|
| IPv4 路由表 | 201、202、203、204；203 为保留槽位 |
| IPv4 规则优先级 | 101–103、201–203、300 |
| 连接／数据包标记 | 线路标记为 `0x101`、`0x102`；路由匹配读取低 12 位（`0xfff`），但当前赋值会覆盖完整标记 |
| nftables 表 | `inet seu_lb` |
| 反向路径过滤 | `all` 与上行接口的 IPv4 `rp_filter` |

```sh
ip -4 rule show
ip -4 route show table all
nft list ruleset
```

`mwan3`、代理和其他策略路由服务可能使用相同规则或标记，不能不检查就同时运行。虽然路由匹配只读取低 12 位，本实现的 `ct mark set` 和 `meta mark set` 会覆盖完整 32 位标记，**不保留其他服务写入的高位**；若其他服务依赖这些标记，需要联合适配，不能仅划分低位与高位就认定兼容。

清理入口按上述预留范围删除策略，**不会判断其中是否混入了其他服务的规则**，因此资源独占是使用前提。启用或调整均衡也可能使原有连接需要重新建立。

## 安装文件，不自动启动

下面在保存仓库文件的电脑上执行，`ROUTER_IP` 替换为管理地址；scp 使用 `-O` 兼容常见的 Dropbear 环境。先备份已有同名脚本与配置，避免覆盖现有服务。

```sh
scp -O scripts/seu-balancer.py root@ROUTER_IP:/usr/sbin/seu-balancer
scp -O scripts/seu-login.py root@ROUTER_IP:/usr/sbin/seu-login
scp -O scripts/seu-balancer.init root@ROUTER_IP:/etc/init.d/seu-balancer
scp -O scripts/cleanup-seu-balancer.sh root@ROUTER_IP:/usr/sbin/cleanup-seu-balancer
scp -O config/accounts.example.json root@ROUTER_IP:/tmp/accounts.example.json
```

然后登录路由器：

```sh
mkdir -p /etc/seu-balancer
chmod 700 /etc/seu-balancer
# 新安装时执行；已有 accounts.json 时不要覆盖它。
cp /tmp/accounts.example.json /etc/seu-balancer/accounts.json
chmod 600 /etc/seu-balancer/accounts.json
chmod 755 /usr/sbin/seu-balancer /usr/sbin/seu-login /usr/sbin/cleanup-seu-balancer
chmod 755 /etc/init.d/seu-balancer
```

在路由器本地编辑 `/etc/seu-balancer/accounts.json`，将占位符替换为真实账号。只用一个账号时删除另一条记录，保留合法 JSON 数组。脚本要求凭据文件不允许组或其他用户读取；不要把真实文件、调试日志或抓包结果提交到 GitHub。

## 先修改 LAN 参数

服务文件上方的示例值是：

```sh
LAN_CIDR="192.168.8.0/24"
LAN_DEVICE="br-lan"
REPAIR_RADIO=""
```

**必须改成自己的实际 LAN 网段与设备名。** 不匹配可能导致策略路由失效或影响管理连接。示例值不是自动检测结果。

默认不自动重启射频。只有明确知道某个射频专用于 `wan2` STA、重启不会影响本地 AP 时，才考虑设置 `REPAIR_RADIO`；不确定就保持空值。

手动命令不读取服务文件中的变量，要使用同样的环境设置。例如下面的网段仅为示例：

```sh
export SEU_LAN_CIDR="192.168.8.0/24"
export SEU_LAN_DEVICE="br-lan"
```

可选的 `SEU_ACCOUNTS_FILE` 可以指定另一个本地凭据文件路径，权限要求相同。

## 单独登录一条上行

```sh
python3 /usr/sbin/seu-login --line wan1
python3 /usr/sbin/seu-login --line wan2
```

默认行为是发现接口、读取凭据、检查到 Portal 的有效源地址路由，再发起认证并探测互联网。仅有 `curl --interface IP` 不能保证多上行路由正确，所以入口会检查 `ip -j -4 route get … from …` 的实际出接口；不匹配时终止，而不是把账号发到另一条上行。

如果还没建立源地址路由，在完成预留资源检查后，可显式创建本包的路由策略：

```sh
python3 /usr/sbin/seu-login --line wan1 --prepare-routes
```

`--prepare-routes` 会修改前述路由表、规则和 `rp_filter`，不是纯只读检查；不添加均衡 nftables 表，也不启动守护服务。测试完成要使用清理入口撤回策略。

输出只包含线路名、归类后的认证状态与可达性布尔值，不输出用户名、密码、登录 URL 或服务端原始回复。`not-needed-or-unreachable` 本身不代表登录成功，仍以 `internet_reachable=True` 为独立验证结果；返回码 0 表示可达、1 表示当前不可达、2 表示配置或路由检查等错误。

## 启动长期认证与负载均衡

在确认 LAN 参数、账号与两条上行正确后，先启动但不设置开机运行：

```sh
/etc/init.d/seu-balancer start
cat /tmp/seu-balancer-state.json
ip -4 rule show
nft list table inet seu_lb
logread -e seu-balancer
```

连续两次 HTTPS 探测成功后，线路进入候选；三次失败或接口消失后移除。正常情况下约 30 秒一轮，两条线路都可用时按 2:1 选择新连接。账号认证失败时有重试间隔，不保证旧连接无中断，也不会把一个 TCP 流拆到两个出口。

确认各出口独立可达、计数正常、故障恢复符合预期后，再设置开机运行：

```sh
/etc/init.d/seu-balancer enable
```

状态文件只记录探测结果与接口信息，不记录凭据，但含运行时网络地址，也不建议原样公开。

## 停用与回滚

**只停止服务不会清理已经添加的策略路由。** 正常撤回使用：

```sh
/usr/sbin/cleanup-seu-balancer
```

该入口停用开机启动并停止服务，再删除 `inet seu_lb`、上述预留规则／路由表，恢复本发布版在 `/var/run/seu-balancer/rp-filter.json` 记录的反向路径过滤值，移除状态文件。

现场旧版本没有记录原始 `rp_filter`，这份清理入口无法恢复它没有记录的历史值。恢复旧版本时应采用之前备份的脚本与配置，并按需要重启路由器，让 OpenWrt 按系统配置重新建立网络。清理脚本也不会还原你自己改过的无线、NAT、OpenClash 或 LAN 配置。

清理后检查 `ip -4 rule show` 和 `nft list tables`，确认保留的常规默认路由能够上网。若你之前依赖的仅是本包策略、原始默认路由已失效，需要先恢复原网络配置；清理入口不会凭空创建一个可用出口。

## 发布版与现场版的区别

- LAN 网段与接口名称集中到环境参数；示例 LAN 使用 `192.168.8.0/24`，部署者必须核对。
- 不再假设账号文件一定包含 `wan2`，拒绝重复或未知线路、空字段和未替换的占位符。
- 自动重启 STA 射频改成显式 opt-in，默认不重启无线。
- 添加路由与规则时检查失败；仅清理不存在的旧策略允许容错。
- nftables 均衡入口明确跳过非 IPv4 流量。
- 保存并提供 `rp_filter` 恢复入口，补上单独登录入口的有效出接口检查。

仓库测试通过 mock 覆盖这些边界，不会访问校园认证服务或修改测试电脑的网络。运行：

```sh
python3 -m unittest discover -s tests -v
```
