"""NewAPI HTTP 客户端 — 封装所有 NewAPI 平台 REST 接口。

支持任何基于 new-api / one-api 搭建的站点。
认证方式: Session Cookie (登录后自动携带) 或 System Access Token。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

_log = logging.getLogger("yukiko.newapi.client")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class NewAPIUser:
    id: int = 0
    username: str = ""
    display_name: str = ""
    email: str = ""
    role: int = 1
    status: int = 1
    quota: int = 0
    used_quota: int = 0
    request_count: int = 0
    group: str = "default"
    aff_code: str = ""

@dataclass
class NewAPIToken:
    id: int = 0
    name: str = ""
    key: str = ""
    status: int = 1
    remain_quota: int = 0
    unlimited_quota: bool = False
    used_quota: int = 0
    created_time: int = 0
    expired_time: int = -1
    group: str = ""
    model_limits_enabled: bool = False

# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class NewAPIClient:
    """与 NewAPI 站点交互的 HTTP 客户端。"""

    def __init__(self, base_url: str, timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            follow_redirects=True,
        )
        self._session_token: str | None = None  # cookie session id
        self._access_token: str | None = None   # system access token
        self._user_id: int | None = None

    async def close(self):
        await self._http.aclose()

    # ── 内部 ──────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {}
        if self._access_token:
            h["Authorization"] = f"Bearer {self._access_token}"
        # 此 fork 的 authHelper 对所有认证方式都要求 New-Api-User
        if self._user_id:
            h["New-Api-User"] = str(self._user_id)
        return h

    async def _get(self, path: str, params: dict | None = None) -> dict:
        r = await self._http.get(path, headers=self._headers(), params=params)
        return self._parse(r)

    async def _post(self, path: str, json: dict | None = None) -> dict:
        r = await self._http.post(path, headers=self._headers(), json=json or {})
        return self._parse(r)

    async def _put(self, path: str, json: dict | None = None) -> dict:
        r = await self._http.put(path, headers=self._headers(), json=json or {})
        return self._parse(r)

    async def _delete(self, path: str) -> dict:
        r = await self._http.delete(path, headers=self._headers())
        return self._parse(r)

    @staticmethod
    def _is_invalid_url_resp(resp: dict | Any) -> bool:
        """判断是否为路由不存在（不同 fork 路径差异）导致的错误。"""
        if not isinstance(resp, dict):
            return False
        err = resp.get("error")
        if isinstance(err, dict):
            msg = str(err.get("message", "")).strip().lower()
            if "invalid url" in msg:
                return True
        msg = str(resp.get("message", "")).strip().lower()
        return msg.startswith("invalid url")

    async def _get_with_fallback(self, paths: list[str], params: dict | None = None) -> dict:
        last: dict[str, Any] = {"success": False, "message": "invalid_path"}
        for path in paths:
            resp = await self._get(path, params)
            if not self._is_invalid_url_resp(resp):
                return resp
            last = resp
        return last

    async def _post_with_fallback(self, paths: list[str], body: dict | None = None) -> dict:
        last: dict[str, Any] = {"success": False, "message": "invalid_path"}
        for path in paths:
            resp = await self._post(path, body)
            if not self._is_invalid_url_resp(resp):
                return resp
            last = resp
        return last

    @staticmethod
    def _parse(r: httpx.Response) -> dict:
        try:
            data = r.json()
        except Exception:
            data = {"success": False, "message": r.text[:500]}
        if not isinstance(data, dict):
            data = {"success": True, "data": data}
        return data

    # ── 认证 ──────────────────────────────────────────────────────────────

    async def register(self, username: str, password: str, email: str = "", aff_code: str = "") -> dict:
        """注册新账号。"""
        body: dict[str, Any] = {"username": username, "password": password}
        if email:
            body["email"] = email
        if aff_code:
            body["aff_code"] = aff_code
        return await self._post("/api/user/register", body)

    async def login(self, username: str, password: str) -> dict:
        """登录并保存 session cookie + user_id。"""
        r = await self._http.post(
            "/api/user/login",
            headers=self._headers(),
            json={"username": username, "password": password},
        )
        data = self._parse(r)
        if data.get("success"):
            # 保存 cookies
            self._http.cookies.update(r.cookies)
            # 提取 user_id — authHelper 对所有请求都要求 New-Api-User 头
            user_data = data.get("data", {})
            if isinstance(user_data, dict) and user_data.get("id"):
                self._user_id = user_data["id"]
        return data

    def set_access_token(self, token: str, user_id: int | None = None):
        """使用 System Access Token 认证。"""
        self._access_token = token
        self._user_id = user_id

    # ── 用户信息 ──────────────────────────────────────────────────────────

    async def get_self(self) -> dict:
        """获取当前用户信息。"""
        return await self._get("/api/user/self")

    async def update_self(
        self,
        display_name: str = "",
        password: str = "",
        email: str = "",
        original_password: str = "",
    ) -> dict:
        """更新个人资料。"""
        body: dict[str, Any] = {}
        if display_name:
            body["display_name"] = display_name
        if password:
            body["password"] = password
        if original_password:
            body["original_password"] = original_password
        if email:
            body["email"] = email
        return await self._put("/api/user/self", body)

    async def get_user_groups(self) -> dict:
        """获取用户可用分组。"""
        return await self._get("/api/user/self/groups")

    async def get_user_models(self) -> dict:
        """获取用户可用模型列表。"""
        return await self._get("/api/user/models")

    # ── 令牌管理 ──────────────────────────────────────────────────────────

    async def list_tokens(self, page: int = 0, size: int = 10) -> dict:
        """列出当前用户的令牌。"""
        return await self._get("/api/token/", {"p": page, "size": size})

    async def search_tokens(self, keyword: str) -> dict:
        """搜索令牌。"""
        return await self._get("/api/token/search", {"keyword": keyword})

    async def get_token(self, token_id: int) -> dict:
        """获取令牌详情。"""
        return await self._get(f"/api/token/{token_id}")

    async def get_token_key(self, token_id: int) -> dict:
        """获取令牌完整密钥 (sk-xxx)。"""
        return await self._post(f"/api/token/{token_id}/key")

    async def create_token(
        self,
        name: str,
        remain_quota: int = 0,
        unlimited_quota: bool = False,
        expired_time: int = -1,
        group: str = "",
        model_limits_enabled: bool = False,
        model_limits: list[str] | None = None,
        allow_ips: str = "",
        count: int = 1,
    ) -> dict:
        """创建新令牌。"""
        body: dict[str, Any] = {
            "name": name,
            "remain_quota": remain_quota,
            "unlimited_quota": unlimited_quota,
            "expired_time": expired_time,
            "count": count,
        }
        if group:
            body["group"] = group
        if model_limits_enabled:
            body["model_limits_enabled"] = True
            body["model_limits"] = model_limits or []
        if allow_ips:
            body["allow_ips"] = allow_ips
        return await self._post("/api/token/", body)

    async def update_token(self, token_id: int, **kwargs) -> dict:
        """更新令牌 (name, remain_quota, unlimited_quota, expired_time, group, status 等)。"""
        body = {"id": token_id, **kwargs}
        return await self._put("/api/token/", body)

    async def delete_token(self, token_id: int) -> dict:
        """删除令牌。"""
        return await self._delete(f"/api/token/{token_id}")

    # ── 钱包 & 充值 ──────────────────────────────────────────────────────

    async def get_topup_info(self) -> dict:
        """获取充值方式和配置。"""
        return await self._get_with_fallback([
            "/api/user/self/topup/info",
            "/api/user/topup/info",
        ])

    async def request_epay(self, amount: int, payment_method: str = "wxpay") -> dict:
        """发起 Epay 支付，返回支付 URL。payment_method: wxpay / alipay"""
        return await self._post_with_fallback([
            "/api/user/self/pay",
            "/api/user/pay",
        ], {
            "amount": amount,
            "payment_method": payment_method,
        })

    async def request_amount(self, amount: int) -> dict:
        """查询充值金额对应的实付金额。"""
        return await self._post_with_fallback([
            "/api/user/self/amount",
            "/api/user/amount",
        ], {"amount": amount})

    async def request_stripe_amount(self, amount: int) -> dict:
        """查询 Stripe 充值对应的实付金额。"""
        return await self._post_with_fallback([
            "/api/user/self/stripe/amount",
            "/api/user/stripe/amount",
        ], {
            "amount": amount,
            "payment_method": "stripe",
        })

    async def request_stripe_pay(
        self,
        amount: int,
        success_url: str = "",
        cancel_url: str = "",
    ) -> dict:
        """发起 Stripe 支付，返回 pay_link。"""
        body: dict[str, Any] = {
            "amount": amount,
            "payment_method": "stripe",
        }
        if success_url:
            body["success_url"] = success_url
        if cancel_url:
            body["cancel_url"] = cancel_url
        return await self._post_with_fallback([
            "/api/user/self/stripe/pay",
            "/api/user/stripe/pay",
        ], body)

    async def request_creem_pay(self, product_id: str) -> dict:
        """发起 Creem 支付，返回 checkout_url。"""
        return await self._post_with_fallback([
            "/api/user/self/creem/pay",
            "/api/user/creem/pay",
        ], {
            "product_id": product_id,
            "payment_method": "creem",
        })

    async def redeem_topup(self, key: str) -> dict:
        """使用兌換碼充值。"""
        return await self._post_with_fallback([
            "/api/user/self/topup",
            "/api/user/topup",
        ], {"key": key})

    async def get_topup_history(
        self,
        page: int = 0,
        size: int = 10,
        keyword: str = "",
    ) -> dict:
        """获取充值历史。"""
        params: dict[str, Any] = {
            "p": max(0, int(page)),
            "size": max(1, min(int(size), 100)),
        }
        if str(keyword or "").strip():
            params["keyword"] = str(keyword).strip()
        return await self._get_with_fallback([
            "/api/user/self/topup/self",
            "/api/user/topup/self",
        ], params)

    async def get_quota_data(self) -> dict:
        """获取用户额度数据。"""
        return await self._get("/api/data/self")

    async def get_log_stat(self) -> dict:
        """获取用户使用统计。"""
        return await self._get("/api/log/self/stat")

    async def get_logs(self, page: int = 0, size: int = 10) -> dict:
        """获取用户日志。"""
        return await self._get("/api/log/self", {"p": page, "size": size})

    # ── 订阅 ──────────────────────────────────────────────────────────────

    async def get_subscription_plans(self) -> dict:
        """获取可用订阅套餐。"""
        return await self._get("/api/subscription/plans")

    async def get_my_subscriptions(self) -> dict:
        """获取我的订阅。"""
        return await self._get("/api/subscription/self")

    async def set_billing_preference(self, preference: str) -> dict:
        """设置计费偏好 (wallet / subscription)。"""
        return await self._put("/api/subscription/self/preference", {"preference": preference})

    # ── 签到 & 邀请 ──────────────────────────────────────────────────────

    async def checkin(self) -> dict:
        """每日签到。"""
        return await self._post("/api/user/self/checkin")

    async def get_checkin_status(self) -> dict:
        """获取签到状态。"""
        return await self._get("/api/user/self/checkin")

    async def get_aff_info(self) -> dict:
        """获取邀请信息。"""
        return await self._get("/api/user/self/aff")

    async def transfer_aff_quota(self) -> dict:
        """划转邀请奖励到余额。"""
        return await self._post("/api/user/self/aff_transfer")

    # ── 模型 & 定价 ──────────────────────────────────────────────────────

    async def get_models(self) -> dict:
        """获取所有可用模型 (dashboard)。"""
        return await self._get("/api/models")

    async def get_pricing(self) -> dict:
        """获取模型定价。"""
        return await self._get("/api/pricing")

    async def get_groups_public(self) -> dict:
        """获取公开分组信息。"""
        return await self._get("/api/user/groups")
