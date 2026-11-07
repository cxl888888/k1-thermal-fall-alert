# 静默守护 · K1 Thermal Fall Alert

基于进迭时空 K1 MUSE Pi Pro 的独居老人险情感知与预警端侧系统。项目面向当事人跌倒后未能及时开口求助的场景。K1 持续读取低分辨率热像，发现疑似异常后发起语音核查。明确呼救可直接触发紧急预警。家属在同一局域网内查看事件、核对情况并记录处置。

## 主要功能

| 功能 | 实现 |
| --- | --- |
| 热像感知 | MLX90640 提供 24×32 温度矩阵。时序随机森林比较连续画面中的热区形态、位置及变化速度，输出疑似跌倒候选。 |
| 本地语音 | sherpa-onnx KWS 监听「救命」「救救我」。Silero VAD、SenseVoice 和领域语言模型处理风险表达及核查回答。板端播放预录提示音频。 |
| 核查与告警 | 热像候选触发安全询问，明确呼救可直接锁存红色告警。家属完成核查后可发起解除，页面根据板端回执更新告警状态。 |
| 手机家属端 | 浏览器查看事件来源、语音记录、实时热像和处置状态。拨号入口将保存的号码填到系统拨号界面，由家属确认拨打。 |
| 采集与分析 | 独立工作台支持采集、动作区间标记和标签订正。可按需接入 LD2410C 记录雷达读数，用于同步采集和离线对照。 |

实时决策使用热像和语音，雷达数据用于分析。运行时推理在 K1 本地完成，手机通过局域网访问家属端。

## 安装

下面的命令使用 Bash，从仓库根目录开始执行。家属端可在普通电脑上构建，K1 实机模式使用 Bianbu Linux 和 Python 3.12。前端需要 Node.js 20.19 或更高的 20.x，也支持 22.12 及以上，具体范围见 [package.json](family_portal/frontend/package.json)。

### 1. 安装并构建家属端

```bash
python3 -m venv .venv-family
.venv-family/bin/python -m pip install -r family_portal/backend/requirements.txt
(cd family_portal/frontend && npm ci && npm run build)
```

若在 PC 构建前端，将生成的 `family_portal/frontend/dist/` 放到 K1 上相同的相对路径。后端依赖由 K1 自己的 Python 环境安装。K1 上的 `pydantic-core` 需要与 RISC-V 系统匹配，可使用对应 Bianbu 软件源提供的包。

### 2. 准备 K1 实机依赖

接入 MLX90640、麦克风和音箱。热像驱动默认使用 `/dev/i2c-4` 与地址 `0x33`，请确认接线与设备节点，并给运行用户授予所需 I²C 权限。用 `pactl list short sources` 和 `pactl list short sinks` 查看实际音频节点。

```bash
python3 -m venv --system-site-packages .venv-k1
.venv-k1/bin/python -m pip install numpy smbus2 adafruit-circuitpython-mlx90640
# 仅在使用 USB 串口雷达时安装
.venv-k1/bin/python -m pip install pyserial
```

| 组件 | 需要准备的资源 |
| --- | --- |
| 热像与音频 | K1 环境中的 OpenCV（`cv2`）、`arecord`、`pactl` 和 `paplay`。录音使用 ALSA 的 `plug:pulse`。 |
| SenseVoice | Bianbu 的 `spacemit-asr-server` 和 `spacemit-sensevoice-model`。运行时读取 `/opt/spacemit-asr/llm_asr` 及 `/usr/share/spacemit-asr/sensevoice`。 |
| Silero VAD | 与 K1 匹配的 ONNX Runtime，以及放在 `voice_runtime/engine/models/silero_vad_v5.1.2.onnx` 的 VAD 模型。 |
| sherpa-onnx KWS | RISC-V ALSA 可执行文件 `~/install/bin/sherpa-onnx-keyword-spotter-alsa` 和 `~/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/` 模型目录。求救词配置已随仓库提供。 |
| 本地 LLM | 默认使用原生 llama.cpp 和项目领域 Q8 GGUF。路径、文件校验值和启动参数见 [runtime_candidates.json](voice_runtime/runtime_candidates.json)。权重及原生可执行文件需另行准备。 |

仓库附有热像推理所需的随机森林和播报 WAV。缺少领域 GGUF、ASR、VAD 或 KWS 资源时，可以先运行家属端和离线数据示例。

## 使用

### 本机打开家属端

构建前端后，从仓库根目录执行以下命令。首次运行创建一个空库，之后启动时沿用它即可。

```bash
cd family_portal/backend
../../.venv-family/bin/python -B -m portal.cli --db ../data/local.db init-db
../../.venv-family/bin/python -B -m portal.cli --db ../data/local.db serve --direct-access
```

浏览器打开 `http://127.0.0.1:8794/`。空库会显示暂无事件，接入板端事件后才会出现现场记录。

### 在 K1 联动热像、语音与手机

在下面两个终端都设置相同的音频节点。将尖括号内的值换成 `pactl` 列出的实际节点名称，家属端用这两个设置检查设备状态。

```bash
export K1_AUDIO_SOURCE='<麦克风节点名>'
export K1_AUDIO_SINK='<音箱节点名>'
```

