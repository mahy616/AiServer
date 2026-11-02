# AiServer API与OpenCode接入

原生HTTP API路径为`/api/v1`，OpenAI兼容入口为`/v1`。对应服务端版本：0.10.2；更新日期：2026-11-02。OpenCode接入见第10节；第2至9节记录服务端保留的原生接口与认证、任务语义。

示例地址和模型名称用于说明；实际服务地址、个人API key及模型由部署方提供。本文不承诺部署当前在线，连接时按实际响应验证。

0.10.0允许服务启动时选择Ollama或Strata，公共接口和认证不变，客户端以models返回的能力为准。Strata初版模型为`qwen3.8-flash-next:iq3_xxs`，仅文字与工具，普通`think=false`／思考`think=true`映射上游none／high；默认总窗口131072、最大输出16384、预留512，思考与正文共用输出预算。容量标为`conservative-estimate-v1`，超限拒绝且不静默截断。手工样例见[Strata OpenCode配置](../../examples/opencode.strata.example.json)和[请求](../../examples/openai-strata-request.json)；部署步骤见运维文档。

## 1. OpenCode与服务端职责

客户端统一使用OpenCode。OpenCode在本机组织需求、项目文件、历史和工具结果，通过AiServer的`/v1/chat/completions`访问模型；模型返回工具调用后，由本机OpenCode执行文件读取、编辑或命令，再回传结果。5080上的AiServer负责鉴权、模型能力、排队、推理、停止、结果和监控，不运行OpenCode Agent或读取客户端磁盘。

