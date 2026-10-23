> **历史专项设计／证据**：统一后的正式实现以 [项目进度](../项目进度.md)、[运维](../正式工程运维.md) 和 `contracts/api/v1` 为准。旧分线归属、未实现状态与精确图文预算前置已被当前工程方案取代；原用户需求和当时实测证据仍保留。

# NATAPP 首轮公网接入：Windows 11 部署手册

更新日期：2026-10-23  
状态：v0.2，主会话已对齐业务参数与图文上传要求；入口路线已确认，尚未开通或执行  
所属专项：[S03 公网接入](03-公网接入.md)

## 1. 已确定的范围

用户已选择 **NATAPP VIP-1 月付 Web 隧道＋平台二级域名＋HTTPS**，并确认目标主机为 Windows 11 x64、具备管理员权限；主机与首批约 5 位测试用户均在中国大陆。这些是用户提供的信息，尚未检查目标机器。

本手册负责公网转发和 NATAPP 连接器。业务 API、用户令牌、任务协议与模型服务分别按 [S04](04-服务端API与任务调度.md)、[S05](05-测试客户端与软件集成.md)、[S02](02-模型与推理部署.md) 实施。目前仓库没有业务服务或客户端代码，不能将以下命令理解为完整 AI 服务已经具备。

执行顺序：先核对第 2 节参数并使本机 API 可用，再开通付费资源、配置隧道、完成前台测试、安装 Windows 服务，最后验收外网和重启恢复。业务 API 尚未可用时，可以阅读和准备本手册，不以连接器安装成功替代业务实现。

按 [实施规划](../实施规划.md)，首轮可连接开发机的已认证 Fake API 独立验证网络，不必等真实模型；之后再换同 API 的真实 VL。迁移 5080 时按目标机更新连接器配置和服务，避免两台主机同时运行同一隧道令牌的实例。正文／上传验收新增图片，但不自动升级已选套餐。

## 2. 配置登记表：执行前先填写

| 项目 | 本手册采用的值 | 状态／填写规则 |
| --- | --- | --- |
| 目标操作系统 | Windows 11 x64 | 用户已确认；执行时记录实际版本 |
| 操作权限 | 管理员 PowerShell | 用户确认拥有权限；执行时核实是否已提升 |
| 入口套餐 | NATAPP VIP-1，Web 协议，1 个月 | 用户已确认路线；尚未下单 |
| 域名 | NATAPP 提供的二级域名 | 用户已确认类型；具体完整域名开通后填写 |
| 公网地址 | `https://<实际完整域名>`，无 `/api` 后缀、无尾部 `/` | 以后台和外网验证结果为准；示例域名不能直接使用 |
| 连接器目录 | `C:\AiServer\natapp` | 本手册建议默认，不代表已经创建；与仓库目录分开 |
| 连接器程序 | `C:\AiServer\natapp\natapp.exe` | 执行时从官网下载 Windows 64 位版本 |
| 配置文件 | `C:\AiServer\natapp\config.ini` | 只保存在服务器；含真实隧道令牌，不回填仓库 |
| 日志文件 | `C:\AiServer\natapp\logs\natapp.log` | 本手册建议默认；实施时验证写入权限 |
| Windows 服务名 | `AiServerNatapp` | 本手册建议默认；安装前检查重名，安装后核实实际服务 |
| 本地业务 API | `http://127.0.0.1:18080` | S04 v0.4 工程默认，文档已对齐；尚未实现，改端口时同时改后台与配置 |
| 健康路径 | `/api/v1/health` | 来自 S04 草案，尚无实现；以 S04 定案为准 |
| 客户端认证 | `Authorization: Bearer <业务令牌>` | 采用 S04 v0.4 草案；不是 NATAPP authtoken；health 免认证，ready／业务需认证 |
| 轮询／请求超时 | 活动任务约 3 秒轮询，查询 15 秒；提交单独建议 120 秒 | 与 S04／S05 文档对齐；平台实际接收行为待测，与模型执行超时分开 |
| 图文正文 | 总 JSON 5MiB、文本合计 128KiB、单图原始 3MiB，图片尺寸读取 S02 ImagePolicy | S04 草案本地限额；NATAPP 是否接受、上传耗时与实际流量必须测，不能据本表承诺 |
| 目标机器名称、部署者、实施日期 | 执行时填写 | 不假定本工作区所在电脑就是目标服务器 |
| NATAPP 实际版本、文件 SHA-256 | 下载后填写 | 官方下载页本次显示 3.0.5，实际以安装时版本为准；没有验证该二进制 |
| 隧道 ID、域名／隧道到期日、流量余量 | 开通后填写 | 可写非敏感信息；真实令牌、密码不记录在项目文档 |