**终端一**，在 K1 上启动家属端。把 `K1_LAN_IP` 换成手机可访问的板端私有 IPv4。首次运行创建独立演示库并生成本机事件生产者凭证：

```bash
cd family_portal/backend
K1_LAN_IP='<K1 的局域网 IPv4>'
FAMILY_PY=../../.venv-family/bin/python
FAMILY_DB=../data/live.db
umask 077
"$FAMILY_PY" -B -m portal.cli --db "$FAMILY_DB" init-db
"$FAMILY_PY" -B -m portal.cli --db "$FAMILY_DB" producer-token |
  sed -n '2p' > ../data/voice_producer_token
chmod 600 ../data/voice_producer_token
"$FAMILY_PY" -B -m portal.cli --db "$FAMILY_DB" serve \
  --host "$K1_LAN_IP" --port 8795 --direct-access --allow-live-ingest \
  --trusted-lan-live --allow-device-resolve
```

重启时只需设置变量并运行 `serve`。生产者凭证仅保存在 K1，供本机事件发送和解除回执使用。

**安全边界。** 上述 8795 配置是受信任局域网内的免配对 HTTP 演示入口。连接该入口的手机具备演示处置权限，请在受控网段使用。

**终端二**，回到仓库根目录启动联合监听：

```bash
.venv-k1/bin/python -B voice_runtime/start_family_voice.py \
  --origin live --source-id k1demo \
  --portal-url http://127.0.0.1:8795 --family-resolve \
  --bridge-python "$PWD/.venv-family/bin/python" \
  --thermal-worker collection_workbench/sensor_worker.py \
  --thermal-model collection_workbench/fall_runtime/thermal_forest.json
```

语音进程也可用 `--source` 和 `--sink` 覆盖环境变量。需要静默保留含糊回答时追加 `--no-clarification`，默认允许一次澄清追问。可先加 `--prepare-only` 查看启动命令，再去掉它启动。

手机与 K1 连到同一局域网，用浏览器打开 `http://<K1 的局域网 IPv4>:8795/`。在事件详情中选择「现场记录」查看转写和实时热像。处置红色告警时，先确认收到并保存人工核查说明，再点击「解除告警」。设备确认后，事件进入历史记录。

### 启动采集工作台

```bash
.venv-k1/bin/python -B collection_workbench/app.py --capture --open
```

工作台默认打开 K1 本机的 `http://127.0.0.1:8791/`。点「开始采集」保存传感器数据，动作起止按钮用于记录标签，采集结束后可回放和订正。工作台与联合监听共用热像设备，切换前先结束正在读热像的一方。

在电脑上回放采集器原始会话时，可执行 `python collection_workbench/launch_pc.py --data-root "<会话目录>"`。发布数据集的读取示例见下节。

## 输入与输出示例

### 示例 1 · 离线读取公开热像数据

公开数据包含 **22 轮**匿名会话和 **17,996 帧**有效热像。每轮以独立目录保存，包含温度帧、修订动作区间和来源校验值，部分会话另有解析后的雷达帧。模拟跌倒使用受控动作采集。标签重叠记录在 [数据索引](dataset/index.json) 中，划分训练和测试时应以会话为单位。

用 Python 环境安装离线分析依赖，再从根目录运行：

```bash
python -m pip install numpy scikit-learn torch
PYTHONPATH="$PWD/algorithms/thermal_fall_baseline" python -B - <<'PY'
from event_data import load_session

session = load_session("dataset/mixed-actions-01")
print(session.meta["session_id"])
print(session.grids.shape)
PY
```

输出：

```text
mixed-actions-01
(1628, 24, 32)
```

这里的输入是 [该会话的温度帧](dataset/mixed-actions-01/thermal.jsonl) 和 [修订标签](dataset/mixed-actions-01/review.json)。输出表示读入 1,628 张有效温度矩阵，每张 24 行、32 列。

### 示例 2 · 现场事件如何流转

以下以设备正在监测、当前无红色锁存为起点。

| 输入 | 板端行为 | 手机端结果 |
| --- | --- | --- |
| 连续热像命中疑似跌倒候选，当事人还未开口 | 播放一次安全询问，等提示音结束后接收回答 | 事件显示热像来源，随后可查看语音转写 |
| 当事人清楚喊出「救命」 | KWS 触发紧急播报，状态机锁存红色告警 | 显示红色告警及事件来源 |
| 家属确认收到、保存核查说明并点击「解除告警」 | 设备核对当前事件和解除请求，返回处理结果 | 收到成功回执后，告警归档，处置记录保留 |

接口开发可参考 [合成事件 JSONL](family_portal/backend/examples/synthetic_event.jsonl)。它用于验证事件格式和入库。手机端的真实告警列表使用板端的实时事件。

## 目录

| 路径 | 内容 |
| --- | --- |
| `voice_runtime/` | 语音、热像联动、告警状态机及启动配置 |
| `family_portal/` | 手机网页、FastAPI 后端和事件桥接 |
| `collection_workbench/` | 采集、回放、动作标注与热像驱动 |
| `algorithms/thermal_fall_baseline/` | 热像时序特征、随机森林和离线评估代码 |
| `k1_collect/` | LD2410C 串口只读采集与协议解析 |
| `dataset/` | 匿名会话、修订标签和数据索引 |