本机启动、密钥设置和验证步骤维护在[运维文档](../../../docs/正式工程运维.md#本机opencode执行端)。本机入口使用独立API key，自动从模型清单刷新已注册模型ID及容量；本文件保留通用provider配置和完整线协议样例。原生接口继续供连接检查、管理工具与服务兼容使用，不再交付独立桌面客户端或C#通信实现。

## 2. 连接约定与端点总表

运行配置至少包含：

| 参数 | 来源 | 示例 |
|---|---|---|
| baseUrl | 服务提供方，允许用户配置 | https://szzj.nat200.top |
| apiKey | 5080上的AiServer管理员签发 | 完整的`ais_…`个人密钥；客户端API key输入框不加`Bearer `前缀 |
| model | 从GET models读取实际ID | qwen3.5:9b-q4_K_M |

baseUrl不包含/api/v1，拼接下面路径。外网只使用HTTPS；本机回环调试可用http://127.0.0.1:18080。OpenCode原生HTTP请求不需要浏览器CORS配置。

- JSON使用UTF-8，属性名大小写严格按本文。请求不允许未知或重复字段；响应解析应忽略未来增加的非必需字段。
- POST JSON设置Content-Type: application/json；提交推理再设置Accept: text/event-stream。
- 只有GET health免认证，其他接口设置Authorization: Bearer <API key>。
- 不把凭据放URL，不跟随重定向转发凭据，不记录密码、访问令牌、原始请求／回答或图片正文。
- 时间为UTC ISO 8601字符串，可含毫秒，例如2026-10-24T02:00:00.123Z；允许null的字段明确列出。
- 路径中的jobId和clientRequestId均为UUID字符串。三种编号不能混用：请求编号clientRequestId由客户端创建；任务编号jobId由服务端返回；traceId用于请求排障。
- 原生/api/v1协议与OpenAI兼容/v1协议分开；OpenCode使用第10节的/v1/chat/completions。没有Responses API、会话同步、文件上传URL、服务端代码执行、模型下载或公网账号管理接口。

| 方法 | 路径 | 成功HTTP及用途 |
|---|---|---|
| GET | /api/v1/health | 200，小型存活响应，免认证 |
| GET | /api/v1/auth/me | 200，核实当前凭据的身份与到期 |
| GET | /api/v1/models | 200，当前模型和输入／输出限制 |
| GET | /api/v1/ready | 200可接收；503暂不能接收，见第5节 |
| POST | /api/v1/jobs | 200并保持SSE连接，排队和生成都通过该连接返回 |
| GET | /api/v1/jobs/by-request/{clientRequestId} | 200，按原请求编号找任务 |
| GET | /api/v1/jobs/{jobId} | 200，查询任务状态 |
| GET | /api/v1/jobs/{jobId}/result | 200，读取终态结果；未结束409，已到期410 |
| POST | /api/v1/jobs/{jobId}/cancel | 200排队／终态；202执行／取消中 |
| GET | /api/v1/jobs?limit=20&beforeSequence=100 | 200，本人任务分页列表 |
| GET | /v1/models | 200，OpenAI格式模型列表，同样需要API key |
| POST | /v1/chat/completions | 200，OpenAI兼容普通JSON或SSE，见第10节 |

所有接口只允许访问当前认证用户的任务。访问不存在或别人的任务统一404 JOB_NOT_FOUND。

## 3. 账号与API key认证

### 3.1 签发与验证

AiServer 0.9起只保留账号与API key，移除密码设置及`POST /api/v1/auth/login`、`POST /api/v1/auth/logout`。账号用于任务归属、配额和用量统计；OpenCode使用该账号的独立key，不提交账号密码。

后续客户端统一沿用此认证方式。用户从管理员取得服务根地址和个人API key，即可接入；请求身份由key绑定的账号确定，不额外提交用户名。这里使用AiServer签发的key，不是OpenAI或NATAPP的key。两个API前缀使用同一把key，每次受保护请求都携带：

~~~http
Authorization: Bearer <完整的AiServer API key>
~~~

`Bearer`是HTTP认证方案名；客户端的API key字段只填完整密钥，由SDK添加这个前缀。自写HTTP客户端则按上面的格式设置请求头。仅传`X-API-Key`、查询参数`api_key`、JSON中的`apiKey`或账号密码均不能认证。没有登录换取令牌、自动刷新或退出登录流程；更换／撤销key由管理员处理，本机清除凭据不会撤销服务端key。

| 接入位置 | 服务地址 | 密钥位置 |
|---|---|---|
| 本工程OpenCode启动入口 | 服务根地址，如`https://szzj.nat200.top`，不带`/v1` | 掩码设置窗口中的API key，Windows当前用户加密保存 |
| 手工配置OpenCode | `provider.aiserver.options.baseURL`为`https://szzj.nat200.top/v1` | `options.apiKey`引用`{env:AISERVER_API_KEY}`；配置见第10.1节 |
| 自写HTTP／OpenAI兼容SDK | 原生路径用根地址；OpenAI兼容SDK的base URL用根地址加`/v1` | HTTP请求头如上；SDK的`api_key`参数只传完整key |

签发、本机设置、手工OpenCode和只读连接检查的具体步骤集中在[运维文档](../../../docs/正式工程运维.md#账号与api-key)。先用带key的`GET /api/v1/auth/me`核实账号，再读取models；health免认证成功只证明存活，不能证明key有效或模型可用。401检查密钥是否完整、账号是否启用、key是否到期／撤销；就绪失败与认证失败按各自状态处理。

管理员在5080本机通过管理器或`token-create`签发key，绑定已有的启用账号。`Store.token_create`使用UUID创建keyId，再用`secrets.token_urlsafe(32)`生成256位随机秘密，组成`ais_<keyId>_<随机秘密>`。完整key仅在签发时返回；数据库`api_keys`只保存完整key的SHA256摘要、keyId、用户及创建／到期／撤销信息，无法从列表取回key明文。

请求使用`Authorization: Bearer <key>`。`Store.authenticate_details`按keyId查询、计算来访key的SHA256摘要，以`hmac.compare_digest`核对，并检查账号启用状态、撤销和到期。key由AiServer随机生成，不从账号名或模型名称推算。

### 3.2 GET /api/v1/auth/me

无请求体。成功字段：user.userId、user.displayName为字符串，expiresAt为UTC时间字符串或null；null表示没有固定到期时间。有限期API key同样有效。

<!-- example:me -->
~~~json
{
  "user": {"userId": "engineer1", "displayName": "工程师1"},
  "expiresAt": "2026-11-23T02:00:00Z"
}
~~~

本机OpenCode独立加密保存key，连接时通过me核实身份。401需要重新设置有效key，网络暂时失败不删除本地会话。

### 3.3 管理、迁移与用量

本地管理器与CLI支持创建／列出账号、启用／禁用，以及签发／列出／撤销key。默认key不设固定到期时间，也可指定未来的到期时间；列表只返回keyId、账号、创建／到期／撤销时间及状态，不返回key或其摘要。具体操作维护在运维文档“账号与API key”章节，不提供公网签发或管理接口。

禁用账号使该账号全部key无法认证并取消活动任务；重新启用后未到期、未撤销的key恢复可用，但旧任务不重做。撤销单个key不会影响同账号其他key，也不承诺自动停止此前已接收的任务；停止任务用cancel或禁用账号。

升级到schema v5前自动备份旧库，移除活动用户表的密码字段，保留现有账号、key、有效期、撤销状态、任务和性能记录。历史已签发的有期限凭据不自动撤销，继续按其有效期／撤销状态校验；升级后无法再通过密码领取凭据。回退必须使用旧程序及升级前数据库备份。

这里的API key是访问凭据；模型输入／输出token是工作量单位。当前服务按任务记录模型token并按账号管理请求／任务配额，同账号多个key共用账号配额；没有按key独立的模型token额度、余额或计费。

## 4. 模型列表和动态能力

### GET /api/v1/models

无请求体。models为数组，列出管理员配置的全部模型，每项有自己的能力与限制。客户端必须从id取得model参数，不能把示例名称写死。数组为空时提示没有可用模型，不能自行换成Fake或调用模型引擎端口。

<!-- example:models -->
~~~json
{
  "models": [
    {
      "id": "qwen3-vl:4b-instruct-q4_K_M",
      "toolCall": true,
      "inputTypes": [
        "text",
        "image"
      ],
      "outputTypes": [
        "text"
      ],
      "limits": {
        "maxRequestBytes": 20971520,
        "maxTextUtf8Bytes": 131072,
        "maxMessages": 128,
        "maxImages": 1,
        "imageMediaTypes": [
          "image/png",
          "image/jpeg"
        ],
        "maxImageBytes": 3145728,
        "maxImageLongEdge": 1280,
        "maxImagePixels": 786432,
        "contextTokens": 32768,
        "maxInputTokens": 32255,
        "maxOutputTokens": 4096,
        "defaultMaxInputTokens": 28160,
        "reservedTokens": 512,
        "inputBudgetPolicy": "window-minus-output-v1"
      },
      "capacityMethod": "conservative-estimate-v1",
      "generationParameters": "request-or-model-default"
    }
  ]
}
~~~

| 字段 | 类型及含义 |
|---|---|
| id | string，提交时使用的精确模型标识 |
| inputTypes | string[]，text／image；以当前返回为准 |
| outputTypes | string[]，当前为text |
| toolCall | boolean，0.9.3起返回引擎已确认的原生工具调用能力；false时客户端不得发送tools。旧服务缺少此字段时按不支持处理 |
| generationParameters | string，request-or-model-default；OpenAI兼容入口可显式传生成参数，未指定时使用模型／引擎默认，资源上限独立生效 |
| thinking | boolean，引擎确认的thinking能力；OpenCode接入脚本映射为reasoning，缺少时为false |
| thinkingLevels／defaultThinkingLevel／thinkingCanDisable | 可选，命名等级模型的可选值、默认值及是否允许关闭；GPT-OSS发布low／medium／high，详见10.2 |
| outputChannels | string[]，传输可保留content、reasoning_content、tool_calls，不代表每个模型都会生成所有通道 |
| roleMappings | object，当前developer映射为system，是后端兼容降级，不保证两种角色语义完全相同 |
| outputLimitPolicy | string，reject-above-limit；显式输出预算超过maxOutputTokens时拒绝，不静默缩小 |
| limits.maxRequestBytes | int，整次UTF-8 JSON字节上限，包含Base64；默认20MiB |
| limits.maxTextUtf8Bytes | int，全部文字总字节上限；旧模型默认128KiB，当前9B／27B为2MiB，以models返回为准 |
| limits.maxMessages | int，最多消息条数，当前128 |
| limits.maxImages | int，整次请求全部消息／历史合计的图片数量上限；当前部署可能为1，协议上限4 |
| limits.imageMediaTypes | string[]，当前image/png、image/jpeg |
| limits.maxImageBytes | int，每张原始文件字节上限，默认3MiB |
| limits.maxImageLongEdge | int，图片长边像素上限，默认1280 |
| limits.maxImagePixels | int，单图宽×高上限，默认786432 |
| limits.contextTokens | int或null，模型上下文容量 |
| limits.maxInputTokens | int或null，只预留1个输出token时的最大输入；每次请求仍须扣除实际输出预算与reservedTokens |
| limits.defaultMaxInputTokens | int或null，采用默认／最大输出预算时的输入上限 |
| limits.reservedTokens | int或null，服务端上下文预留 |
| limits.inputBudgetPolicy | window-minus-output-v1或provider-defined；前者要求输入＋本次有效输出预算＋预留不超过contextTokens |
| limits.maxOutputTokens | int或null，服务端固定输出上限 |
| capacityMethod | string，容量检查方法；生产当前为conservative-estimate-v1，Fake可为fake-bytes-only-v1 |

数字是样例，客户端启动／切换模型后重新读取能力。字节和张数达标不保证上下文装得下：文字和图片共同消耗上下文，最终以服务端容量检查为准。不得静默删图、截代码或丢弃历史以规避报错。

## 5. 健康与就绪

### GET /api/v1/health

200正文为{"status":"ok"}。它只能证明API有响应，不能证明模型可用、用户认证正确或本次输入可被接收。健康限流可返回429，致命状态可返回503标准错误。

### GET /api/v1/ready

200表示当前允许接收；503可返回同样的就绪结构，canAcceptJobs=false。503也可能是标准错误结构，必须根据字段区分。已有请求排队不等于不就绪，busy时仍可接受队列中的新任务；实际用户／全局配额在提交时检查。

<!-- example:ready -->
~~~json
{
  "canAcceptJobs": true,
  "schedulerState": "available",
  "inferenceProvider": "Ollama",
  "acceptedInputTypes": ["text", "image"],
  "reasonCode": null,
  "observedAt": "2026-10-24T02:00:00Z",
  "recommendedPollSeconds": 3,
  "capacityMethod": "conservative-estimate-v1",
  "storage": "ok"
}
~~~

全部字段必有：canAcceptJobs为bool；schedulerState为下表枚举；inferenceProvider、capacityMethod为string；acceptedInputTypes为string[]；reasonCode为string或null；observedAt为UTC时间；recommendedPollSeconds为int；storage为ok或unavailable。

| schedulerState | 客户端显示建议 |
|---|---|
| starting | 服务正在启动 |
| available | 模型就绪 |
| busy | 正在处理任务，可排队 |
| draining | 当前任务收尾中 |
| recovering | 引擎恢复中 |
| paused | 服务已暂停 |
| stopping | 服务正在停止 |

不按模型供应商或capacityMethod猜测是否已业务验证。认证、协议可用、模型生成质量及脚本正确性分别确认。

## 6. 推理请求：POST /api/v1/jobs

### 6.1 唯一请求结构

顶层只有下列三个必填字段，没有operation、context、history、sessionId、parentJobId、stream、temperature或max_tokens。

| 字段 | 类型 | 规则 |
|---|---|---|
| clientRequestId | string(UUID) | 每次用户主动发起的新一轮生成创建新UUID；先保存，再联网 |
| model | string | 1–128字符，使用models.id原文 |
| messages | object[] | 1–128条，按模型应看到的顺序提供完整必要输入 |

每个message必填且仅包含role和content：

| 字段 | 类型 | 规则 |
|---|---|---|
| role | string | system、user、assistant；角色原样传递，服务端不补C#系统规则 |
| content | object[] | 1–256个有序片段 |

片段是以下二者之一，不能增加其他字段：

- 文字：type="text"，text为string。单个text最多2097152个字符，所有文字合计还受maxTextUtf8Bytes约束。
- 图片：type="image"，mediaType为image/png或image/jpeg，dataBase64为原始文件的纯Base64；不包含data:前缀或换行，编码字符串最多4194304字符。

图片只放在user消息。至少存在一条user消息，整体输入不能全为空白。图片格式和MIME必须一致；不支持URL、文件路径、动画、压缩请求或multipart。多段文字原样拼接，不自动加空格或换行，请客户端明确分隔。

带图消息的文字中不能包含保留字面串[img]，否则400 INVALID_REQUEST。客户端不需要自己添加图片占位符。

### 6.2 一次文字请求

<!-- example:request -->
~~~json
{
  "clientRequestId": "416417b5-e936-4cde-b0df-969281936318",
  "model": "qwen3-vl:4b-instruct-q4_K_M",
  "messages": [
    {
      "role": "system",
      "content": [{"type": "text", "text": "请用中文回答。代码需兼容.NET Framework 4.7.2，只使用我提供的接口。"}]
    },
    {
      "role": "user",
      "content": [{"type": "text", "text": "请写一个计算double数组平均值的C#方法，空数组时抛出异常。"}]
    }
  ]
}
~~~

如果不需要系统规则，省略system消息。一般聊天、解释或修改均由用户文字表达，不使用另一组业务接口。

### 6.3 图片和历史

下面是实际可校验的合成1×1 PNG传输样例，用于协议测试，不作为视觉效果验收。

<!-- example:request -->
~~~json
{
  "clientRequestId": "416417b5-e936-4cde-b0df-969281936319",
  "model": "qwen3-vl:4b-instruct-q4_K_M",
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "下面是一张接口测试用合成图片："},
        {"type": "image", "mediaType": "image/png", "dataBase64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="},
        {"type": "text", "text": "\n请说明你能观察到的内容。"}
      ]
    }
  ]
}
~~~

