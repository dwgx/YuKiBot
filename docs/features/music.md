# 音乐链路说明

## 当前主链路

1. `music_search` 先走 Alger / 网易云搜索。
2. `music_play_by_id` 先尝试网易云播放链接。
3. 若网易云不可用，再按配置尝试：
   - `unblock` 解锁链接
   - 本地替代音源匹配

## 关键配置

### `unblock_sources`

- 作用：只用于 `UnblockNeteaseMusic` 的 `source=` 参数。
- 格式：逗号分隔字符串。
- 当前白名单：`qq, kuwo, kugou, migu`
- 解析规则：`trim + lower + whitelist + 去重`

示例：

```yaml
music:
  unblock_enable: true
  unblock_api_base: http://127.0.0.1:5200
  unblock_sources: qq,kuwo,kugou,migu
```

### `alternative_sources`

- 作用：只用于本地替代音源搜索顺序。
- 不再与 `unblock_sources` 共用同一个字段。
- 当前白名单：`qq, kuwo, kugou, migu`

示例：

```yaml
music:
  local_source_enable: true
  alternative_sources: qq,kugou,kuwo,migu
```

## 本次调整

- `soundcloud` 已从音乐主链路移除。
- `soundcloud` 不再参与：
  - 音乐搜索 fallback
  - 本地替代音源匹配默认链路
  - `unblock_sources` 默认值

## 这样做的原因

- `soundcloud` 混入 `unblock_sources` 会污染 `UnblockNeteaseMusic` 的 `source=` 参数。
- 音乐侧已经有本地匹配和解锁链路，继续塞一条 SoundCloud 支路会增加复杂度和误差面。
- `unblock_sources` 与 `alternative_sources` 分离后，配置语义更清晰，也更容易测试。

## 配置建议

- 默认安装：

```yaml
music:
  local_source_enable: true
  unblock_enable: true
  unblock_sources: qq,kuwo,kugou,migu
  alternative_sources: qq,kuwo,kugou,migu
```

- 如果没有可用的 `unblock_api_base`，`unblock_enable: true` 也不会主动生效；真正是否调用还取决于 `unblock_api_base` 是否配置。
