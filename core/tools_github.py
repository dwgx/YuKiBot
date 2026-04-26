"""ToolExecutor GitHub mixin — GitHub API 交互相关。

从 core/tools.py 拆分。"""
from __future__ import annotations

import asyncio
import re
from typing import Any
import logging as _logging

from utils.text import clip_text, normalize_text
from core.tools_types import ToolResult, _tool_trace_tag, _shared_github_request, _shared_repo_readme_request

_tool_log = _logging.getLogger("yukiko.tools")


class ToolGithubMixin:
    """Mixin — 从 tools.py ToolExecutor 拆分。"""

    def _get_github_client(self) -> httpx.AsyncClient:
        """Return a shared AsyncClient for GitHub API requests.

        Uses ``self._http_timeout`` and ``self._github_headers``.
        """
        if self._shared_github_client is None or self._shared_github_client.is_closed:
            self._shared_github_client = httpx.AsyncClient(
                timeout=self._http_timeout,
                follow_redirects=True,
                headers=self._github_headers,
            )
        return self._shared_github_client

    async def _method_browser_github_search(
        self,
        method_name: str,
        method_args: dict[str, Any],
        query: str,
        message_text: str = "",
        group_id: int = 0,
        api_call: Callable[..., Awaitable[Any]] | None = None,
    ) -> ToolResult:
        if not self._tool_interface_github_enable:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "GitHub 方法已关闭。"},
                error="github_method_disabled",
            )

        raw_query = normalize_text(str(method_args.get("query", ""))) or normalize_text(
            query
        )
        if not raw_query:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "请告诉我你要在 GitHub 搜什么。"},
                error="empty_query",
            )

        search_query = raw_query
        language = normalize_text(str(method_args.get("language", "")))
        if language:
            search_query = f"{search_query} language:{language}"

        stars_min = method_args.get("stars_min", 0)
        try:
            stars_min = max(0, int(stars_min))
        except Exception:
            stars_min = 0
        if stars_min > 0:
            search_query = f"{search_query} stars:>={stars_min}"

        sort = normalize_text(str(method_args.get("sort", ""))).lower()
        if sort not in {"updated", "stars"}:
            sort = "stars"

        params = {
            "q": search_query,
            "sort": sort,
            "order": "desc",
            "per_page": self._github_search_per_page,
        }

        endpoint = f"{self._github_api_base}/search/repositories"
        try:
            response = await self._get_github_client().get(endpoint, params=params)
        except Exception as exc:
            return await self._github_search_web_fallback(
                method_name=method_name,
                raw_query=raw_query,
                reason=f"github_search_failed:{exc}",
                human_reason="GitHub API 暂时不可用，已改用网页搜索兜底。",
            )

        if response.status_code == 403:
            return await self._github_search_web_fallback(
                method_name=method_name,
                raw_query=raw_query,
                reason="github_rate_limited",
                human_reason="GitHub API 触发限流，已改用网页搜索兜底。",
            )
        if response.status_code >= 400:
            return await self._github_search_web_fallback(
                method_name=method_name,
                raw_query=raw_query,
                reason=f"github_search_http_{response.status_code}",
                human_reason=f"GitHub API 返回 {response.status_code}，已改用网页搜索兜底。",
            )

        try:
            data = response.json()
        except Exception as exc:
            return await self._github_search_web_fallback(
                method_name=method_name,
                raw_query=raw_query,
                reason=f"github_search_parse_failed:{exc}",
                human_reason="GitHub 返回数据解析失败，已改用网页搜索兜底。",
            )

        items = data.get("items", []) if isinstance(data, dict) else []
        if not isinstance(items, list) or not items:
            fallback_terms = re.findall(r"[A-Za-z0-9_.-]{2,}", raw_query)
            alt_query = " ".join(dict.fromkeys(fallback_terms[:3]))
            if alt_query and alt_query.lower() != raw_query.lower():
                alt_params = dict(params)
                alt_params["q"] = alt_query
                try:
                    alt_resp = await self._get_github_client().get(endpoint, params=alt_params)
                    if alt_resp.status_code < 400:
                        alt_data = alt_resp.json()
                        alt_items = (
                            alt_data.get("items", [])
                            if isinstance(alt_data, dict)
                            else []
                        )
                        if isinstance(alt_items, list) and alt_items:
                            items = alt_items
                except Exception:
                    pass
        if not isinstance(items, list) or not items:
            return await self._github_search_web_fallback(
                method_name=method_name,
                raw_query=raw_query,
                reason="github_search_empty",
                human_reason="GitHub API 未命中，已改用网页搜索兜底。",
            )

        results: list[dict[str, Any]] = []
        evidence: list[dict[str, str]] = []
        lines = [
            f"GitHub 里“{raw_query}”我先给你找了 {min(len(items), self._github_search_per_page)} 个："
        ]
        for idx, item in enumerate(items[: self._github_search_per_page], start=1):
            if not isinstance(item, dict):
                continue
            full_name = normalize_text(str(item.get("full_name", "")))
            html_url = normalize_text(str(item.get("html_url", "")))
            description = normalize_text(str(item.get("description", "")))
            language_name = normalize_text(str(item.get("language", "")))
            stars = item.get("stargazers_count", 0)
            updated = normalize_text(str(item.get("updated_at", "")))
            if not full_name or not html_url:
                continue

            results.append(
                {
                    "full_name": full_name,
                    "url": html_url,
                    "description": description,
                    "language": language_name,
                    "stars": stars,
                    "updated_at": updated,
                }
            )
            star_text = f"{stars}★" if isinstance(stars, int) else "未知★"
            extra = f" | {language_name}" if language_name else ""
            desc_short = clip_text(description, 72) if description else "无简介"
            lines.append(f"{idx}. {full_name} ({star_text}{extra})")
            lines.append(f"   {desc_short}")
            lines.append(f"   {html_url}")
            evidence.append(
                {"title": full_name, "point": desc_short, "source": html_url}
            )

        auto_download_notice = ""
        should_auto_download = (
            bool(api_call)
            and int(group_id or 0) > 0
            and self._looks_like_download_request_text(f"{message_text}\n{raw_query}")
        )
        if should_auto_download and results:
            ok_auto, auto_text, auto_payload = await self._try_auto_upload_github_asset(
                raw_query=raw_query,
                results=results,
                message_text=message_text,
                group_id=int(group_id),
                api_call=api_call,
            )
            if ok_auto:
                payload = {
                    "text": auto_text,
                    "query": raw_query,
                    "results": results,
                    "evidence": evidence,
                }
                payload.update(auto_payload)
                return ToolResult(
                    ok=True,
                    tool_name=method_name,
                    payload=payload,
                    evidence=evidence,
                )
            if auto_text:
                auto_download_notice = auto_text

        if len(lines) == 1:
            return ToolResult(
                ok=True,
                tool_name=method_name,
                payload={"text": f"GitHub 上没拿到可用仓库结果：{raw_query}"},
            )

        if auto_download_notice:
            lines.insert(1, auto_download_notice)

        return ToolResult(
            ok=True,
            tool_name=method_name,
            payload={
                "text": "\n".join(lines),
                "query": raw_query,
                "results": results,
                "evidence": evidence,
            },
            evidence=evidence,
        )

    async def _try_auto_upload_github_asset(
        self,
        raw_query: str,
        results: list[dict[str, Any]],
        message_text: str,
        group_id: int,
        api_call: Callable[..., Awaitable[Any]] | None,
    ) -> tuple[bool, str, dict[str, Any]]:
        if not api_call or group_id <= 0 or not results:
            return False, "", {}
        try:
            # 复用 Agent 里现成的下载/验签/上传链路，避免重复维护两套逻辑。
            from core.agent_tools import _handle_smart_download
        except Exception as exc:
            return (
                False,
                f"自动下载不可用（工具加载失败：{clip_text(str(exc), 80)}）",
                {},
            )

        errors: list[str] = []
        for item in results[:3]:
            repo_url = normalize_text(str(item.get("url", "")))
            repo_name = normalize_text(str(item.get("full_name", "")))
            if not repo_url:
                continue
            prefer_ext, file_name = self._guess_download_preferences(
                raw_query=raw_query,
                message_text=message_text,
                repo_name=repo_name,
            )
            args: dict[str, Any] = {
                "url": repo_url,
                "query": raw_query,
                "kind": "file",
                "upload": True,
                "group_id": int(group_id),
            }
            if prefer_ext:
                args["prefer_ext"] = prefer_ext
            if file_name:
                args["file_name"] = file_name
            try:
                dl_result = await _handle_smart_download(
                    args,
                    {
                        "api_call": api_call,
                        "group_id": int(group_id),
                        "tool_executor": self,
                        "config": self._raw_config,
                    },
                )
            except Exception as exc:
                errors.append(clip_text(f"{repo_name or repo_url}: {exc}", 120))
                continue

            if not bool(getattr(dl_result, "ok", False)):
                err_text = normalize_text(
                    str(getattr(dl_result, "display", ""))
                ) or normalize_text(str(getattr(dl_result, "error", "")))
                if err_text:
                    errors.append(
                        clip_text(f"{repo_name or repo_url}: {err_text}", 120)
                    )
                continue

            data = getattr(dl_result, "data", {}) or {}
            local_file = normalize_text(str(data.get("local_file", "")))
            download_url = normalize_text(str(data.get("download_url", "")))
            source_url = normalize_text(str(data.get("source_url", ""))) or repo_url
            file_label = Path(local_file).name if local_file else (file_name or "文件")
            text = normalize_text(str(getattr(dl_result, "display", "")))
            if not text:
                text = f"已下载并上传群文件：{file_label}"
                if download_url:
                    text += f"\n下载源：{download_url}"
            payload = {
                "downloaded_file": local_file,
                "download_url": download_url,
                "source_url": source_url,
                "uploaded": True,
            }
            return True, text, payload

        if errors:
            return False, f"自动下载尝试失败：{errors[0]}。先给你可靠链接。", {}
        return False, "", {}

    async def _github_search_web_fallback(
        self,
        method_name: str,
        raw_query: str,
        reason: str,
        human_reason: str,
    ) -> ToolResult:
        try:
            rows = await self.search_engine.search(f"site:github.com {raw_query}")
        except Exception:
            rows = []

        picked: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in rows:
            url = _unwrap_redirect_url(normalize_text(getattr(item, "url", "")))
            title = normalize_text(getattr(item, "title", ""))
            snippet = normalize_text(getattr(item, "snippet", ""))
            if "github.com/" not in url.lower():
                continue
            if not url or url in seen:
                continue
            seen.add(url)
            picked.append({"title": title or "GitHub", "snippet": snippet, "url": url})
            if len(picked) >= self._github_search_per_page:
                break

        if not picked:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={},
                error=reason,
            )

        lines = [f"{human_reason}", f"GitHub 相关结果（{raw_query}）："]
        evidence: list[dict[str, str]] = []
        for idx, item in enumerate(picked, start=1):
            title = clip_text(normalize_text(item.get("title", "")) or "GitHub", 68)
            snippet = clip_text(normalize_text(item.get("snippet", "")) or "无摘要", 88)
            url = normalize_text(item.get("url", ""))
            lines.append(f"{idx}. {title}")
            lines.append(f"   {snippet}")
            lines.append(f"   {url}")
            evidence.append({"title": title, "point": snippet, "source": url})

        return ToolResult(
            ok=True,
            tool_name=method_name,
            payload={
                "text": "\n".join(lines),
                "query": raw_query,
                "results": picked,
                "evidence": evidence,
                "fallback": "web_search",
            },
            evidence=evidence,
        )

    async def _method_browser_github_readme(
        self,
        method_name: str,
        method_args: dict[str, Any],
        query: str,
    ) -> ToolResult:
        if not self._tool_interface_github_enable:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "GitHub 方法已关闭。"},
                error="github_method_disabled",
            )

        repo = normalize_text(str(method_args.get("repo", "")))
        if not repo:
            url_value = normalize_text(str(method_args.get("url", "")))
            if url_value:
                repo = self._extract_github_repo_from_text(url_value)
        if not repo:
            repo = self._extract_github_repo_from_text(query)
        if not repo:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "请给我仓库名（owner/repo）或 GitHub 仓库链接。"},
                error="repo_required",
            )

        max_chars = method_args.get("max_chars", self._github_readme_max_chars)
        try:
            max_chars = max(200, min(12000, int(max_chars)))
        except Exception:
            max_chars = self._github_readme_max_chars

        repo_endpoint = f"{self._github_api_base}/repos/{repo}"
        readme_endpoint = f"{repo_endpoint}/readme"
        repo_resp = None
        readme_resp = None
        try:
            gh = self._get_github_client()
            repo_resp = await gh.get(repo_endpoint)
            readme_resp = await gh.get(readme_endpoint)
        except Exception as exc:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={},
                error=f"github_readme_failed:{exc}",
            )

        if repo_resp is None or repo_resp.status_code >= 400:
            status = repo_resp.status_code if repo_resp is not None else 0
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": f"仓库 {repo} 不存在或不可访问。"},
                error=f"github_repo_http_{status}",
            )

        try:
            repo_data = repo_resp.json()
        except Exception:
            repo_data = {}

        full_name = normalize_text(str(repo_data.get("full_name", ""))) or repo
        html_url = (
            normalize_text(str(repo_data.get("html_url", "")))
            or f"https://github.com/{repo}"
        )
        description = normalize_text(str(repo_data.get("description", "")))
        stars = repo_data.get("stargazers_count", 0)
        language = normalize_text(str(repo_data.get("language", "")))

        readme_text = ""
        if readme_resp is not None and readme_resp.status_code < 400:
            try:
                readme_data = readme_resp.json()
            except Exception:
                readme_data = {}
            content_b64 = normalize_text(str(readme_data.get("content", "")))
            encoding = normalize_text(str(readme_data.get("encoding", ""))).lower()
            if content_b64 and encoding == "base64":
                try:
                    decoded = base64.b64decode(
                        content_b64.encode("utf-8"), validate=False
                    )
                    readme_text = decoded.decode("utf-8", errors="ignore")
                except Exception:
                    readme_text = ""

        cleaned = self._clean_markdown_text(readme_text) if readme_text else ""
        cleaned = clip_text(normalize_text(cleaned), max_chars)

        summary_lines = [f"仓库：{full_name}"]
        if isinstance(stars, int):
            summary_lines.append(f"Stars：{stars}")
        if language:
            summary_lines.append(f"语言：{language}")
        if description:
            summary_lines.append(f"简介：{description}")
        summary_lines.append(f"链接：{html_url}")
        if cleaned:
            summary_lines.append(f"README 摘要：{cleaned}")
        else:
            summary_lines.append("README 摘要：这个仓库没有拿到可读 README。")
        evidence = [
            {
                "title": full_name,
                "point": clip_text(cleaned or description or "仓库元数据已获取。", 180),
                "source": html_url,
            }
        ]

        return ToolResult(
            ok=True,
            tool_name=method_name,
            payload={
                "text": "\n".join(summary_lines),
                "repo": full_name,
                "repo_url": html_url,
                "readme_excerpt": cleaned,
                "evidence": evidence,
            },
            evidence=evidence,
        )

    def _looks_like_github_request(self, text: str) -> bool:
        content = normalize_text(text)
        if not content:
            return False
        if _shared_github_request(content, config=self._raw_config):
            return True
        return bool(
            re.search(
                r"https?://(?:www\.)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
                content,
                flags=re.IGNORECASE,
            )
        )

    def _looks_like_repo_readme_request(self, text: str) -> bool:
        content = normalize_text(text)
        if not content:
            return False
        if _shared_repo_readme_request(content, config=self._raw_config):
            return True
        plain = re.sub(r"\s+", "", content.lower())
        if "/readme" in plain:
            return True
        return bool(
            re.search(
                r"(?:^|\s)readme\s+[a-z0-9_.-]+/[a-z0-9_.-]+(?:\s|$)",
                content,
                flags=re.IGNORECASE,
            )
        )

    @staticmethod
    def _extract_github_repo_from_text(text: str) -> str:
        content = normalize_text(text)
        if not content:
            return ""

        url_match = re.search(
            r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
            content,
            flags=re.IGNORECASE,
        )
        if url_match:
            owner = url_match.group(1)
            repo = url_match.group(2)
            repo = re.sub(r"\.git$", "", repo, flags=re.IGNORECASE)
            return f"{owner}/{repo}"

        token_match = re.search(r"\b([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\b", content)
        if token_match:
            owner = token_match.group(1)
            repo = re.sub(r"\.git$", "", token_match.group(2), flags=re.IGNORECASE)
            if owner.lower() not in {"http", "https"}:
                return f"{owner}/{repo}"
        return ""