JPEG用同样结构，mediaType改为image/jpeg，并将JPEG文件字节转为Base64。每次最多几张以models.limits.maxImages为准；历史中的图片也计入整次上限。

多轮请求需要显式带回必要历史，没有服务端会话ID或隐式历史：

<!-- example:request -->
~~~json
{
  "clientRequestId": "416417b5-e936-4cde-b0df-969281936320",
  "model": "qwen3-vl:4b-instruct-q4_K_M",
  "messages": [
    {"role": "system", "content": [{"type": "text", "text": "请用中文回答，代码兼容.NET Framework 4.7.2。"}]},
    {"role": "user", "content": [{"type": "text", "text": "解释数组平均值的计算。"}]},
    {"role": "assistant", "content": [{"type": "text", "text": "先求和，再除以元素数量；空数组需要单独处理。"}]},
    {"role": "user", "content": [{"type": "text", "text": "现在写成C#方法。"}]}
  ]
}
~~~

所有样例UUID仅为演示，实际调用使用新编号。客户端可让用户调整要发送的历史，但不能在用户不知情时丢弃关键代码或图片。

## 7. SSE成功响应：提交和生成共用一个连接

正常接纳后HTTP 200，Content-Type: text/event-stream; charset=utf-8，Location指向/api/v1/jobs/{jobId}。X-Trace-Id用于本次HTTP排障，Cache-Control为no-store。

