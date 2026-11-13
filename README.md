# 冷链配送施工封闭核对与附件归档 API

两项能力，共用一个服务：

1. **施工封闭核对**：出车前核对计划路线是否与公路施工封闭冲突；
2. **附件归档**：把施工公告附带的 PDF 交通管制图整组归档到持久卷，
   供原站附件失效后复核。

## 施工封闭核对（POST /check）

给定公告列表页 URL 和计划路段（编号、方向、带时区的驶入/驶出时间），服务会：

1. 抓取公告列表页并自动跟随**同站“下一页”分页**；
2. 逐页抓取公告详情（BeautifulSoup 解析），提取**路段、方向、生效日期、
   每周封闭日、起止钟点**；
3. 按**路段编号 + 方向**匹配，判断路段通行区间与封闭区间是否相交；
4. 返回受阻路段、相交的封闭时段与公告链接；
5. 任何页面抓取失败时标记 `undetermined`（无法判定）并给出失败地址，
   **绝不会在信息不完整时报“全程畅通”**。

### 时间规则（需求约定）

- 公告中的所有时间一律按**北京时间（UTC+8）**解释；请求中的时间可带任意
  时区偏移（如 `Z`、`+09:00`），服务内部统一换算为北京时间。
- **跨午夜封闭归属开始当天**：如 `22:00 至 次日 05:00` 的封闭日按 22:00
  所在日期计算，结束时刻落在次日。
- **生效日期限定封闭开始日**：只有封闭区间的开始日落入生效日期区间内，
  该封闭才成立；跨午夜窗口的尾部即使延伸到生效期外也不影响归属。
- 通行区间与封闭区间均按半开区间 `[start, end)` 判断相交，恰好首尾相接
  （如 05:00 驶入、封闭 05:00 结束）不算受阻。
- `00:00 至 24:00` 视为当天全天封闭（跨午夜到次日 00:00）。

## 附件归档（POST /archive）

给定公告目录（列表页）URL，服务会：

1. 抓取同站分页列表与全部公告详情；
2. 从每条详情页提取**同站 PDF 附件**链接并全部下载；
3. 逐个校验**真实文件类型**（`%PDF-` 文件头，不认扩展名/Content-Type），
   边下边算 **SHA-256**；
4. 把每条公告的附件作为**一个整体组**写入持久卷（`ARCHIVE_DIR`，
   Compose 中挂载命名卷 `archive-data` 到 `/data/archive`）；
5. 返回每条公告的链接、各附件 SHA-256 与**可调用的下载地址**。

### 整组替换与失败语义

- **整组替换**：附件集合或内容（SHA-256）与既有归档不一致时，先在新暂存
  目录准备好全部新文件，再把旧组目录整体换出、新组整体换入并原子更新清单
  ——同一公告的归档**绝不混用新旧文件**；无变化时保留原归档（`unchanged`）。
- **单公告失败隔离**：某附件下载失败、实际内容不是 PDF 或落盘失败时，
  该公告本轮标为**未完成**并在 `incomplete[]` 中列出全部失败地址与阶段
  （`download`/`validate`/`store`）；其**原有完整归档保持可下载**
  （`previous_archive_retained`），其他公告照常归档。
- **崩溃恢复**：暂存目录与换出的回收目录在服务启动时自动清理；
  清单写入采用临时文件 + `os.replace` 原子替换。
- 单附件上限 50 MiB；归档轮次在进程内串行执行，避免并发写卷。

### 存储布局（持久卷内）

```
/data/archive/
├── manifest.json                  # 完整归档清单（公告 -> 文件/SHA-256/来源）
└── announcements/<公告key>/<归档文件名>.pdf
```

`<公告key>` 由公告 URL 派生（可读 slug + 短哈希），稳定不变。

### 归档接口一览

| 方法/路径 | 说明 |
| --- | --- |
| `POST /archive` | 执行一轮归档，入参 `{"announcement_list_url": "..."}` |
| `GET /archive/announcements` | 列出当前卷内全部完整归档（复核查询） |
| `GET /archive/files/{key}/{filename}` | 下载归档附件（即响应中的 `download_url`） |

`POST /archive` 响应要点：

| 字段 | 含义 |
| --- | --- |
| `status` | `completed` 全部成功；`partial` 部分完成；`failed` 本轮无任何公告完成 |
| `archived[]` | 成功公告：`state`（`archived`/`updated`/`unchanged`）+ 每个附件的 `sha256`、`download_url` |
| `incomplete[]` | 未完成公告：失败地址/阶段/原因、是否保留原有完整归档 |
| `failures[]` | 列表页/详情页抓取失败地址 |