本节为工程默认，未伪记为用户逐项确认。端口、健康、认证格式已在文档层对齐；P0 落实 schema／样例，执行时再登记真实域名、版本和上传结果。

## 3. 最终拓扑与凭据分工

```mermaid
flowchart LR
    A["外网测试客户端"] -->|"HTTPS ＋ 用户业务令牌"| B["NATAPP 二级域名入口"]
    B <-->|"连接器主动建立的隧道"| C["Windows 11：natapp 服务"]
    C -->|"HTTP 回环访问"| D["127.0.0.1：业务 API"]
    D --> E["队列与模型适配"]
    E --> F["内部推理引擎"]
```

公网 HTTPS 的 TLS 在 NATAPP 平台终止；连接器把请求送至同机回环 HTTP 服务，入口证书由平台提供。[HTTPS 官方说明](https://natapp.cn/article/https) 本方案不把业务 API 的 HTTP 端口映射到家庭路由器，不要求本机安装公网证书，也不发布模型端口。

| 凭据 | 用途 | 保存位置 |
| --- | --- | --- |
| NATAPP 账号密码 | 管理资源、续费、域名、隧道 | 资源管理者保管，不分发给测试用户 |
| 隧道 `authtoken` | 让服务器连接器绑定对应隧道 | 服务器本地 `config.ini`；不放入客户端或 Git |
| 用户业务令牌 | 标识用户、限制任务归属、撤销访问 | 服务端与对应用户客户端；由 S04 定义 |

能访问域名不代表有权提交任务。公网入口启用前，受保护业务接口必须具备用户认证；采用脱敏样例开展首轮验证。NATAPP 后台可选的额外访问控制与业务认证是两层配置，如启用额外层，须补充其 C# 请求方式，不能用浏览器登录成功代表 API 客户端已兼容。

## 4. 在目标主机做执行前检查

在目标 Windows 11 主机打开“以管理员身份运行”的 PowerShell。本文兼容 Windows PowerShell 5.1 的基础命令；不需要 Python、Docker 或额外反向代理。

```powershell
Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, OSArchitecture
$PSVersionTable.PSVersion
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
```

预期：Windows 11、64 位、PowerShell 5.1 或兼容版本，最后一行 `True`。不满足时先处理环境，不继续安装服务。

在每个新 PowerShell 窗口先设置下列变量。API 端口和健康路径按第 2 节对齐后的实际值填写：

```powershell
$NatappDir = 'C:\AiServer\natapp'
$NatappExe = Join-Path $NatappDir 'natapp.exe'
$ConfigPath = Join-Path $NatappDir 'config.ini'
$LogDir = Join-Path $NatappDir 'logs'
$LogPath = Join-Path $LogDir 'natapp.log'
$NatappService = 'AiServerNatapp'
$ApiPort = 18080
$ApiOrigin = "http://127.0.0.1:$ApiPort"
$HealthPath = '/api/v1/health'
```

确认业务 API 已监听并能直接响应：

```powershell
Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction SilentlyContinue |
    Select-Object LocalAddress, LocalPort, OwningProcess
$localResponse = Invoke-WebRequest -Uri "$ApiOrigin$HealthPath" -UseBasicParsing -TimeoutSec 15
$localResponse.StatusCode
$localResponse.Content
```

预期：按 S04 定案契约返回健康响应；首轮建议健康检查为 HTTP 200，且只含最小状态。监听地址应符合回环绑定设计；发现 `0.0.0.0` 或 `::` 时需由 API 实施者核对并收窄，不默认扩大监听。

如果健康检查实际要求认证，应按 S04 定案加请求头；401／403 仅能证明收到 HTTP 响应，不能记为健康检查通过。连接拒绝、404、500 等先在本机定位。尚无 API 时返回 S04 开发／模拟服务任务，不先用模型端口替代业务 API。

同时确认主机可正常访问 NATAPP 官方网站、DNS 正常，没有阻断连接器的出站策略。连接器实际出口目的地址和端口尚未在目标网络核实，不提前写死某个防火墙端口；通常先保留既有正常出站配置，有失败再按日志定位。

## 5. 注册和开通单月资源

这是人工网页操作，涉及账号认证和付款。本次仅编写步骤，没有代为注册、认证、充值或购买。

1. 打开 [NATAPP 官网](https://natapp.cn/)，注册并登录，按平台要求完成个人或企业认证；正式资源所有者自行完成相关信息提交。
2. 进入“购买隧道”，选择 **VIP-1、Web 协议、1 个月**。不要选择 VIP-3 TCP／UDP 套餐；本项目需要 HTTPS Web API。
3. 选择平台提供的二级域名，登记实际后缀、完整域名、费用和有效期。当前公开资料列二级域名 9～20 元／年，不代表隧道要买一年。
4. 支付前核对订单：VIP-1 月费公开价 9 元；月付档列 2Mbps、5GB／月，超额流量标价 0.3 元／GB。按上述域名费用首笔预计 18～29 元；最终订单金额、是否另有开通要求以账号页面为准。
5. 记录隧道和域名各自的到期日期、自动续费选项、余量查看位置。只有在业务 API 已准备好进入外网验证时才开始计费测试。
6. 在“我的隧道”找到本条隧道，取得其 `authtoken`，仅用于第 7 节服务器本地配置。

购买页面需要登录，本次没有核实账号内实际字段或订单。因此这里按官方功能和公开入口写步骤，不虚构下单按钮的精确坐标、域名下拉项或保证没有额外费用。执行者遇到页面差异时，在本手册原位置修正后继续。

来源：[套餐](https://natapp.cn/tunnel/buy)、[域名费用](https://natapp.cn/)、[新手流程](https://natapp.cn/article/natapp_newbie)、[注册／认证说明所在页面](https://natapp.cn/login)。

## 6. 配置平台 Web 隧道

在“我的隧道”中编辑本条隧道，按下表填写。界面标签可能随版本变化，按字段含义对应；保存实际配置记录，但不要保存令牌截图。

| 字段 | 填写内容 |
| --- | --- |
| 名称／备注 | `AiServer-研发测试`，方便识别 |
| 隧道协议 | Web／HTTP，供业务 API 使用 |
| 本地地址 | `127.0.0.1`，不要填公网域名或模型地址 |
| 本地端口 | 与 `$ApiPort` 一致，默认建议 `18080` |
| 域名 | 第 5 节已取得的平台完整二级域名 |
| 公网／域名 HTTPS | 开启；VIP-1 支持在“我的隧道”启用 |
| 本地 HTTPS | 关闭；本机业务 API 使用回环 HTTP |
| Host 重写 | 首轮保留默认；若业务 API 有 Host 允许名单，将实际公网域名纳入对应配置 |
| Web 调试／请求检查控制台 | 若页面提供开关，持续内测时关闭；避免持续保留脚本或认证请求内容 |

这里“本地端口”与“公网 HTTPS 的 443”是不同端口。不要把本地服务改成 443，也不要为了 HTTPS 给本机 HTTP 服务上传证书。平台域名 HTTPS 的说明见 [官方文档](https://natapp.cn/article/https)。

## 7. 下载 Windows 客户端并创建配置

先创建专用目录：

```powershell
New-Item -ItemType Directory -Path $NatappDir -Force | Out-Null
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
```

打开 [官方下载页](https://natapp.cn/download)，登录后下载 Windows **64 位**客户端，将解压后的 `natapp.exe` 放到 `$NatappExe` 对应位置。记录下载时显示的版本；本次官网显示 3.0.5，不表示已验证该文件。

```powershell
Test-Path -LiteralPath $NatappExe
Get-FileHash -LiteralPath $NatappExe -Algorithm SHA256
& $NatappExe -h
& $NatappExe -service help
```

预期：文件存在，能够显示帮助，包含配置文件和服务管理参数；把 SHA-256 写入实施记录，用于追踪实际文件。哈希记录本身不替代官方发布来源验证。若当前版本参数与本手册不同，按实际帮助更新后执行，不能静默换用不明旧版本。

用管理员记事本创建或编辑本地文件：

```powershell
notepad.exe $ConfigPath
```

填写下列完整内容，将 `REPLACE_WITH_TUNNEL_TOKEN` 替换为本条隧道真实令牌。已有配置时先核对，不覆盖其他隧道配置。保存为 `config.ini`，不要保存成 `config.ini.txt`。

```ini
[default]
authtoken=REPLACE_WITH_TUNNEL_TOKEN
log=C:\AiServer\natapp\logs\natapp.log
loglevel=INFO
http_proxy=
```

如第 2 节改过目录，配置中的绝对日志路径也必须同步。普通直接上网环境将 `http_proxy` 留空。生产代码／请求体不应成为本手册的日志示例；故障时截取最小脱敏信息，不公开整个配置。

在新建的专用连接器目录上限制普通账户读取令牌。下面配置适用于服务以 `LocalSystem` 运行且由管理员维护的首轮约定；安装后在第 9 节核实实际账户。若实际采用其他服务账户，先为该账户授予配置读取和日志写入权限，再启动，不直接照搬账户权限。

```powershell
icacls.exe $NatappDir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F'
```

预期：命令成功；目录由 SYSTEM 与本机管理员管理。此命令仅用于专用 `natapp` 目录，不对 `C:\AiServer` 或整个磁盘应用。配置项依据 [官方 config.ini 文档](https://natapp.cn/article/config_ini)，权限参数依据 [Microsoft icacls](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/icacls)；执行环境的命令帮助已核对，但没有执行权限变更。

## 8. 先在前台验证

确认没有其他实例使用同一个隧道令牌：

```powershell
Get-CimInstance Win32_Process -Filter "Name='natapp.exe'" |
    Select-Object ProcessId, ExecutablePath
Get-Service -Name $NatappService -ErrorAction SilentlyContinue
```

若已经有本项目服务在运行，转第 12 节维护，不再额外启动前台实例；若看到其他隧道进程，先辨认归属，不结束无关程序。

启动本条配置：

```powershell
Set-Location -LiteralPath $NatappDir
& $NatappExe "-config=$ConfigPath"
```

预期：显示在线状态，转发地址对应实际域名。官方将 `Tunnel Status Online` 作为连接成功标识；`Avg Conn Time` 不能作为实际公网延迟。[状态说明](https://natapp.cn/article/natapp_newbie)

保持本窗口运行，在第二个 PowerShell 窗口查看日志：

```powershell
Get-Content -LiteralPath 'C:\AiServer\natapp\logs\natapp.log' -Tail 50
```

再用手机热点连接的另一台电脑或真实测试用户电脑做外网检查。不要只在服务器或同一局域网访问域名就记为跨网验收通过。

在外网电脑设置真实地址并执行：

```powershell
$PublicOrigin = (Read-Host '粘贴实际 HTTPS 公网地址，不带 /api 后缀').Trim().TrimEnd('/')
if (([uri]$PublicOrigin).Scheme -ne 'https') { throw '请输入实际 HTTPS 地址' }
$HealthPath = '/api/v1/health'
$publicResponse = Invoke-WebRequest -Uri "$PublicOrigin$HealthPath" -UseBasicParsing -TimeoutSec 15
$publicResponse.StatusCode
$publicResponse.Content
```

健康路径和是否需请求头按 S04 定案调整。预期是与本机一致的健康响应、证书校验正常；不得忽略证书错误来记为通过。这只验证入口与健康接口，不代表任务、模型或用户隔离已经通过。

## 9. 安装后台服务、启动与故障恢复

前台外网验证通过后，在前台窗口按 `Ctrl+C` 结束连接器。确认该前台实例已退出，再在管理员 PowerShell 中重新设置第 4 节变量。

执行安装前检查，防止覆盖同名服务：

```powershell
if (Get-Service -Name $NatappService -ErrorAction SilentlyContinue) {
    throw '同名服务已存在，请先按维护章节核实归属，不重复安装'
}
& $NatappExe "-config=$ConfigPath" -service install "-servicename=$NatappService"
if ($LASTEXITCODE -ne 0) { throw 'NATAPP 服务安装失败，检查帮助和错误输出' }
Get-CimInstance Win32_Service -Filter "Name='$NatappService'" |
    Select-Object Name, StartName, PathName, StartMode, State
```

预期：服务存在，程序与配置路径指向本项目专用目录。核实 `StartName`；若为 `LocalSystem`，与第 7 节权限约定匹配；若不同，核对该实际账户的配置／日志权限。路径与账户不符合时先修正，不继续宣布服务可用。

设置开机启动、进程退出后的重启动作，并启动：

```powershell
Set-Service -Name $NatappService -StartupType Automatic
sc.exe failure $NatappService reset= 86400 actions= restart/5000/restart/15000/restart/60000
if ($LASTEXITCODE -ne 0) { throw '服务故障恢复配置失败' }
if ((Get-Service -Name $NatappService).Status -ne 'Running') {
    Start-Service -Name $NatappService
}
Get-Service -Name $NatappService | Select-Object Name, Status, StartType
sc.exe qfailure $NatappService
Get-Content -LiteralPath $LogPath -Tail 50
```

预期：服务为 `Running`、启动类型为 `Automatic`；故障动作显示依次约 5 秒、15 秒、60 秒后重启。`sc.exe` 参数名后面的空格保留；PowerShell 中不要简写为 `sc`。

NATAPP 安装／服务参数依据 [官方服务说明](https://natapp.cn/article/system_service)；启动类型依据 [Microsoft Set-Service](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.management/set-service?view=powershell-5.1)，恢复动作依据 [Microsoft SC failure](https://learn.microsoft.com/en-us/previous-versions/windows/it-pro/windows-server-2012-r2-and-2012/cc742019%28v%3Dws.11%29)。

运行状态、进程崩溃恢复、隧道连通、业务 API 健康是不同检查。上述故障动作处理进程退出，不自动发现 API 卡死或业务错误。不要为了验证恢复而杀死其他 NATAPP 实例；首轮用停启、网络中断和主机重启验收。

## 10. 客户端接入与接口验收

测试用户只需要：本人的业务令牌、真实 HTTPS `BaseUrl`、实现后的业务客户端。不需要 NATAPP 软件或隧道令牌。客户端按 S05 配置完整入口根地址，不拼接 `127.0.0.1` 或连接器管理地址。

认证头和路径采用第 2 节／S04 v0.4，实际 schema 和 Demo 样例在 P0～P1B 生成后引用，不在手册复制另一套 JSON。接口尚未实现，以下验证语义必须保留：

| 检查 | 预期结果 |
| --- | --- |
| 健康检查 | 符合定案契约；API 存活与模型就绪区分 |
| 受保护业务接口不带／错误令牌 | 被业务认证拒绝，按协议返回 401／403，不执行生成 |
| 合法用户提交任务 | 返回任务标识；按当前草案为 202，提交响应不等待整个推理完成 |
| 查询任务和取回结果 | 能显示排队／运行／结束状态；任务完成可取回文本 |
| 取消任务 | 区分排队取消、运行取消；实际结果按 S04 定案验收 |
| 用户 A 查询／取消用户 B 任务 | 被拒绝，不泄露结果 |
| 5 位用户同时使用 | 验证排队、轮询、错误表现和连接额度；记录实际流量 |
| 请求超时后重试 | 按幂等协议查回原任务，不重复创建生成任务 |
| 小图与接近上限的图文提交 | 实测正文上限、完整上传、120 秒默认是否适用及流量；超限不能丢图后继续文字任务 |

轮询期间复用 HTTP 客户端连接，仅在有任务时轮询，失败后退避。VIP-1 月付公开限制为每分钟 90 个新 TCP 连接，并非每分钟 90 个 HTTP 请求。[额度说明](https://natapp.cn/tunnel/buy) 若客户端每次轮询都新建连接，须先改调用层或调整间隔，再考虑升级套餐。

正式测试仅用 HTTPS 地址。域名 HTTPS 开启不代表 HTTP 地址自动停用；若要求阻断 HTTP，在平台能力与 API 可信代理行为核实后补充实施，当前不宣称已强制 HTTPS 或配置 HSTS。

## 11. 重连、重启与完成标准

在没有重要运行任务的测试窗口实施，不对未保存的工作直接重启机器。重启由实施者在目标主机操作，本轮文档编写没有执行。

| 编号 | 测试步骤 | 通过依据 | 当前结果 |
| --- | --- | --- | --- |
| N01 | 本机检查 API 与健康接口 | 监听、地址和健康响应符合约定 | 未执行 |
| N02 | 前台启动连接器 | 状态在线、域名正确、日志可写 | 未执行 |
| N03 | 真实外网调用 HTTPS | 证书正常，健康结果与本机一致 | 未执行 |
| N04 | 第 10 节业务接口测试 | 认证、归属、任务状态符合 S04 | 未执行；依赖 API 实现 |
| N05 | 停止、启动本条连接器服务 | 外网访问中断后恢复；不重复创建原任务 | 未执行 |
| N06 | 短时断开服务器网络再恢复 | 记录连接器恢复过程和恢复耗时，外网重新可达 | 未执行 |
| N07 | 保存工作后重启 Windows | 登录前服务已自动启动；网络和 API 就绪后外网恢复 | 未执行 |
| N08 | 5 位用户同时提交与查询 | 额度内完成排队、查询和结果获取，记录错误率与流量 | 未执行 |

N05 的服务停启命令见第 12 节。N06／N07 必须同时检查 API 和模型自己的启动方式；只给 NATAPP 设置自启不会让业务 API 与模型自动启动。API 是否恢复运行中任务按 S04 约定，不由隧道负责。

完成公网层至少需要 N01、N02、N03、N05、N06、N07 通过；完整业务联调还需 N04、N08 和模型专项验证。记录 HTTP 调用、排队、模型生成各自耗时，不能把入口带宽当作推理速度。

## 12. 日常查看、修改、停用与卸载

以下命令都在目标服务器运行。每个新管理员 PowerShell 先加载第 4 节变量，并确认操作对象是本项目连接器。

查看状态和近期日志：

```powershell
Get-Service -Name $NatappService | Select-Object Name, Status, StartType
Get-CimInstance Win32_Service -Filter "Name='$NatappService'" |
    Select-Object Name, StartName, PathName, StartMode, State
Get-Content -LiteralPath $LogPath -Tail 50
```

停止、启动或重启本项目服务，按当前目的只选择一条执行：

```powershell
Stop-Service -Name $NatappService
```

```powershell
Start-Service -Name $NatappService
```

```powershell
Restart-Service -Name $NatappService
```

修改配置时先停止服务，再用管理员记事本编辑。修改本地配置内容后启动并复验；修改服务安装参数（如配置文件路径）时，官方要求卸载再重新安装，按第 9 节重新设置并验证自启／故障恢复。平台修改域名、端口或 HTTPS 后重启本条连接器，并同步客户端与第 2 节登记表。

首次运行后观察日志增长，每周检查；首轮建议保留约 7 天脱敏排障记录。当前未核实连接器是否自动轮转，不宣称已实现自动清理；日志持续增长时在测试窗口停服务、归档旧日志再启动，记录实际策略。

若停止首月测试：在平台停止后续续费安排，分别检查域名和隧道的到期设置；停止并卸载本项目服务，按需要在平台停用／撤销隧道凭据。

卸载前确认实际服务路径属于专用目录：

```powershell
$serviceInfo = Get-CimInstance Win32_Service -Filter "Name='$NatappService'"
if ($null -eq $serviceInfo) { throw '服务不存在，先核对实际服务名' }
if ($serviceInfo.PathName -notlike "*$NatappExe*") { throw '服务路径不匹配，请核实归属' }
Stop-Service -Name $NatappService -ErrorAction Stop
& $NatappExe "-config=$ConfigPath" -service uninstall "-servicename=$NatappService"
if ($LASTEXITCODE -ne 0) { throw '服务卸载失败，请检查错误输出' }
Get-Service -Name $NatappService -ErrorAction SilentlyContinue
```

预期：本项目服务不再存在。若服务已停止，先核对状态，可跳过重复停止操作。默认保留本地目录和脱敏排障记录，不递归删除 `C:\AiServer`，也不停止模型服务或其他项目隧道。域名和订阅的停用需要在平台分别核实，不因卸载程序就视为订阅已取消。

## 13. 常见故障定位

| 现象 | 先检查什么 | 处理与复验 |
| --- | --- | --- |
| 本机连接拒绝 | API 是否启动、地址／端口是否一致 | 先恢复本机健康接口，再查公网 |
| 连接器提示认证错误，包含 `errorCode:200` | 是否填了正确隧道 `authtoken`、配置是否真正加载 | 这是连接器错误码，不是业务 HTTP 200；只在本机核对令牌，修正配置后重启 |
| `Tunnel not found` | 连接器在线状态、域名拼写、隧道是否到期 | 核对平台状态和本地日志；与业务 API 的普通 404 分开判断 |
| HTTP 可访问，HTTPS 不可访问 | 平台是否启用域名 HTTPS、证书与域名是否对应 | 保存 HTTPS 设置并重启连接器后用真实 HTTPS 地址复验，不忽略证书错误 |
| 无法连接 `127.0.0.1:<端口>` | 平台本地端口与 API 实际端口 | 同时修正登记表、平台配置和 API 绑定 |
| `Reconnecting` 反复出现 | 另一前台／服务实例占用同一令牌、出站网络是否异常 | 识别并停止本项目重复实例；不关闭系统防火墙或结束无关实例 |
| HTTP 401／403 | 业务令牌、任务归属，或另行启用的平台访问控制 | 根据 API／平台各自日志判断；不把连接器令牌发给用户 |
| HTTP 404／5xx | 是否访问正确路径、请求是否进入 API、应用错误日志 | 核对业务路由与 Host 限制，同时排查平台转发；不能只凭状态码断定模型出错 |
| 服务安装／启动失败 | 管理员权限、同名服务、程序和配置路径、服务账户与文件权限 | 查看原始错误、`Get-CimInstance Win32_Service` 与脱敏日志；按实际帮助核对参数 |
| 服务 Running，但公网不可用 | 隧道在线、日志、API 健康、网络和套餐余量 | SCM 状态不等于网络健康，分层恢复 |
| 429、连接断开或流量不足提示 | 平台连接额度、余量，客户端是否频繁建连接 | 复用连接、退避；确认真实需求后补流量或调整套餐 |
| 健康检查通过但生成失败／很慢 | API、队列、模型就绪和各阶段计时 | 分别交给 S04、S02；不要用换更大公网带宽替代模型排查 |

来源：[NATAPP 错误汇总](https://natapp.cn/article/errors)、[网络问题排查](https://natapp.cn/article/networkerrors)。网页可能给出修改系统 DNS 等通用建议，本项目先收集实际错误再针对处理，不把全局网络修改作为默认安装步骤。

## 14. 实施记录与剩余依赖

| 类别 | 本次状态 |
| --- | --- |
| 用户选型与目标系统 | NATAPP VIP-1 月付＋平台二级域名＋HTTPS；Windows 11 x64、管理员权限；用户已确认 |
| 官方功能核实 | 已查阅套餐、域名、Windows 下载、配置、HTTPS、服务和错误文档 |
| 本文 PowerShell 命令 | 18 段命令在本地 Windows PowerShell 5.1.22621.4249 和 PowerShell 7.6.5 解析均无语法错误；本手册和 S03 的本地文件链接检查通过；没有运行安装、联网测试、权限或服务修改 |
| 账号订单／实际域名／令牌 | 未开通或取得；执行时本机填写，令牌不写进仓库 |
| API 端口／健康路径／认证头 | 已在文档层与 S04 v0.4 对齐；schema／实际服务待实现 |
| 图文上传 | 已增加正文与提交超时要求；平台实际限制、耗时与流量未验证 |
| 本机与外网验证、主机重启 | 全部未执行，结果按第 11 节登记 |

执行者完成后在本节记录实际版本、文件哈希、非敏感资源标识、日期与 N01～N08 结果，并把证据摘要回写 S03。日志涉及令牌或脚本正文时，先脱敏再作为证据引用。此手册用于实施，不提前把命令示例记为运行成功。