请求格式／鉴权／容量等前置失败直接是4xx／5xx JSON。流开始后不能改变HTTP状态，业务失败通过failed事件、存储层中断通过error事件告知。

### 7.1 事件结构

每个事件由event:事件名、data:单行JSON和一个空行构成。文字中的换行在JSON中转义。默认每10秒有冒号开头的注释心跳：

~~~text
: keep-alive

~~~

| event | data全部字段／含义 |
|---|---|
| accepted | jobId:string(UUID)、clientRequestId:string(UUID)、model:string；保存任务编号 |
| status | 第8.1节的完整状态对象；可能有多个，展示排队／执行／取消中 |
| delta | jobId:string(UUID)、sequence:int（从1递增）、text:string；追加回答片段 |
| completed | 第8.2节完整结果，status=succeeded且output.isComplete=true |
| failed | 第8.2节完整结果，status=failed，可能有不完整output |
| cancelled | 第8.2节完整结果，status=cancelled，可能有不完整output |
| error | jobId:string(UUID)、error:第9节错误对象、traceId:string(UUID)；流服务层中断，需要后续查任务 |

不要把TCP数据块当作事件或完整JSON；必须连续解码UTF-8，再按行和空行解析。忽略注释及未知SSE字段。delta可能只是一小段文字，不能按片段尝试编译或覆盖脚本。

### 7.2 完整SSE样例

以下是合成演示，所有data均为完整JSON，模型和计数不表示真实推理实测。状态事件可能与delta交错，客户端以事件类型和任务编号处理，不假定固定帧数。

<!-- example:sse -->
~~~text
event: accepted
data: {"jobId":"196867e2-2b19-4a06-9fcf-8f34427840a1","clientRequestId":"416417b5-e936-4cde-b0df-969281936318","model":"qwen3-vl:4b-instruct-q4_K_M"}

event: status
data: {"jobId":"196867e2-2b19-4a06-9fcf-8f34427840a1","clientRequestId":"416417b5-e936-4cde-b0df-969281936318","status":"running","createdAt":"2026-10-24T02:00:00Z","startedAt":"2026-10-24T02:00:01Z","finishedAt":null,"queue":null,"timing":{"queueMs":1000,"executionMs":30},"schedulerState":"busy","resultAvailable":false,"error":null,"payloadExpiresAt":null,"pollAfterSeconds":3,"stopReason":null,"inferenceStopConfirmed":null}

event: delta
data: {"jobId":"196867e2-2b19-4a06-9fcf-8f34427840a1","sequence":1,"text":"先求和，"}

: keep-alive

event: delta
data: {"jobId":"196867e2-2b19-4a06-9fcf-8f34427840a1","sequence":2,"text":"再除以元素数量。"}

event: completed
data: {"jobId":"196867e2-2b19-4a06-9fcf-8f34427840a1","clientRequestId":"416417b5-e936-4cde-b0df-969281936318","model":"qwen3-vl:4b-instruct-q4_K_M","status":"succeeded","output":{"text":"先求和，再除以元素数量。","isComplete":true},"generation":{"inputTokens":null,"outputTokens":null,"finishReason":"stop","profileId":"example-profile","messageMappingVersion":"client-messages-v1","inferenceProvider":"Ollama","estimatedInputTokens":null,"capacityMethod":"conservative-estimate-v1"},"stopReason":null,"inferenceStopConfirmed":null,"error":null,"payloadExpiresAt":"2026-10-25T02:00:03Z"}

~~~

同一任务按sequence追加，重复的sequence不要再次追加。最终事件的output.text是完整快照，应替换临时回答，不能再追加一次。如果收到failed／cancelled，保留部分内容并标记未完成；收到error、异常EOF或超时时，不能自行把当前文字升级为成功。

### 7.3 连接断开和停止

- 用户点击停止：可POST cancel，或取消当前SSE HTTP请求并释放响应。关闭SSE后服务端检测到断开会取消排队／运行任务；公网代理可能增加检测延迟。
- cancel成功表示停止意图已受理；inferenceStopConfirmed为false时仍在清理引擎，不代表已释放GPU执行位。
- 暂时断网、客户端关闭、45秒没有心跳或完整SSE行：按中断处理，保留请求编号及已显示文字，重新连通后GET原任务。
- 不支持从某个SSE序号续传、Last-Event-ID恢复或对同一任务重开生成流。恢复只查询状态／最终或部分结果。
- 已生成成功后再关闭连接，不撤回成功结果。完成和停止竞争时，以服务器先提交的终态／停止意图为准。

## 8. 状态、结果、停止和列表的精确字段

### 8.1 GET jobs/{jobId}与GET jobs/by-request/{clientRequestId}

两个接口返回相同形状，区别只是查找编号。都不接受请求体，也不会启动推理。

<!-- example:status -->
~~~json
{
  "jobId": "196867e2-2b19-4a06-9fcf-8f34427840a1",
  "clientRequestId": "416417b5-e936-4cde-b0df-969281936318",
  "status": "queued",
  "createdAt": "2026-10-24T02:00:00Z",
  "startedAt": null,
  "finishedAt": null,
  "queue": {"waitingAhead": 1, "activeAhead": 1, "position": 2, "isPaused": false},
  "timing": {"queueMs": 800, "executionMs": null},
  "schedulerState": "busy",
  "resultAvailable": false,
  "error": null,
  "payloadExpiresAt": null,
  "pollAfterSeconds": 3,
  "stopReason": null,
  "inferenceStopConfirmed": null
}
~~~

| 字段 | 类型及含义 |
|---|---|
| jobId／clientRequestId | string(UUID)，分别是服务器任务与客户端请求编号 |
| status | queued、running、cancelling、succeeded、failed、cancelled |
| createdAt | UTC string，任务创建时间 |
| startedAt／finishedAt | UTC string或null，尚未发生时为null |
| queue | object或null，只有排队时有对象；其他状态为null |
| queue.waitingAhead | int，前面等待中的任务数 |
| queue.activeAhead | int，当前在执行／取消中的任务数，单独统计 |
| queue.position | int，等待队列中的位置=waitingAhead+1，不包含执行中的任务 |
| queue.isPaused | bool，当前调度是否处于暂停／恢复／停止等状态 |
| timing.queueMs | int，排队耗时毫秒；等待时持续增加 |
| timing.executionMs | int或null，执行毫秒，未开始为null |
| schedulerState | 第5节枚举，表示整个服务，不是本任务状态 |
| resultAvailable | bool，终态正文目前能否读取，不表示生成成功；failed／cancelled也可能为true |
| error | 第9节错误对象或null |
| payloadExpiresAt | UTC string或null，正文到期时刻；非终态通常为null |
| pollAfterSeconds | int，客户端查询间隔建议 |
| stopReason | string或null，可能为user_cancel、client_disconnected、user_disabled或超时／停机原因码 |
| inferenceStopConfirmed | bool或null：true确认引擎停止；false仍待确认；null未设置或无需确认 |

