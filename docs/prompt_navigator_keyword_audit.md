# Prompt Navigator Keyword Routing Audit

Last scanned: 2026-04-29

This audit tracks local keyword / cue based paths that still sit outside Prompt Navigator. The goal is not to delete every string match in the codebase. The rule is:

- Migrate semantic routing and tool choice to Prompt Navigator + LLM section prompts.
- Keep structural facts: URLs, file extensions, OneBot segments, message ids, QQ ids, reply/artifact context, permissions, schema validation, and safety checks.
- Keep tool-internal ranking / validation when it operates after the LLM has already chosen the tool.

## Already Moved

- `core/prompt_navigator.py`
  - Removed natural-language preselect for media search, music, download, sticker, memory, creative generation, web research, and bot strategy.
  - `_preselect()` now keeps structural signals only: URL, video/image URL, media segments, recent media artifact, download file extension, mention target.
- `core/agent.py`
  - Removed keyword-derived `search_media`, download, music, sticker, memory, and image-generation timeout fallbacks.
  - Removed local media type/search mode inference from Agent normalization.
  - Removed legacy force-tool initialization from `run()`.
- `core/agent_tools_search.py`
  - Removed `_infer_media_search_type()`.
  - `search_media` and `search_web_media` require explicit `media_type`.
- `app_helpers.py`
  - Heavy-request detection now uses structural video/file/URL/domain signals instead of broad text cues.
- `core/tools.py`
  - Old download detection was narrowed to explicit control tokens and file extensions.

## Must Migrate Next

### Engine pre-Agent gates

These decide whether the message goes to router/agent, whether a task interrupts another task, or whether a special path runs before Prompt Navigator.

- `core/engine.py`
  - `handle_message()` still calls `_detect_bot_strategy_directive()` before normal routing. This is the biggest remaining "闭嘴/安静/活跃" keyword bypass.
  - `_should_prefer_router_for_plain_text()` still uses local `_looks_like_*` helpers to decide router preference.
  - `_self_check_decision()` still uses local request / media / profile heuristics around router decisions.
  - `should_interrupt_previous_task()` still builds `task_like` from local request/media/music/download helpers.
  - `_looks_like_explicit_request()`, `_looks_like_recent_media_followup_instruction()`, `_looks_like_qq_avatar_intent()`, `_looks_like_qq_profile_intent()`, `_looks_like_local_file_request()`, `_looks_like_local_media_request()` are semantic cue paths.

Migration target:
Engine should only handle structural gates, safety, dedupe, whitelist, queue, and permissions. Semantic task selection should become Prompt Navigator evidence / LLM section selection.

### Trigger / listen probes

- `core/trigger.py`
  - `_looks_like_explicit_bot_request()`
  - `_looks_like_explicit_memory_declare()`
  - `_explicit_request_signal_from_cues()`
  - `_explicit_request_signal()`
  - `evaluate()` still uses these to decide whether to handle or probe.

Migration target:
Trigger should decide "allowed to consider responding" from mention/private/reply/session/whitelist/activity, then let Router/Agent+Navigator decide intent. Keep anti-noise throttling, remove semantic task cue routing.

### Router fallback paths

- `core/router.py`
  - `_fast_path_decision()`
  - `_fallback_media_decision_without_model()`
  - `_parse_decision()` still contains local correction / fallback behavior.

Migration target:
Router should only decide whether to respond and maybe coarse confidence. It should not select final tool family when Agent + Prompt Navigator is enabled.

### Agent legacy helpers

Some helpers are now unreachable from strict main flow, but still exist and are used by tests or legacy branches.

- `core/agent.py`
  - `_looks_like_webpage_fetch_request()`
  - `_infer_resource_file_type()`
  - `_infer_emoji_query()`
  - `_is_explicit_emoji_request()`
  - `_looks_like_file_send_request()`
  - `_looks_like_download_file_request()`
  - `_rewrite_download_tool_if_needed()`
  - `_looks_like_generic_media_question()`
  - `_should_force_image_tool_first()`
  - `_select_forced_video_tool()`
  - `_looks_like_video_parse_request()`
  - `_looks_like_video_analysis_request()`
  - `_should_force_voice_tool_first()`
  - `_select_forced_web_tool()`
  - `_select_forced_media_tool()`
  - `_looks_like_all_images_request()`

Migration target:
Delete unreachable legacy force paths after tests are updated. Keep only structural helpers used for URL/path/media extraction, or convert them into tool arg validation that runs after LLM chooses the tool.

### Old ToolExecutor routing

- `core/tools.py`
  - `_detect_query_type()`
  - `_should_auto_web_analysis()`
  - `_looks_like_media_request()`
  - `_looks_like_deep_web_analysis_request()`
  - `_looks_like_image_request()`
  - `_looks_like_image_send_request()`
  - `_looks_like_qq_avatar_request()`
  - `_contains_self_avatar_cue()`
  - `_looks_like_local_file_request()`
  - `_looks_like_local_media_request()`

Migration target:
ToolExecutor should execute explicit actions selected by Agent tools. It should not infer high-level task intent from free text once Prompt Navigator is the main architecture.

### Tool-local behavior that may still be too semantic

These are lower priority because they usually run after a tool was selected, but they can still override behavior inside the tool.

- `core/tools_video.py`
  - `_looks_like_video_send_request()`
  - `_looks_like_douyin_search_request()`
  - `_looks_like_video_analysis_request()`
  - `_looks_like_analysis_text_only_request()`
- `core/tools_vision.py`
  - `_looks_like_vision_web_lookup_request()`
  - `_looks_like_image_analysis_request()`
  - `_looks_like_analyze_all_images_request()`
  - `_has_animated_image_hint()`
- `core/agent_tools_media.py`
  - `analyze_image` infers analyze-all and current/reply target from cue lists.
- `core/agent_tools_utility.py`
  - `_STICKER_SEND_CUES`
  - `_STICKER_MANAGEMENT_CUES`
  - `_looks_like_explicit_sticker_send_message()`
  - `_looks_like_sticker_management_message()`

Migration target:
Move these choices into explicit tool args in the relevant Prompt Navigator section prompts, then have tools return `missing_arg` / `ambiguous_target` instead of guessing.

## Keep / Do Not Migrate Blindly

These keyword-like paths are not Prompt Navigator routing and should generally stay:

- Safety and abuse filters: `core/safety.py`, harmful knowledge checks, NSFW/risk/domain blocks.
- High-risk confirmation and cancellation text in `core/agent.py`.
- URL/domain/file signature detection, MIME sniffing, media header checks, HTML payload checks.
- QQ id, message id, file path, local media path, URL, and extension parsing.
- Tool-internal scoring after the tool was explicitly selected, for example search result ranking, download source trust scoring, music candidate ranking.
- Plugin command parsers where the plugin is explicitly invoked, for example NewAPI payment/password parsing.

## Suggested Migration Order

1. Move bot strategy control from `core/engine.py` direct keyword bypass to `qq_admin_social -> admin_command`; keep permission execution in admin layer.
2. Simplify `core/trigger.py` so it only gates attention/noise, not task type.
3. Make `core/router.py` stop selecting tool families when Prompt Navigator strict mode is enabled.
4. Delete strict-mode-unreachable Agent force helpers and rewrite tests around `navigate_section`.
5. Convert old `core/tools.py` free-text routing into explicit Agent tool actions or remove it.
6. Convert tool-local semantic guesses into explicit arguments and `missing_arg` observations.
