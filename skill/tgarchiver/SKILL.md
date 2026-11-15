---
name: tgarchiver
description: 批量归档 Telegram 频道里的视频/文件。当用户说「把这个 TG 频道的视频下到某目录」「批量下载频道内容」「归档 Telegram 频道」「找可用的 MTProto / SOCKS 代理给下载器用」时使用。基于本地 telegram-channel-archiver 工具（G:\Python_Code\telegram-channel-archiver）：先扫索引、再按文案/关键词/体积筛选批量下载，2MB 分片断点续传、换代理自动重连、重名加时间戳，附速度看板与代理池。下载前必查 cryptg（缺了慢 112 倍）。
---

# Telegram 频道批量归档（tgarchiver）

把「一个频道 → 一个本地目录」的批量下载固化成五步，断线、关机、换代理都能接着跑。

## 0. 先自检（每次都做，10 秒）

~~~bat
python scripts/check_env.py
~~~

输出是 JSON，看三个字段：

- \`ok\` 为 true 才能继续；
- \`python\` / \`telethon\` 假 → 提示用户装依赖（\`pip install -r requirements.txt\`）；
- **\`cryptg\` 假 → 必须先装**：\`pip install cryptg -i https://pypi.org/simple\`。缺它 MTProto 只能跑 pyaes 纯 Python AES-IGE，速度掉到 1/100（实测 7.3 → 0.065 MB/s），而且加并发、换代理、换协议全都没用 —— 这是「下载慢」的第一嫌疑，不要先去折腾代理；
- \`session\` 假 → 让用户先登录（\`python qr_server.py\` + \`python login.py\`，浏览器开 http://127.0.0.1:47050 扫码）。

**不要替用户猜安装路径**：脚本会优先读环境变量 \`TGARCHIVER_HOME\`，其次自动找 \`G:\Python_Code\telegram-channel-archiver\`。

## 1. 建任务（只需一次）

~~~bat
python tgarchiver.py new --name <任务名> --channel <频道> --out "<输出目录>" --selector <caption|keyword|all> --name-mode <caption|orig|msgid>
~~~

- \`--channel\` 可写 \`@name\`、\`name\`、\`https://t.me/name\`；
- \`--out\` 支持 UNC（\`\\NAS\share\dir\`），不存在会自动建；
- 已经存在同名任务会拒绝，确认要覆盖才加 \`--force\`；
- 建完任务就是 \`state/jobs/<任务名>.json\`，之后可以手改。

## 2. 扫索引（先看命中多少，不下载）

~~~bat
python tgarchiver.py index <任务名>
~~~

输出 \`scanned / titled / selected / totalBytes\`。**命中数为 0 时不要直接 run**，先回去改 \`--selector\` / \`--keywords\` / 体积区间。

## 3. 开下（后台长跑）

~~~bat
python tgarchiver.py run <任务名>
~~~

- 单进程死循环，Ctrl+C 停；**重跑同一条命令就是续传**（分片进度在 \`state/<任务>_progress.json\`）；
- 同样的文件已经下过（同名同大小）会直接跳过；
- 需要限额时加 \`--limit N\`。

在 Agent 里跑请用后台任务，不要阻塞会话；进度读 \`state/<任务>_speed.json\`：

~~~json
{"ts":"...","bps":52428,"mbps":0.05,"session_mb":1.9,"files_done":0,"files_total":1045,"current":"...","proxy":"socks5 1.2.3.4:1080","crypto":"cryptg 0.5.2"}
~~~

\`bps\` 连续两次为 0 且 \`session_mb\` 不涨 → 线路死了，走第 4 步换线路。
但**换线路前先看 \`crypto\`**：不是 \`cryptg x.y.z\` 就先装 cryptg，否则换什么节点都只有几十 KB/s。
装了 cryptg 之后 8 并发 × 512KB 是实测饱和点（约 6.5~7.3 MB/s），不要盲目加到 16（会回落到 3.9）。

## 4. 线路不好就换（代理池）

~~~bat
python tgproxy.py harvest --limit 120                          :: 从频道/机器人收集
python tgproxy.py add "<粘贴的 t.me/proxy 或 t.me/socks 链接>"    :: 用户手贴
python tgproxy.py test --limit 200 --conc 8                     :: TCP 预检 + 真连一次
python speedprobe.py --top 3 --bytes 3                          :: 真下一段量 MB/s
python tgproxy.py apply                                         :: 写 state/proxy.json
~~~

关键事实（别误判成 bug）：

- Telethon **不支持 \`ee\` 前缀（fake-TLS）与 webproxy**，这类只能标「客户端专用」；
- \`apply\` 之后下载器**下一轮重连**才用新线路，不用重启进程；
- 公开频道里的节点存活率低、速度常在几十 KB/s，别向用户承诺「换了就快」。

## 5. 看进度 / 收尾

~~~bat
python monitor.py     :: http://127.0.0.1:47060/  自适应单位 + 五级配色
python tgarchiver.py list
~~~

完成后核对：目标目录文件数 = \`files_done\`，重名文件带 \`_YYYYMMDD_HHMMSS\`，扩展名正确。

## 常见失败与处理

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| \`database is locked\` | 两个进程共用同一个 session | 只留一个 \`run\`；诊断脚本用 sqlite backup 复制会话 |
| \`Could not find the input entity\` | 用了数字 id 当频道 | 用 \`@name\` / username，或先 \`probe\` 一次 |
| \`AuthBytesInvalidError\` | 多连接共用同一 auth key | 减少并发或每个 worker 一份会话副本 |
| 一直 0 B/s | **先查 cryptg**；装好了才是代理出口堵 / 节点死 | 看遥测 \`crypto\` 字段：不是 \`cryptg\` 就 \`pip install cryptg -i https://pypi.org/simple\`；已装再 \`tgproxy test\` + \`speedprobe\` 换线路 |
| 速度只有 0.0x MB/s，怎么调都不涨 | 缺 cryptg（AES-IGE 在跑纯 Python） | 同上；装完 \`BOOT\` 日志末尾应显示 \`crypto=cryptg x.y.z\` |
| 文件名乱码或过长 | 标题里有非法字符 | 已由 \`naming.sanitize\` 处理，\`MAX_NAME_LEN\` 默认 150 |

## 红线

- \`state/tg_session.session\` 等于账号，**不外发、不进仓库、不打包**；
- 只归档用户本来就有权限看到的内容；提醒遵守 Telegram 条款与当地法律；
- 不把频道名、片名、代理账号密码写进任何公开文档或日志。