succeeded、failed、cancelled是终态，不能回到running。终态后引擎停止确认等元数据仍可能更新。排队位置是当前快照，不承诺预计完成时间。

### 8.2 GET jobs/{jobId}/result

未终态409 JOB_NOT_FINISHED；已到期410 RESULT_EXPIRED；任务不存在或不属于自己404。终态成功读取均为HTTP 200，仍需检查body.status。

<!-- example:result -->
~~~json
{
  "jobId": "196867e2-2b19-4a06-9fcf-8f34427840a1",
  "clientRequestId": "416417b5-e936-4cde-b0df-969281936318",
  "model": "qwen3-vl:4b-instruct-q4_K_M",
  "status": "succeeded",
  "output": {"text": "先求和，再除以元素数量。", "isComplete": true},
  "generation": {
    "inputTokens": null,
    "outputTokens": null,
    "finishReason": "stop",
    "profileId": "example-profile",
    "messageMappingVersion": "client-messages-v1",
    "inferenceProvider": "Ollama",
    "estimatedInputTokens": null,
    "capacityMethod": "conservative-estimate-v1"
  },
  "stopReason": null,
  "inferenceStopConfirmed": null,
  "error": null,
  "payloadExpiresAt": "2026-10-25T02:00:03Z"
}
~~~

全部顶层字段必有。jobId／clientRequestId为UUID，model和status为string。output为null或{text:string,isComplete:bool}。output=null表示没有可用文字；不等同于任务不存在。error、stopReason、inferenceStopConfirmed、payloadExpiresAt含义同状态响应。

generation始终为对象，字段如下：

| 字段 | 类型及含义 |
|---|---|
| inputTokens／outputTokens | int或null，实际模型计数；未取得时不能用估算冒充 |
| estimatedInputTokens | int或null，输入容量估算 |
| finishReason | string或null，模型正常完成通常为stop，输出限额通常为length |
| profileId | string，冻结的服务端模型配置版本 |
| messageMappingVersion | string，消息映射版本，当前新请求为client-messages-v1 |
| inferenceProvider | string，例如Ollama；Fake表示模拟 |
| capacityMethod | string，容量估算方法 |

output.text始终是原始回答，可能包含Markdown、代码块、解释或澄清。服务端不提供code字段、补丁、结构化脚本或编译结果；客户端自己决定如何提取和展示。

### 8.3 POST jobs/{jobId}/cancel

无请求体，必须认证。排队或已有终态200；运行／取消中202。重复停止不会再生成。

<!-- example:cancel -->
~~~json
{
  "jobId": "196867e2-2b19-4a06-9fcf-8f34427840a1",
  "status": "cancelling",
  "cancelAccepted": true,
  "inferenceStopConfirmed": false,
  "schedulerState": "busy"
}
~~~

字段全部必有：jobId为UUID、status为任务枚举、cancelAccepted为bool、inferenceStopConfirmed为bool或null、schedulerState为服务枚举。对于已成功／失败的任务cancelAccepted可能为false，不能用停止操作抹掉已提交的结果。之后继续查询状态，或等待原SSE的终态事件。

### 8.4 GET jobs列表

limit可省略，默认20、范围1–50。beforeSequence是可省略的正整数游标，不是页码。收到nextBeforeSequence后作为下一次beforeSequence；null表示没有下一页。

<!-- example:list -->
~~~json
{
  "items": [
    {
      "sequence": 100,
      "jobId": "196867e2-2b19-4a06-9fcf-8f34427840a1",
      "clientRequestId": "416417b5-e936-4cde-b0df-969281936318",
      "status": "succeeded",
      "createdAt": "2026-10-24T02:00:00Z",
      "finishedAt": "2026-10-24T02:00:03Z",
      "errorCode": null,
      "resultAvailable": true
    }
  ],
  "nextBeforeSequence": null
}
~~~

items为数组，每项字段全部必有：sequence为64位正整数；jobId／clientRequestId为UUID；status为任务枚举；createdAt为UTC string；finishedAt为UTC string或null；errorCode为string或null；resultAvailable为bool。列表按sequence倒序，只含本人任务，不含输入／输出正文。

## 9. 错误、重试、幂等和保留

### 9.1 统一错误结构

<!-- example:errorResponse -->
~~~json
{
  "error": {
    "code": "RATE_LIMITED",
    "message": "Rate limited",
    "retryable": true,
    "details": {"retryAfterSeconds": 60}
  },
  "traceId": "27cf5da7-d451-47c5-833b-9afc15b06c1e"
}
~~~

error.code／message为string，retryable为bool，details为object；traceId为UUID。程序依赖code，不匹配message中的英文文案。details可能为空，可包含field、reason、jobId、status、retryAfterSeconds；仅按对应错误读取可选字段。响应头X-Trace-Id也用于排障。

代理可能返回HTML、空正文或连接失败；解析不到上述结构时按传输／服务入口错误处理，不显示原始HTML或请求凭据。重试延迟优先遵守Retry-After（秒数或HTTP日期），无头时可3、6、12、30秒退避并加少量随机抖动。