## 目录结构

```
.
├── app/                  # FastAPI 采集、核对与归档服务
│   ├── main.py           # 路由：/health、/check、/archive、/archive/announcements、/archive/files/...
│   ├── models.py         # /check 请求/响应模型（时间必须带时区）
│   ├── archive_models.py # /archive 请求/响应模型
│   ├── archiver.py       # 附件下载校验、SHA-256、整组原子替换、清单管理
│   ├── parser.py         # BeautifulSoup 列表分页、详情与附件链接解析
│   ├── scraper.py        # httpx 异步抓取、重试、失败记录
│   ├── scheduler.py      # 路段匹配与区间相交判定
│   └── timeutils.py      # 北京时间、跨午夜、生效期、星期解析
├── sample-site/          # 离线样本站（Flask）：分页+详情+PDF 附件，
│                         #   含多附件、附件变更、下载失败、假冒 PDF 示例
├── tests/smoke_test.py         # /check 端到端冒烟测试（仅标准库）
├── tests/archive_smoke_test.py # /archive 端到端冒烟测试（仅标准库）
├── Dockerfile
├── docker-compose.yml    # api + sample-site + archive-data 持久卷
└── requirements.txt
```

## 用 Docker Compose 启动（离线可用）

镜像构建仅需在有网络时拉取基础镜像与依赖；运行后两个容器之间走内部
Docker 网络，调用样本站不需要外网。归档数据保存在命名卷 `archive-data`
中，容器重建后仍在。

```bash
docker compose up --build
```

- API 文档：<http://localhost:8000/docs>
- 样本站列表：<http://localhost:8080/announcements?page=1>

## 请求示例

### 封闭核对

```bash
curl -X POST http://localhost:8000/check \
  -H 'Content-Type: application/json' \
  -d '{
    "announcement_list_url": "http://sample-site:8080/announcements?page=1",
    "segments": [
      {
        "road_id": "G15-K123",
        "direction": "下行",
        "enter_at": "2026-11-16T21:30:00+08:00",
        "exit_at":  "2026-11-17T02:00:00+08:00"
      }
    ]
  }'
```

### 附件归档

```bash
curl -X POST http://localhost:8000/archive \
  -H 'Content-Type: application/json' \
  -d '{"announcement_list_url": "http://sample-site:8080/announcements?page=1"}'

# 查看当前卷内归档清单
curl http://localhost:8000/archive/announcements

# 按响应中的 download_url 下载某个归档附件
curl -OJ http://localhost:8000/archive/files/gg-2026-001--xxxxxxxx/g15k123-phase1-map.pdf
```

> 容器内访问样本站用 `http://sample-site:8080`；从宿主机访问用
> `http://localhost:8080`（此时把该 URL 作为 `announcement_list_url` 即可）。

## 样本站的演示开关

样本站内置四类附件场景，全部离线可用：

| 公告 | 场景 |
| --- | --- |
| `gg-2026-001` | 多附件（2 个 PDF 管制图） |
| `gg-2026-002` | 附件变更：`POST /admin/revise` 升级版本，文件名与内容同时变化，触发整组替换 |
| `gg-2026-003` | 故障注入：`POST /admin/outage` 切换附件 503，验证“本轮未完成 + 原归档仍可下载” |
| `gg-2026-004` | 持续故障：一个附件 404（下载失败）、一个附件实为 HTML（类型校验失败） |

另有一个已撤销公告的失效详情链接，用于演示页面采集失败处理。

## 本地开发（不用 Docker）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r sample-site/requirements.txt
python sample-site/app.py &
ARCHIVE_DIR=/tmp/archive-data uvicorn app.main:app --port 8000
python tests/smoke_test.py          # /check 冒烟
python tests/archive_smoke_test.py  # /archive 冒烟（需空归档目录，如新卷）
```

## 解析约定

- 列表页：提取同站（同 scheme + 同 host）的公告详情链接，并按“下一页/
  next/›”等文本识别分页链接，最多跟随 50 页并按 URL 去重防环。
- 详情页：从含“路段编号/方向/生效日期/封闭日/封闭时间”关键词的行中用
  正则提取字段；字段缺失或无法解析时该详情页按采集失败处理。
- 附件：优先在 `attachments` 容器内提取同站 `.pdf` 链接，否则整页扫描；
  按 URL 去重，文件类型以实际内容（`%PDF-` 文件头）为准。
- 方向：公告为“双向”时匹配任一通行方向；否则需精确一致（忽略空白）。
- 请求超时 10 秒（连接 5 秒），每个地址失败后重试 1 次。