| HTTP／错误码 | 客户端动作 |
|---|---|
| 400 INVALID_REQUEST | 检查字段、类型、JSON、未知／重复键、图片角色／保留标记；修改输入后创建新请求编号 |
| 401 UNAUTHORIZED | key过期、撤销、无效或账号禁用；检查账号状态并重新设置有效key |
| 404 MODEL_NOT_FOUND | 重新查询models；不要静默切换模型 |
| 404 JOB_NOT_FOUND | 编号不存在或无权访问；不把404当作完成 |
| 404／405 INVALID_REQUEST或非JSON | 路径／部署版本可能不符，报告服务维护者，不伪造认证或推理成功 |
| 408 UPLOAD_TIMEOUT | 先按原clientRequestId查是否已经接受 |
| 409 REQUEST_ALREADY_EXISTS | 相同编号／输入已存在，details中有jobId、status；只查询，不启动第二次推理 |
| 409 IDEMPOTENCY_CONFLICT | 相同编号对应不同输入；新一轮必须新UUID |
| 409 JOB_NOT_FINISHED | 继续查询状态，当前结果未到读取终态时机 |
| 410 RESULT_EXPIRED | 正文已过期，使用本地历史，不自动重新生成 |
| 413 REQUEST_TOO_LARGE | 检查整次JSON和文字字节限制 |
| 415 UNSUPPORTED_MEDIA_TYPE／IMAGE_INVALID | 检查JSON类型、压缩、Base64、文件格式和MIME |
| 422 IMAGE_LIMIT_EXCEEDED／CONTEXT_TOO_LARGE | 根据models缩小输入或由用户决定重做；不能静默截断 |
| 429 RATE_LIMITED／USER_JOB_LIMIT／QUEUE_FULL | 退避，先查原编号以确认是否已存在 |
| 503 SERVICE_NOT_READY／ENGINE_RECOVERING／STORAGE_UNAVAILABLE／INPUT_BUDGET_UNVERIFIED | 暂停提交，退避查询ready，保留原请求编号 |
| 500 INTERNAL_ERROR | 保存traceId，先查原任务，不自动创建新请求重复推理 |

流内failed.error还可能为QUEUE_TIMEOUT、EXECUTION_TIMEOUT、OUTPUT_TRUNCATED、MODEL_EMPTY_OUTPUT、MODEL_PROTOCOL_ERROR、MODEL_CONFIG_ERROR、MODEL_UNAVAILABLE、MODEL_BUSY、SERVER_RESTARTED、SERVICE_STOPPED、CONTEXT_TOO_LARGE。OUTPUT_TRUNCATED可含部分文字，必须标记未完成。模型质量问题不一定有错误码，完整文本也可能答错。

retryable只提示该类临时故障可能恢复，不授权自动重新生成。GET可以退避重试；POST jobs的未知结果必须先查回。

### 9.2 提交响应丢失或流中断的恢复算法

1. 从客户端保存的原始请求取得clientRequestId，GET jobs/by-request/{id}。
2. 200且仍queued／running／cancelling：按pollAfterSeconds轮询；不能重新POST去订阅。
3. 200且为终态：resultAvailable=true时GET result；false表示当前没有可读取正文，保留状态并提示到期。
4. 404：没有查到，不代表可随意换编号重发。只有确认要重试本次尚未接受的提交时，可以用同一编号与完全相同输入有限重试；首次SSE断开已查到任务时不走此分支。
5. 重新生成是明确的新任务：用户触发、新UUID、新请求快照。不要把错误重试偷偷变为新一轮。

相同账号下，JSON对象键顺序不影响请求身份；文字内容、消息／片段顺序、模型、图片原始字节会影响身份。相同编号相同输入返回409 REQUEST_ALREADY_EXISTS，不重新生成；相同编号改输入返回409 IDEMPOTENCY_CONFLICT。客户端无需自己实现服务端哈希算法，只需冻结保存原请求。

### 9.3 默认限额、超时和正文保存

| 项目 | 当前工程默认／客户端建议 |
|---|---|
| 推理并发／队列 | 单生成位；每用户最多2个未完成，全局10个，可排队 |
| 认证请求／提交频率 | 每用户120次请求／分钟、其中推理提交6次／分钟 |
| 普通HTTP请求 | 客户端建议15秒；不要把该超时套在整个推理上 |
| 推理上传／接收 | 120秒，客户端等到SSE响应头后解除上传期限 |
| 排队／执行 | 默认1800秒／600秒，由服务端控制 |
| SSE心跳／网络空闲 | 默认10秒心跳；客户端建议45秒无完整行或心跳视为中断 |
| 查询 | 遵循pollAfterSeconds，默认3秒；避免高速轮询耗尽请求配额 |
| 正文保存 | 新任务从终态起默认24小时；确切时间看payloadExpiresAt |
| 幂等元数据 | 默认保留30天；窗口之后也不要复用旧UUID |

限额和超时可由部署方调整。上述默认数字不是延迟保证。正文包含输入、图片、准备材料、片段和最终回答；到期后接口拒绝读取，不能把服务器当成永久历史库。客户端需保留自己的会话和必要结果。

## 10. OpenAI兼容接口与OpenCode

### 10.1 连接与模型选择

同一个服务额外提供GET /v1/models和POST /v1/chat/completions，两者均使用Authorization: Bearer <个人API key>。使用管理员签发的个人API key；服务端不再提供账号密码登录。账号禁用、密钥撤销／到期、每账号／全局任务配额和请求／提交限流与原生接口共用。

GET /v1/models返回OpenAI格式：{"object":"list","data":[{"id":"实际模型ID","object":"model","created":0,"owned_by":"aiserver"}]}。只列出已配置的模型，不自动公开Ollama中的全部模型。每个请求的model决定使用哪个模型；两种接口共用FIFO队列与单生成位，切换模型可涉及加载等待。模型限制从GET /api/v1/models逐项读取。

0.9.2的OpenCode接入脚本将全部已发布模型及各自能力写入provider.models，支持在`/models`切换；多模型公开配置见[样例](../../examples/opencode.multimodel.example.json)。自管引擎允许模型分别保留KV精度／Flash Attention设置，差异通过串行任务间重启自管引擎生效；不会同时运行多个生成。Qwen3.5-9B当前只开放文字／工具，输入预算为保守估算，实际usage仍来自引擎。安装、运行与测试只维护在运维文档。

OpenCode为唯一客户端。配置示例为9B的128K窗口、默认输出预算下114176输入／16384输出、纯文本与工具调用。limit.input取models.limits.defaultMaxInputTokens；旧服务缺少该字段时按contextTokens减maxOutputTokens及reservedTokens（缺少时512）推导，并保留更小的maxInputTokens上限。地址、模型ID及三个limit须与实际服务能力一致：

<!-- openai-example:config -->
~~~json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "aiserver": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "AiServer",
      "options": {
        "baseURL": "https://szzj.nat200.top/v1",
        "apiKey": "{env:AISERVER_API_KEY}",
        "timeout": 3600000
      },
      "models": {
        "qwen3.5:9b-q4_K_M": {
          "name": "Qwen3.5 9B Q4 / Q8 KV (thinking off)",
          "tool_call": true,
          "modalities": {
            "input": [
              "text"
            ],
            "output": [
              "text"
            ]
          },
          "limit": {
            "context": 131072,
            "input": 114176,
            "output": 16384
          },
          "reasoning": true,
          "options": {
            "think": false
          },
          "variants": {
            "normal": {
              "think": false
            },
            "thinking": {
              "think": true
            }
          }
        },
        "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-IQ3_XXS": {
          "name": "Qwen3.8 27B UD-IQ3_XXS / Q8 KV",
          "tool_call": true,
          "modalities": {
            "input": [
              "text"
            ],
            "output": [
              "text"
            ]
          },
          "limit": {
            "context": 81920,
            "input": 65024,
            "output": 16384
          },
          "reasoning": true,
          "options": {
            "think": false
          },
          "variants": {
            "normal": {
              "think": false
            },
            "thinking": {
              "think": true
            }
          }
        }
      }
    }
  },
  "model": "aiserver/qwen3.5:9b-q4_K_M",
  "compaction": {
    "auto": true,
    "reserved": 10000,
    "prune": true
  },
  "permission": {
    "edit": "ask",
    "bash": "ask"
  }
}
~~~

上例通过`provider.aiserver.options.apiKey`引用进程环境变量`AISERVER_API_KEY`，变量值为完整个人key，不带`Bearer `。本工程启动入口自动解密并注入该变量，用户无需手工配置环境变量或再执行`/connect`。自行管理OpenCode时，也可删除上例的`options.apiKey`，改用OpenCode的`/connect`凭据存储；两种设置步骤见[运维文档](../../../docs/正式工程运维.md#手工opencode接入)。

上例声明9B和27B，默认选择9B。服务端须完成模型准备并开放该精确ID；旧图文模型文件保留不代表仍对外开放。自动压缩会改变后续发送的历史；原始大输入仍须完整符合输入预算。历史、工具结果及压缩由OpenCode自行管理，并计入服务端输入预算。不要选择Responses API适配器。baseURL以/v1结尾，不是/api/v1或完整/chat/completions路径。

### 10.2 请求与支持范围

顶层必填model、messages；stream默认false。支持stream_options.include_usage、tools、tool_choice、parallel_tool_calls、temperature、top_p、seed、stop、presence_penalty、max_tokens或max_completion_tokens（二者不能同时给出）、n=1，以及可选user标签（不改变认证身份）。扩展字段top_k、repeat_penalty与think只接受当前锁定Ollama支持的值。未知参数明确400，不静默宣称支持。

0.9.12起按模型区分思考控制。`/api/v1/models`的`thinking`仍表示引擎确认的能力；只有命名等级模型额外返回`thinkingLevels`、`defaultThinkingLevel`和`thinkingCanDisable`。Qwen及Gemma沿用布尔开关：未指定、null或false关闭，true开启；无思考能力模型忽略布尔开关，向引擎省略think。GPT-OSS发布`["low","medium","high"]`、默认medium及`thinkingCanDisable=false`：think未指定、null或true使用medium，字符串选择对应等级，false返回HTTP 400／UNSUPPORTED_PARAMETER，不把low冒充关闭。字符串等级发给未发布该等级的模型也返回400，发生在入队和生成前。

兼容接口还接受`reasoning_effort=low|medium|high|null`作为命名think别名；与非null的think同时给出时必须同值，否则400／INVALID_REQUEST。原生`/api/v1/jobs`使用同样的think布尔／等级语义，不增加reasoning_effort别名。例见`contracts/examples/openai-gptoss-request.json`。命名等级来自目标引擎`/api/show`确认；旧模型配置、旧客户端及布尔请求行为保持兼容，GPT-OSS使用新版OpenCode接入脚本生成low／medium／high variants。公开五模型配置见`contracts/examples/opencode.multimodel.example.json`，只列实际注册并验证的模型；不自动安装示例中的模型。

0.9.5起不再将旧私有Temperature／TopP／TopK／Seed／Think／PresencePenalty字段注入推理请求。采样参数优先级为显式请求、可选GenerationDefaults、模型／引擎默认；GenerationDefaults默认空。思考独立采用上述模型控制策略，旧GenerationDefaults.think及顶层Think保留兼容读取但不生效；命名模型配置使用ThinkingLevels和DefaultThinkingLevel。预检与生成复用冻结的消息、工具、思考设置与输出预算。服务端不增加业务提示、不裁剪或摘要历史、不改写正文；模型模板由锁定安装资源管理。

max_tokens／max_completion_tokens在上限内原样执行，超过models.maxOutputTokens返回HTTP 400／OUTPUT_LIMIT_EXCEEDED，param为对应请求字段；未指定时采用公开的服务上限。NumPredict保留为旧资源限额字段名，另支持MaxOutputTokens优先覆盖。完整输入上限=contextTokens－本次有效输出预算－reservedTokens；上下文超限仍明确拒绝，保持truncate=false／shift=false。原生/api/v1可选think及assistant.reasoning_content，输出预算仍采用服务上限。各模型可以有不同输出上限，客户端必须从models读取，不把Qwen的16K套给新模型。

messages支持system、developer（按公开roleMappings映射为system）、user、assistant和tool。content可为字符串或有序text／image_url片段；assistant带tool_calls或reasoning_content时可为null。assistant.reasoning_content原样映射为Ollama的thinking字段；计入文字和上下文预算。messages.name及image_url.detail=low／high当前无法忠实映射，返回400／UNSUPPORTED_PARAMETER；detail缺省或auto可用。image_url仅接受data:image/png;base64,...或data:image/jpeg;base64,...，图片只放user；不抓取远程URL、不重压缩图片。全部文字、工具定义、历史工具参数与思考合计受maxTextUtf8Bytes限制，协议最大2MiB，最多128条消息／128个工具。消息和工具属性顺序保留，幂等哈希使用的规范化序列化不用于重排发送材料。

tools只支持type=function，function包含name、parameters（JSON Schema对象）、可选description；strict=true不受支持并返回400。服务端在启动时从真实/api/show确认tools能力；不支持时带tools的请求返回TOOLS_NOT_SUPPORTED，普通图文仍可用。

OpenCode的tool_call=false是能力声明，Build仍可能发送工具定义；接入脚本为无工具模型生成绑定该模型、禁用全部工具的文字代理。该模型作为配置默认模型时同时选择文字代理。TOOLS_NOT_SUPPORTED错误的param为tools，提示使用文字代理；不删除请求工具后静默重试，也不把普通文字转换成工具执行。具体操作统一见运维文档。


0.9.3的`/api/v1/models`逐模型返回`toolCall`，OpenCode接入脚本将其写入`tool_call`，缺少字段时默认false。客户端不能把代码模型名称等同于原生工具调用支持。

tool_choice支持auto、none、required或指定函数对象。none不向引擎提供工具；指定函数只向引擎提供该函数。Ollama没有强制选择保证：required／指定函数若未生成调用，返回TOOL_CHOICE_NOT_SATISFIED，不能把普通文字冒充工具调用。parallel_tool_calls=false时若引擎仍返回多个调用，本轮报模型协议错误，不向客户端发布这些调用。

一次工具请求示例：

<!-- openai-example:request -->
~~~json
{
  "model": "qwen3.5:9b-q4_K_M",
  "messages": [{"role": "user", "content": "请读取 example.txt 并概括内容"}],
  "stream": true,
  "stream_options": {"include_usage": true},
  "tools": [{"type": "function", "function": {
    "name": "read_file",
    "description": "读取客户端本地文本文件",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
  }}],
  "tool_choice": "auto"
}
~~~

服务端返回的assistant.tool_calls中，每项含id、type=function和function.name／arguments；arguments是JSON字符串。客户端在自己的授权范围内执行工具，下一次请求带上原assistant消息和所有对应tool结果。例如：

<!-- openai-example:tool-result -->
~~~json
{
  "model": "qwen3.5:9b-q4_K_M",
  "messages": [
    {"role": "user", "content": "请读取 example.txt 并概括内容"},
    {"role": "assistant", "content": null, "tool_calls": [{
      "id": "call_example", "type": "function",
      "function": {"name": "read_file", "arguments": "{\"path\":\"example.txt\"}"}
    }]},
    {"role": "tool", "tool_call_id": "call_example", "content": "这是一份示例文件。"}
  ],
  "stream": false,
  "tools": [{"type": "function", "function": {
    "name": "read_file", "description": "读取客户端本地文本文件",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
  }}]
}
~~~

需要继续调用工具时再带tools。tool_call_id必须匹配前一assistant中的未处理调用；所有调用必须有且只有一个结果，不能漏掉、重复或插入其他角色。服务端转换为Ollama的tool_name；服务端不执行工具、不读客户端文件、不运行模型生成的命令。OpenCode继续管理本机工具、权限和会话历史。

### 10.3 响应、排队、断线与错误

非流式返回chat.completion，含id、created、model、choices[0].message、finish_reason和usage。正文在message.content；引擎公开返回的思考保留在message.reasoning_content扩展字段，不拼入正文、不添加思考标签。成功响应的finish_reason为stop／tool_calls；达到输出上限返回下述OUTPUT_TRUNCATED错误，原生任务指标保留finishReason=length。思考独有、空白或空正文按引擎真实结束原因保留，不再一律转换为MODEL_EMPTY_OUTPUT。usage只使用引擎实际计数，缺失时null，不用估算冒充。

流式为text/event-stream，每帧data: JSON加空行，不含原生accepted／status事件。首帧为assistant角色，正文使用choices[0].delta.content，思考使用delta.reasoning_content；两通道分别原样保存后按引擎分块顺序发送。工具调用先保存，确认成功后按index发完整delta.tool_calls；未完成工具不作为可执行成功调用发布。include_usage=true时额外发送choices=[]的usage帧，最后data: [DONE]。排队／长生成期间发送注释心跳。失败后发送error帧，不发送成功finish或[DONE]；客户端不能将缺终态EOF当作成功。思考样例见openai-reasoning-request.json、openai-reasoning-response.json和openai-reasoning-stream.sse。

输出达到本次有效上限时，兼容接口返回`OUTPUT_TRUNCATED`错误，不再只返回`finish_reason=length`后静默结束。非流式使用HTTP 422，流式在已发送的正文／思考片段之后发送兼容错误帧，不发送成功结束或`[DONE]`，也不发布本轮工具调用。错误提示包含本次有效token上限，说明思考与正文共用预算，建议提高上限、关闭思考或分批生成；它不是上下文超限，不触发压缩重试。为阻止OpenCode 1.19.x对未知流错误自动重试，流内error.message采用序列化事件，内层code=invalid_prompt、aiserverCode=OUTPUT_TRUNCATED；外层标准error.code仍为OUTPUT_TRUNCATED，提示正文同样带此码。已保存的部分结果及实际usage通过原生结果接口读取，`isComplete=false`。公开错误样例见`contracts/examples/openai-truncated-error.json`及`openai-truncated-stream-error.sse`。

响应头X-Job-Id可关联同账号的原生状态／结果／取消接口，X-Trace-Id用于排障。原生结果output保留text、可选reasoningText与toolCalls，isComplete标识完整性；失败或取消后的已保存正文和思考仍可在有效期内查询。原生SSE使用delta返回正文、reasoning返回思考，二者共用sequence递增，客户端分别累计；不支持思考展示的旧客户端可以忽略reasoning事件。两通道及工具结果继承正文保留／清理期限，不进入性能日志。普通和流式连接断开都会请求取消并经过原有恢复流程；非流式等待期间不提前发送200。兼容提交没有幂等重放；网络不确定时不要自动重提执行了工具的整轮业务。

上下文超限在响应开始前返回HTTP 400、error.code=context_length_exceeded、type=invalid_request_error、param=messages；原生API保持422／CONTEXT_TOO_LARGE，并可带输入计数、有效输出预算与本次输入上限。OpenCode开启compaction.auto时可据此进入压缩恢复。流已开始后保持HTTP 200并发送相同code的错误帧；为兼容OpenCode 1.19.x及其SDK解包行为，流内error.message为序列化的{type:error,error:{message,type,param,code}}事件，外层仍保留标准code／type／param。对应公开样例为openai-context-error.json和openai-context-stream-error.sse。

发送响应前的其他错误使用HTTP错误状态与{"error":{"message":"安全描述","type":"invalid_request_error等","param":null,"code":"错误码"}}；429／503可带Retry-After。认证、任务归属、错误正文脱敏和保留期限沿用原服务规则。此兼容范围不包括/v1/responses、音频、嵌入、文件、批处理和服务端工具执行。

9B的capacityMethod为ollama-render-tokenizer-v1：引擎只渲染实际聊天模板，服务端用锁定tokenizer对完整模板分词，包含系统规则、历史及工具。预算计数与引擎实际usage分别记录；保持truncate=false、shift=false，超限明确拒绝。9B的128K窗口／16K默认输出／512预留时，默认输入上限为114176；请求1024输出时为129536，仅请求1输出时为130559。models.maxInputTokens表示后一种最大值，OpenCode仍须为实际输出和压缩留空间。纯文本模型返回maxImages=0、imageMediaTypes=[]及图片尺寸限制0。
