import { useState, useEffect, useCallback, useMemo } from "react";
import {
  Card, CardBody, Input, Button, Select, SelectItem,
  Switch, Progress, Textarea, Divider, Tooltip, Chip,
} from "@heroui/react";
import {
  Rocket, ChevronRight, ChevronLeft, Cpu, Zap,
  Shield, Volume2, Cookie, Check, Download, RefreshCw, Sparkles, QrCode,
} from "lucide-react";
import { ModelCombobox } from "../../components/model-combobox";
import {
  BASE, SETUP_DRAFT_KEY,
  PROVIDERS, ENDPOINT_TYPE_OPTIONS,
  DEFAULT_IMAGE_GEN_PROVIDER, DEFAULT_SETUP_PROVIDER,
  IMAGE_GEN_DEFAULTS, IMAGE_GEN_MODEL_HINTS,
  IMAGE_MODEL_OPTIONS, MODEL_OPTIONS, STEPS,
  allModelOptions, uniqueModelOptions,
  normalizeImageGenBaseRoot, shouldResetImageGenBaseUrl, getImageGenApiKeyDescription,
  type SetupDraft, type CookieCapabilities, type CookieLoginGuide,
} from "./setup-constants";

export default function SetupPage() {
  const [step, setStep] = useState(0);
  const [saving, setSaving] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState("");

  // Step 1: API
  const [provider, setProvider] = useState(DEFAULT_SETUP_PROVIDER);
  const [endpointType, setEndpointType] = useState(PROVIDERS[0]?.endpointType || "openai");
  const [model, setModel] = useState(PROVIDERS[0]?.model || "gpt-5-codex");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [testingApi, setTestingApi] = useState(false);
  const [testApiResult, setTestApiResult] = useState<{ ok: boolean; msg: string; latencyMs?: number } | null>(null);
  const [showApiAdvanced, setShowApiAdvanced] = useState(false);

  // Step 2: Features
  const [botName, setBotName] = useState("YuKiKo");
  const [search, setSearch] = useState(true);
  const [image, setImage] = useState(true);
  const [markdown, setMarkdown] = useState(true);

  // Step 3: Admin & Output
  const [superAdmin, setSuperAdmin] = useState("");
  const [verbosity, setVerbosity] = useState("medium");
  const [tokenSaving, setTokenSaving] = useState(false);
  const [showOutputAdvanced, setShowOutputAdvanced] = useState(false);

  // Step 4: Music
  const [musicEnable, setMusicEnable] = useState(true);
  const [musicApi, setMusicApi] = useState("http://mc.alger.fun/api");

  // Image Gen
  const [imageGenEnable, setImageGenEnable] = useState(true);
  const [imageGenProvider, setImageGenProvider] = useState(DEFAULT_IMAGE_GEN_PROVIDER);
  const [imageGenApiKey, setImageGenApiKey] = useState("");
  const [imageGenBaseUrl, setImageGenBaseUrl] = useState(IMAGE_GEN_DEFAULTS[DEFAULT_IMAGE_GEN_PROVIDER]?.baseUrl || "");
  const [imageGenModel, setImageGenModel] = useState(IMAGE_GEN_DEFAULTS[DEFAULT_IMAGE_GEN_PROVIDER]?.model || "gpt-image-1");
  const [imageGenSize, setImageGenSize] = useState("1024x1024");
  const [testingImageGen, setTestingImageGen] = useState(false);
  const [testImageGenResult, setTestImageGenResult] = useState<{ ok: boolean; msg: string; imageUrl?: string } | null>(null);
  const [showImageGenAdvanced, setShowImageGenAdvanced] = useState(false);

  // Step 5: Cookies
  const [biliSessdata, setBiliSessdata] = useState("");
  const [biliBiliJct, setBiliBiliJct] = useState("");
  const [douyinCookie, setDouyinCookie] = useState("");
  const [kuaishouCookie, setKuaishouCookie] = useState("");
  const [qzoneCookie, setQzoneCookie] = useState("");
  const [extracting, setExtracting] = useState<string | null>(null);
  const [openingLogin, setOpeningLogin] = useState<string | null>(null);
  const [cookieStatus, setCookieStatus] = useState<Record<string, { ok: boolean; msg: string }>>({});
  const [loginGuides, setLoginGuides] = useState<Record<string, CookieLoginGuide>>({});
  const [cookieBrowser, setCookieBrowser] = useState("edge");
  const [cookieAllowClose, setCookieAllowClose] = useState(false);
  const [smartExtracting, setSmartExtracting] = useState(false);
  const [smartMsg, setSmartMsg] = useState("");
  const [cookieCapabilities, setCookieCapabilities] = useState<CookieCapabilities | null>(null);
  const [cookieCapabilitiesError, setCookieCapabilitiesError] = useState("");
  const [biliQrSessionId, setBiliQrSessionId] = useState("");
  const [biliQrImage, setBiliQrImage] = useState("");
  const [biliQrUrl, setBiliQrUrl] = useState("");
  const [biliQrStatus, setBiliQrStatus] = useState("");
  const [biliQrLoading, setBiliQrLoading] = useState(false);
  const [showCookieManualEditors, setShowCookieManualEditors] = useState(false);
  const [showCookieAdvanced, setShowCookieAdvanced] = useState(false);
  const mainModelOptions = useMemo(
    () => uniqueModelOptions(MODEL_OPTIONS[provider], allModelOptions(MODEL_OPTIONS)),
    [provider],
  );
  const imageModelOptions = useMemo(
    () => uniqueModelOptions(IMAGE_MODEL_OPTIONS[imageGenProvider], allModelOptions(IMAGE_MODEL_OPTIONS)),
    [imageGenProvider],
  );

  // Auto-poll smart extraction results
  const pollSmartResult = useCallback(async () => {
    for (let i = 0; i < 60; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      try {
        const res = await fetch(`${BASE}/setup/smart-extract-result`);
        const data = await res.json();
        if (data.status === "done" && data.data) {
          const foundPlatforms = Object.keys(data.data || {}).filter((k) => Boolean((data.data as Record<string, unknown>)[k]));
          if (foundPlatforms.length === 0) {
            const browser = String(data?.meta?.browser || cookieBrowser || "edge");
            const mode = String(data?.meta?.mode || "no_restart");
            const restartHint = mode === "restart"
              ? "（已尝试自动关闭重试）"
              : "（未执行自动关闭重试）";
            setSmartMsg(`提取完成但未命中可用 Cookie：当前 ${browser} 配置文件可能未登录目标站点 ${restartHint}`);
            setSmartExtracting(false);
            return;
          }
          if (data.data.bilibili) {
            setBiliSessdata(data.data.bilibili.sessdata || "");
            setBiliBiliJct(data.data.bilibili.bili_jct || "");
            setCookieStatus((p) => ({ ...p, bilibili: { ok: true, msg: "已获取" } }));
          }
          if (data.data.douyin) {
            setDouyinCookie(data.data.douyin.cookie || "");
            setCookieStatus((p) => ({ ...p, douyin: { ok: true, msg: "已获取" } }));
          }
          if (data.data.kuaishou) {
            setKuaishouCookie(data.data.kuaishou.cookie || "");
            setCookieStatus((p) => ({ ...p, kuaishou: { ok: true, msg: "已获取" } }));
          }
          if (data.data.qzone) {
            setQzoneCookie(data.data.qzone.cookie || "");
            setCookieStatus((p) => ({ ...p, qzone: { ok: true, msg: "已获取" } }));
          }
          const mode = String(data?.meta?.mode || "no_restart");
          setSmartMsg(mode === "restart" ? "提取完成（已自动关闭重试）" : "提取完成");
          setSmartExtracting(false);
          setStep(4); // Jump to cookie step
          // Backward compatibility: clear legacy pending URL param
          window.history.replaceState({}, "", window.location.pathname);
          return;
        } else if (data.status === "error") {
          setSmartMsg(data.message || "提取失败");
          setSmartExtracting(false);
          return;
        }
      } catch { /* server might be restarting */ }
    }
    setSmartMsg("轮询超时");
    setSmartExtracting(false);
  }, []);

  const loadCookieCapabilities = useCallback(async () => {
    try {
      const res = await fetch(`${BASE}/setup/cookie-capabilities`);
      const data = await res.json();
      if (data?.ok && data?.data) {
        const caps = data.data as CookieCapabilities;
        setCookieCapabilities(caps);
        setCookieCapabilitiesError("");
        const recommended = String(caps?.browsers?.recommended || "").trim();
        if (recommended) setCookieBrowser(recommended);
        return;
      }
      setCookieCapabilitiesError(String(data?.message || "获取 Cookie 能力失败"));
    } catch (e: unknown) {
      setCookieCapabilitiesError(e instanceof Error ? e.message : "获取 Cookie 能力失败");
    }
  }, []);

  const startBilibiliQr = useCallback(async () => {
    setBiliQrLoading(true);
    setBiliQrStatus("正在生成二维码...");
    try {
      if (biliQrSessionId) {
        await fetch(`${BASE}/setup/bilibili-qr/cancel`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: biliQrSessionId }),
        }).catch(() => undefined);
      }
      const res = await fetch(`${BASE}/setup/bilibili-qr/start`, { method: "POST" });
      const data = await res.json();
      if (!res.ok || !data?.ok) {
        setBiliQrStatus(String(data?.message || "二维码生成失败"));
        setBiliQrLoading(false);
        return;
      }
      const sid = String(data.session_id || "");
      setBiliQrSessionId(sid);
      setBiliQrImage(String(data.qr_image_data_uri || ""));
      setBiliQrUrl(String(data.qr_url || ""));
      setBiliQrStatus(String(data.message || "请使用 B站 App 扫码"));
      setBiliQrLoading(false);
    } catch (e: unknown) {
      setBiliQrStatus(e instanceof Error ? e.message : "二维码生成失败");
      setBiliQrLoading(false);
    }
  }, [biliQrSessionId]);

  useEffect(() => {
    let timer: number | undefined;
    if (!biliQrSessionId) return () => {
      if (timer) window.clearTimeout(timer);
    };

    const poll = async () => {
      try {
        const res = await fetch(`${BASE}/setup/bilibili-qr/status?session_id=${encodeURIComponent(biliQrSessionId)}`);
        const data = await res.json();
        const status = String(data?.status || "");
        if (status === "done" && data?.data) {
          setBiliSessdata(String(data.data.sessdata || ""));
          setBiliBiliJct(String(data.data.bili_jct || ""));
          setCookieStatus((prev) => ({ ...prev, bilibili: { ok: true, msg: "扫码登录成功" } }));
          setBiliQrStatus("扫码登录成功，已回填 Cookie");
          setBiliQrSessionId("");
          return;
        }
        if (status === "expired" || status === "error") {
          setCookieStatus((prev) => ({ ...prev, bilibili: { ok: false, msg: String(data?.message || "二维码已失效") } }));
          setBiliQrStatus(String(data?.message || "二维码已失效"));
          setBiliQrSessionId("");
          return;
        }
        setBiliQrStatus(String(data?.message || "等待扫码..."));
      } catch {
        // 服务可能重启，继续重试
      }
      timer = window.setTimeout(poll, 1800);
    };
    timer = window.setTimeout(poll, 1200);
    return () => {
      if (timer) window.clearTimeout(timer);
    };
  }, [biliQrSessionId]);

  useEffect(() => () => {
    if (!biliQrSessionId) return;
    fetch(`${BASE}/setup/bilibili-qr/cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: biliQrSessionId }),
    }).catch(() => undefined);
  }, [biliQrSessionId]);

  useEffect(() => {
    loadCookieCapabilities();
  }, [loadCookieCapabilities]);

  useEffect(() => {
    // Restore draft if exists.
    try {
      const raw = window.sessionStorage.getItem(SETUP_DRAFT_KEY);
      if (!raw) return;
      const draft = JSON.parse(raw) as SetupDraft;
      const canRestoreProviderDraft = !draft.provider || PROVIDERS.some((item) => item.value === draft.provider);
      const canRestoreImageGenDraft =
        typeof draft.imageGenProvider !== "string"
        || (draft.imageGenProvider in IMAGE_GEN_DEFAULTS && draft.imageGenProvider !== "skiapi");
      if (typeof draft.step === "number") setStep(Math.max(0, Math.min(draft.step, STEPS.length - 1)));
      if (draft.provider && canRestoreProviderDraft) setProvider(draft.provider);
      if (canRestoreProviderDraft && draft.endpointType) setEndpointType(draft.endpointType);
      if (canRestoreProviderDraft && draft.model) setModel(draft.model);
      if (canRestoreProviderDraft && typeof draft.apiKey === "string") setApiKey(draft.apiKey);
      if (canRestoreProviderDraft && typeof draft.baseUrl === "string") setBaseUrl(draft.baseUrl);
      if (typeof draft.botName === "string") setBotName(draft.botName);
      if (typeof draft.search === "boolean") setSearch(draft.search);
      if (typeof draft.image === "boolean") setImage(draft.image);
      if (typeof draft.markdown === "boolean") setMarkdown(draft.markdown);
      if (typeof draft.superAdmin === "string") setSuperAdmin(draft.superAdmin);
      if (draft.verbosity) setVerbosity(draft.verbosity);
      if (typeof draft.tokenSaving === "boolean") setTokenSaving(draft.tokenSaving);
      if (typeof draft.musicEnable === "boolean") setMusicEnable(draft.musicEnable);
      if (typeof draft.musicApi === "string") setMusicApi(draft.musicApi);
      if (typeof draft.imageGenEnable === "boolean") setImageGenEnable(draft.imageGenEnable);
      if (typeof draft.imageGenProvider === "string" && canRestoreImageGenDraft) {
        setImageGenProvider(draft.imageGenProvider);
      }
      if (canRestoreImageGenDraft && typeof draft.imageGenApiKey === "string") setImageGenApiKey(draft.imageGenApiKey);
      if (canRestoreImageGenDraft && typeof draft.imageGenBaseUrl === "string") setImageGenBaseUrl(draft.imageGenBaseUrl);
      if (canRestoreImageGenDraft && typeof draft.imageGenModel === "string") setImageGenModel(draft.imageGenModel);
      if (canRestoreImageGenDraft && typeof draft.imageGenSize === "string") setImageGenSize(draft.imageGenSize);
      if (typeof draft.biliSessdata === "string") setBiliSessdata(draft.biliSessdata);
      if (typeof draft.biliBiliJct === "string") setBiliBiliJct(draft.biliBiliJct);
      if (typeof draft.douyinCookie === "string") setDouyinCookie(draft.douyinCookie);
      if (typeof draft.kuaishouCookie === "string") setKuaishouCookie(draft.kuaishouCookie);
      if (typeof draft.qzoneCookie === "string") setQzoneCookie(draft.qzoneCookie);
      if (draft.cookieBrowser) setCookieBrowser(draft.cookieBrowser);
      if (typeof draft.cookieAllowClose === "boolean") setCookieAllowClose(draft.cookieAllowClose);
    } catch {
      // Ignore broken draft payload
    }
  }, []);

  useEffect(() => {
    const draft: SetupDraft = {
      step,
      provider,
      endpointType,
      model,
      apiKey,
      baseUrl,
      botName,
      search,
      image,
      markdown,
      superAdmin,
      verbosity,
      tokenSaving,
      musicEnable,
      musicApi,
      imageGenEnable,
      imageGenProvider,
      imageGenApiKey,
      imageGenBaseUrl,
      imageGenModel,
      imageGenSize,
      biliSessdata,
      biliBiliJct,
      douyinCookie,
      kuaishouCookie,
      qzoneCookie,
      cookieBrowser,
      cookieAllowClose,
    };
    try {
      window.sessionStorage.setItem(SETUP_DRAFT_KEY, JSON.stringify(draft));
    } catch {
      // Ignore quota/storage errors
    }
  }, [
    step,
    provider,
    endpointType,
    model,
    apiKey,
    baseUrl,
    botName,
    search,
    image,
    markdown,
    superAdmin,
    verbosity,
    tokenSaving,
    musicEnable,
    musicApi,
    imageGenEnable,
    imageGenProvider,
    imageGenApiKey,
    imageGenBaseUrl,
    imageGenModel,
    imageGenSize,
    biliSessdata,
    biliBiliJct,
    douyinCookie,
    kuaishouCookie,
    qzoneCookie,
    cookieBrowser,
    cookieAllowClose,
  ]);

  useEffect(() => {
    setTestApiResult(null);
  }, [provider, endpointType, model, apiKey, baseUrl]);

  useEffect(() => {
    setTestImageGenResult(null);
  }, [imageGenProvider, imageGenModel, imageGenApiKey, imageGenBaseUrl]);

  useEffect(() => {
    // Backward compatibility for old "pending=1" flow.
    const params = new URLSearchParams(window.location.search);
    if (params.get("pending") === "1") {
      setSmartExtracting(true);
      setSmartMsg("正在提取 Cookie...");
      setStep(4);
      pollSmartResult();
    }
  }, [pollSmartResult]);

  const handleSmartExtract = async () => {
    setSmartExtracting(true);
    setSmartMsg(
      cookieAllowClose
        ? "正在提取 Cookie（优先不关闭浏览器，失败后会自动关闭重试）..."
        : "正在提取 Cookie（不关闭浏览器，可能弹出管理员授权）...",
    );
    try {
      const res = await fetch(`${BASE}/setup/smart-extract`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          browser: cookieBrowser,
          allow_close: cookieAllowClose,
        }),
      });
      const data = await res.json();
      if (!data.ok) {
        setSmartMsg(data.message || "启动失败");
        setSmartExtracting(false);
        return;
      }
      pollSmartResult();
    } catch {
      setSmartMsg("请求失败，请重试");
      setSmartExtracting(false);
    }
  };

  const extractCookie = async (platform: string) => {
    setExtracting(platform);
    setCookieStatus((prev) => ({ ...prev, [platform]: { ok: false, msg: "提取中..." } }));
    try {
      const res = await fetch(`${BASE}/setup/extract-cookie`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ platform, browser: cookieBrowser, allow_close: cookieAllowClose }),
      });
      const data = await res.json();
      if (!data.ok) {
        setCookieStatus((prev) => ({ ...prev, [platform]: { ok: false, msg: data.message } }));
        return;
      }
      const sourceHint = (() => {
        const sources = data?.meta?.sources;
        if (!sources || typeof sources !== "object") return "";
        const uniq = Array.from(new Set(Object.values(sources).map((v) => String(v || "")).filter(Boolean)));
        return uniq.length ? ` (${uniq.join(",")})` : "";
      })();
      if (platform === "bilibili") {
        setBiliSessdata(data.data.sessdata || "");
        setBiliBiliJct(data.data.bili_jct || "");
      } else if (platform === "douyin") {
        setDouyinCookie(data.data.cookie || "");
      } else if (platform === "kuaishou") {
        setKuaishouCookie(data.data.cookie || "");
      } else if (platform === "qzone") {
        setQzoneCookie(data.data.cookie || "");
      }
      setCookieStatus((prev) => ({ ...prev, [platform]: { ok: true, msg: `已获取${sourceHint}` } }));
    } catch (e: unknown) {
      setCookieStatus((prev) => ({ ...prev, [platform]: { ok: false, msg: e instanceof Error ? e.message : "提取失败" } }));
    } finally {
      setExtracting(null);
    }
  };

  const startPlatformLogin = async (platform: string) => {
    setOpeningLogin(platform);
    setCookieStatus((prev) => ({ ...prev, [platform]: { ok: true, msg: "Opening login page..." } }));
    try {
      const res = await fetch(`${BASE}/setup/prepare-login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ platform, browser: cookieBrowser }),
      });
      const data = await res.json();
      if (!res.ok || !data?.ok) {
        setCookieStatus((prev) => ({ ...prev, [platform]: { ok: false, msg: String(data?.message || "Open failed") } }));
        return;
      }
      setLoginGuides((prev) => ({
        ...prev,
        [platform]: {
          message: String(data.message || ""),
          login_url: String(data.login_url || ""),
          after_login_url: String(data.after_login_url || ""),
          instructions: Array.isArray(data.instructions) ? data.instructions.map((item: unknown) => String(item)) : [],
          notes: Array.isArray(data.notes) ? data.notes.map((item: unknown) => String(item)) : [],
        },
      }));
      setCookieStatus((prev) => ({ ...prev, [platform]: { ok: true, msg: "Scan-login opened" } }));
    } catch (e: unknown) {
      setCookieStatus((prev) => ({ ...prev, [platform]: { ok: false, msg: e instanceof Error ? e.message : "Open failed" } }));
    } finally {
      setOpeningLogin(null);
    }
  };

  const renderCookieLoginGuide = (platform: string, fallbackText: string) => {
    const guide = loginGuides[platform];
    return (
      <div className="space-y-1 text-xs text-default-500">
        <p>{guide?.message || fallbackText}</p>
        {guide?.login_url ? (
          <a className="block text-primary underline break-all" href={guide.login_url} target="_blank" rel="noreferrer">
            {guide.login_url}
          </a>
        ) : null}
        {guide?.after_login_url ? (
          <p>After login, confirm the page has reached {guide.after_login_url}</p>
        ) : null}
        {(guide?.instructions || []).map((item, idx) => (
          <p key={`${platform}-instruction-${idx}`}>{idx + 1}. {item}</p>
        ))}
        {(guide?.notes || []).map((item, idx) => (
          <p key={`${platform}-note-${idx}`}>Note: {item}</p>
        ))}
      </div>
    );
  };

  const handleProviderChange = (val: string) => {
    const previousModels = MODEL_OPTIONS[provider] || [];
    const shouldResetModel = !model || previousModels.some((item) => item.value === model);
    setProvider(val);
    const info = PROVIDERS.find((item) => item.value === val);
    setEndpointType(info?.endpointType || "openai");
    const list = MODEL_OPTIONS[val] || [];
    if (shouldResetModel && list.length > 0) {
      setModel(list[0].value);
      return;
    }
    if (shouldResetModel && info) setModel(info.model);
  };

  const handleTestApi = async () => {
    setTestingApi(true);
    setTestApiResult(null);
    try {
      const res = await fetch(`${BASE}/setup/test-api`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider,
          endpoint_type: endpointType,
          model,
          api_key: apiKey,
          base_url: baseUrl,
        }),
      });
      const data = await res.json();
      setTestApiResult({
        ok: Boolean(data?.ok),
        msg: String(data?.message || (data?.ok ? "检测成功" : "检测失败")),
        latencyMs: Number.isFinite(Number(data?.latency_ms)) ? Number(data?.latency_ms) : undefined,
      });
    } catch (e: unknown) {
      setTestApiResult({ ok: false, msg: e instanceof Error ? e.message : "检测请求失败" });
    } finally {
      setTestingApi(false);
    }
  };

  const handleTestImageGen = async () => {
    setTestingImageGen(true);
    setTestImageGenResult(null);
    try {
      const res = await fetch(`${BASE}/setup/test-image-gen`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider: imageGenProvider,
          model: imageGenModel,
          api_key: imageGenApiKey,
          base_url: imageGenBaseUrl,
          size: imageGenSize,
        }),
      });
      const data = await res.json();
      setTestImageGenResult({
        ok: Boolean(data?.ok),
        msg: String(data?.message || (data?.ok ? "生成成功" : "生成失败")),
        imageUrl: data?.image_url,
      });
    } catch (e: unknown) {
      setTestImageGenResult({ ok: false, msg: e instanceof Error ? e.message : "请求失败" });
    } finally {
      setTestingImageGen(false);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setError("");
    try {
      const res = await fetch(`${BASE}/setup/save`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider, endpoint_type: endpointType, model, api_key: apiKey, base_url: baseUrl,
          bot_name: botName, search, image, markdown,
          super_admin_qq: superAdmin, verbosity, token_saving: tokenSaving,
          music: musicEnable, music_api_base: musicApi,
          image_gen_enable: imageGenEnable,
          image_gen_provider: imageGenProvider,
          image_gen_api_key: imageGenApiKey,
          image_gen_base_url: imageGenBaseUrl,
          image_gen_model: imageGenModel,
          image_gen_size: imageGenSize,
          bili_sessdata: biliSessdata, bili_jct: biliBiliJct,
          douyin_cookie: douyinCookie, kuaishou_cookie: kuaishouCookie,
          qzone_cookie: qzoneCookie,
        }),
      });
      const data = await res.json();
      if (data.ok) {
        setDone(true);
        window.sessionStorage.removeItem(SETUP_DRAFT_KEY);
      }
      else setError(data.message || "保存失败");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "请求失败");
    } finally {
      setSaving(false);
    }
  };

  const next = () => setStep((s) => Math.min(s + 1, STEPS.length - 1));
  const prev = () => setStep((s) => Math.max(s - 1, 0));
  const isLast = step === STEPS.length - 1;

  if (done) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-background">
        <Card className="w-full max-w-md shadow-2xl border border-success/20">
          <CardBody className="text-center py-12 gap-4">
            <div className="mx-auto w-16 h-16 rounded-full bg-success/10 flex items-center justify-center">
              <Check size={32} className="text-success" />
            </div>
            <h2 className="text-2xl font-bold">配置完成</h2>
            <p className="text-default-500">
              配置文件已生成，请重新运行 Bot
            </p>
            <code className="text-sm bg-content2 px-3 py-2 rounded-lg">
              python main.py
            </code>
          </CardBody>
        </Card>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background flex flex-col items-center justify-center p-4">
      {/* Header */}
      <div className="text-center mb-6">
        <h1 className="text-3xl font-bold bg-gradient-to-r from-primary to-secondary bg-clip-text text-transparent">
          YuKiKo
        </h1>
        <p className="text-default-400 text-sm mt-1">首次运行配置向导</p>
      </div>

      {/* Progress */}
      <div className="w-full max-w-lg mb-4">
        <div className="flex justify-between mb-2">
          {STEPS.map((s, i) => {
            const Icon = s.icon;
            const active = i === step;
            const completed = i < step;
            return (
              <button
                key={i}
                onClick={() => setStep(i)}
                className={`flex flex-col items-center gap-1 transition-all ${
                  active ? "text-primary scale-110" : completed ? "text-success" : "text-default-400"
                }`}
              >
                <div className={`w-9 h-9 rounded-full flex items-center justify-center border-2 ${
                  active ? "border-primary bg-primary/10" : completed ? "border-success bg-success/10" : "border-default-300"
                }`}>
                  {completed ? <Check size={16} /> : <Icon size={16} />}
                </div>
                <span className="text-[10px] hidden sm:block">{s.title}</span>
              </button>
            );
          })}
        </div>
        <Progress value={((step + 1) / STEPS.length) * 100} size="sm" color="primary" />
      </div>

      {/* Card */}
      <Card className="w-full max-w-lg shadow-2xl border border-divider">
        <CardBody className="gap-5 p-6">
          <h2 className="text-lg font-semibold flex items-center gap-2">
            {(() => { const Icon = STEPS[step].icon; return <Icon size={20} className="text-primary" />; })()}
            {STEPS[step].title}
          </h2>
          <Divider />

          {/* Step 1: API */}
          {step === 0 && (
            <div className="space-y-4">
              <div className="rounded-xl border border-primary/15 bg-primary/5 p-3">
                <p className="text-sm font-medium">先完成基础连接</p>
                <p className="text-xs text-default-500">大多数情况下只需要「提供商 + 模型 + API Key」。端点类型和 Base URL 属于高级兼容项。</p>
              </div>
              <Select
                label="API 提供商"
                selectedKeys={[provider]}
                onSelectionChange={(keys) => {
                  const v = Array.from(keys)[0];
                  if (v) handleProviderChange(String(v));
                }}
                classNames={{ trigger: "bg-content2" }}
              >
                {PROVIDERS.map((p) => (
                  <SelectItem key={p.value}>{p.label}</SelectItem>
                ))}
              </Select>
              <ModelCombobox
                label="模型名称（可搜索 / 可自定义）"
                value={model}
                onValueChange={setModel}
                options={mainModelOptions}
                placeholder="gpt-5.5"
                inputClassNames={{ inputWrapper: "bg-content2" }}
              />
              <Input
                label="API Key"
                description={`留空则从环境变量 ${PROVIDERS.find(p => p.value === provider)?.env || "API_KEY"} 读取`}
                type="password"
                value={apiKey}
                onValueChange={setApiKey}
                classNames={{ inputWrapper: "bg-content2" }}
              />
              <div className="flex items-center justify-between gap-3 rounded-lg border border-default-200/50 bg-content2/40 px-3 py-2">
                <div className="min-w-0">
                  <p className="text-xs text-default-500">连通性检测</p>
                  <p className="truncate text-xs text-default-400">检测 key / base_url / 模型是否可用</p>
                </div>
                <Button size="sm" color="primary" variant="flat" isLoading={testingApi} onPress={handleTestApi}>
                  检测
                </Button>
              </div>
              <div className="flex justify-end">
                <Button size="sm" variant="light" onPress={() => setShowApiAdvanced((prev) => !prev)}>
                  {showApiAdvanced ? "收起高级 API 选项" : "展开高级 API 选项"}
                </Button>
              </div>
              {showApiAdvanced && (
                <div className="space-y-3 rounded-xl border border-warning/20 bg-warning/5 p-3">
                  <Select
                    label="端点类型"
                    selectedKeys={[endpointType]}
                    onSelectionChange={(keys) => {
                      const v = Array.from(keys)[0];
                      if (v) setEndpointType(String(v));
                    }}
                    classNames={{ trigger: "bg-content2" }}
                  >
                    {ENDPOINT_TYPE_OPTIONS.map((ep) => (
                      <SelectItem key={ep.value}>{ep.label}</SelectItem>
                    ))}
                  </Select>
                  <Input
                    label="Base URL（可选，自定义端点）"
                    placeholder="留空使用默认"
                    value={baseUrl}
                    onValueChange={setBaseUrl}
                    classNames={{ inputWrapper: "bg-content2" }}
                  />
                </div>
              )}
              {testApiResult && (
                <Chip size="sm" variant="flat" color={testApiResult.ok ? "success" : "danger"}>
                  {testApiResult.msg}{typeof testApiResult.latencyMs === "number" ? ` (${testApiResult.latencyMs}ms)` : ""}
                </Chip>
              )}
            </div>
          )}

          {/* Step 2: Features */}
          {step === 1 && (
            <div className="space-y-4">
              <Input
                label="Bot 名称"
                value={botName}
                onValueChange={setBotName}
                classNames={{ inputWrapper: "bg-content2" }}
              />
              <div className="grid grid-cols-1 gap-3 pt-1">
                <div className="flex items-center justify-between p-3 rounded-lg bg-content2">
                  <div>
                    <p className="text-sm font-medium">网络搜索</p>
                    <p className="text-xs text-default-400">允许 Bot 搜索互联网获取信息</p>
                  </div>
                  <Switch isSelected={search} onValueChange={setSearch} />
                </div>
                <div className="flex items-center justify-between p-3 rounded-lg bg-content2">
                  <div>
                    <p className="text-sm font-medium">AI 画图</p>
                    <p className="text-xs text-default-400">支持文生图功能</p>
                  </div>
                  <Switch isSelected={image} onValueChange={setImage} />
                </div>
                <div className="flex items-center justify-between p-3 rounded-lg bg-content2">
                  <div>
                    <p className="text-sm font-medium">Markdown 输出</p>
                    <p className="text-xs text-default-400">回复使用 Markdown 格式渲染</p>
                  </div>
                  <Switch isSelected={markdown} onValueChange={setMarkdown} />
                </div>
              </div>
            </div>
          )}

          {/* Step 3: Admin & Output */}
          {step === 2 && (
            <div className="space-y-4">
              <Input
                label="超级管理员 QQ 号"
                description="留空则不启用权限系统"
                value={superAdmin}
                onValueChange={setSuperAdmin}
                classNames={{ inputWrapper: "bg-content2" }}
              />
              <Select
                label="默认输出详略度"
                selectedKeys={[verbosity]}
                onSelectionChange={(keys) => {
                  const v = Array.from(keys)[0];
                  if (v) setVerbosity(String(v));
                }}
                classNames={{ trigger: "bg-content2" }}
              >
                <SelectItem key="verbose">详细 — 完整分析和解释</SelectItem>
                <SelectItem key="medium">中等 — 默认推荐</SelectItem>
                <SelectItem key="brief">简洁 — 抓重点不展开</SelectItem>
                <SelectItem key="minimal">极简 — 一两句话概括</SelectItem>
              </Select>
              <div className="flex justify-end">
                <Button size="sm" variant="light" onPress={() => setShowOutputAdvanced((prev) => !prev)}>
                  {showOutputAdvanced ? "收起高级输出选项" : "展开高级输出选项"}
                </Button>
              </div>
              {showOutputAdvanced && (
                <div className="flex items-center justify-between p-3 rounded-lg bg-content2">
                  <div>
                    <p className="text-sm font-medium">省 Token 模式</p>
                    <p className="text-xs text-default-400">压缩上下文降低 API 成本，可能影响回复质量</p>
                  </div>
                  <Switch isSelected={tokenSaving} onValueChange={setTokenSaving} />
                </div>
              )}
            </div>
          )}

          {/* Step 4: Music & Image Gen */}
          {step === 3 && (
            <div className="space-y-4">
              <div className="flex items-center justify-between p-3 rounded-lg bg-content2">
                <div>
                  <p className="text-sm font-medium">点歌 / 听歌功能</p>
                  <p className="text-xs text-default-400">通过 Alger API 搜索和播放音乐</p>
                </div>
                <Switch isSelected={musicEnable} onValueChange={setMusicEnable} />
              </div>
              {musicEnable && (
                <Input
                  label="音乐 API 地址"
                  value={musicApi}
                  onValueChange={setMusicApi}
                  classNames={{ inputWrapper: "bg-content2" }}
                />
              )}
              <Divider />
              <div className="flex items-center justify-between p-3 rounded-lg bg-content2">
                <div>
                  <p className="text-sm font-medium">AI 图片生成</p>
                    <p className="text-xs text-default-400">支持 OpenAI / Gemini / xAI / Flux / SD / 自定义网关</p>
                </div>
                <Switch isSelected={imageGenEnable} onValueChange={setImageGenEnable} />
              </div>
              {imageGenEnable && (
                <>
                  <Select
                    label="图片生成提供商"
                    selectedKeys={[imageGenProvider]}
                    onSelectionChange={(keys) => {
                      const v = Array.from(keys)[0];
                      if (v) {
                        const nextProvider = String(v);
                        const previousDefaults = IMAGE_GEN_DEFAULTS[imageGenProvider] || IMAGE_GEN_DEFAULTS.custom;
                        const shouldResetModel = !imageGenModel || imageGenModel === previousDefaults.model;
                        setImageGenProvider(nextProvider);
                        const defaults = IMAGE_GEN_DEFAULTS[nextProvider] || IMAGE_GEN_DEFAULTS.custom;
                        if (shouldResetModel) {
                          setImageGenModel(defaults.model);
                        }
                        if (shouldResetImageGenBaseUrl(imageGenBaseUrl)) {
                          setImageGenBaseUrl(defaults.baseUrl);
                        }
                      }
                    }}
                    classNames={{ trigger: "bg-content2" }}
                  >
                    <SelectItem key="openai">OpenAI</SelectItem>
                    <SelectItem key="gemini">Gemini</SelectItem>
                    <SelectItem key="xai">xAI</SelectItem>
                    <SelectItem key="newapi">NEWAPI</SelectItem>
                    <SelectItem key="openrouter">OpenRouter</SelectItem>
                    <SelectItem key="siliconflow">SiliconFlow</SelectItem>
                    <SelectItem key="flux">Flux（SiliconFlow）</SelectItem>
                    <SelectItem key="sd">Stable Diffusion</SelectItem>
                    <SelectItem key="custom">自定义</SelectItem>
                  </Select>
                  <ModelCombobox
                    label="模型名称"
                    description={IMAGE_GEN_MODEL_HINTS[imageGenProvider] || "填写当前提供商实际支持的图片模型名称"}
                    value={imageGenModel}
                    onValueChange={setImageGenModel}
                    placeholder="gpt-image-1"
                    options={imageModelOptions}
                    inputClassNames={{ inputWrapper: "bg-content2" }}
                  />
                  <div className="flex items-center justify-between gap-3 rounded-lg border border-default-200/50 bg-content2/40 px-3 py-2">
                    <div className="min-w-0">
                      <p className="text-xs text-default-500">测试生成</p>
                      <p className="truncate text-xs text-default-400">生成一张可爱的猫娘图片测试配置</p>
                    </div>
                    <Button size="sm" color="secondary" variant="flat" isLoading={testingImageGen} onPress={handleTestImageGen} startContent={<Sparkles size={14} />}>
                      测试
                    </Button>
                  </div>
                  {testImageGenResult && (
                    <div className="space-y-2">
                      <Chip size="sm" variant="flat" color={testImageGenResult.ok ? "success" : "danger"}>
                        {testImageGenResult.msg}
                      </Chip>
                      {testImageGenResult.ok && testImageGenResult.imageUrl && (
                        <div className="rounded-lg overflow-hidden border border-default-200">
                          <img src={testImageGenResult.imageUrl} alt="测试生成" className="w-full h-auto" />
                        </div>
                      )}
                    </div>
                  )}
                  <div className="flex justify-end">
                    <Button size="sm" variant="light" onPress={() => setShowImageGenAdvanced((prev) => !prev)}>
                      {showImageGenAdvanced ? "收起高级图片通道选项" : "展开高级图片通道选项"}
                    </Button>
                  </div>
                  {showImageGenAdvanced && (
                    <div className="space-y-3 rounded-xl border border-warning/20 bg-warning/5 p-3">
                      <Input
                        label="API Key"
                        description={getImageGenApiKeyDescription(imageGenProvider)}
                        type="password"
                        value={imageGenApiKey}
                        onValueChange={setImageGenApiKey}
                        classNames={{ inputWrapper: "bg-content2" }}
                      />
                      <Input
                        label="Base URL（可选）"
                        placeholder={IMAGE_GEN_DEFAULTS[imageGenProvider]?.baseUrl || "留空使用默认"}
                        value={imageGenBaseUrl}
                        onValueChange={setImageGenBaseUrl}
                        classNames={{ inputWrapper: "bg-content2" }}
                      />
                      <Input
                        label="默认图片尺寸"
                        value={imageGenSize}
                        onValueChange={setImageGenSize}
                        placeholder="1024x1024"
                        classNames={{ inputWrapper: "bg-content2" }}
                      />
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          {/* Step 5: Cookies */}
          {step === 4 && (
            <div className="space-y-4">
              {/* Smart Extract Banner */}
              <div className="p-3 rounded-xl bg-primary/5 border border-primary/20 space-y-2">
                <div className="flex items-center justify-between">
                  <div className="flex-1">
                    <p className="text-sm font-medium">一键智能提取</p>
                    <p className="text-xs text-default-400">
                      不关闭浏览器，必要时请求管理员权限读取 Cookie（推荐）
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <div className="w-28">
                      <Select
                        size="sm"
                        label="浏览器"
                        selectedKeys={[cookieBrowser]}
                        onSelectionChange={(keys) => {
                          const v = Array.from(keys)[0];
                          if (v) setCookieBrowser(String(v));
                        }}
                        classNames={{ trigger: "bg-content1" }}
                      >
                        <SelectItem key="edge">Edge</SelectItem>
                        <SelectItem key="chrome">Chrome</SelectItem>
                        <SelectItem key="brave">Brave</SelectItem>
                        <SelectItem key="chromium">Chromium</SelectItem>
                        <SelectItem key="firefox">Firefox</SelectItem>
                      </Select>
                    </div>
                    <Button
                      size="sm" variant="shadow" color="primary" radius="full"
                      startContent={<RefreshCw size={14} />}
                      isLoading={smartExtracting}
                      onPress={handleSmartExtract}
                    >
                      智能提取
                    </Button>
                  </div>
                </div>
                {smartMsg && (
                  <Chip size="sm" variant="flat" color={smartMsg.includes("完成") ? "success" : smartMsg.includes("失败") || smartMsg.includes("超时") ? "danger" : "warning"}>
                    {smartMsg}
                  </Chip>
                )}
                {cookieCapabilitiesError && (
                  <Chip size="sm" variant="flat" color="danger">
                    Cookie 能力检测失败：{cookieCapabilitiesError}
                  </Chip>
                )}
                {!cookieCapabilitiesError && cookieCapabilities?.notices?.map((note, idx) => (
                  <Chip key={`${idx}-${note}`} size="sm" variant="flat" color="warning">
                    {note}
                  </Chip>
                ))}
              </div>

              <Divider />

              <div className="flex items-center justify-between">
                <p className="text-xs text-default-400">
                  或单独提取（优先无关闭策略，失败后再考虑管理员/自动关闭重试）
                </p>
                <div className="flex flex-wrap items-center gap-2">
                  <Button size="sm" variant="light" onPress={() => setShowCookieAdvanced((prev) => !prev)}>
                    {showCookieAdvanced ? "收起提取策略" : "展开提取策略"}
                  </Button>
                  <Button size="sm" variant="light" onPress={() => setShowCookieManualEditors((prev) => !prev)}>
                    {showCookieManualEditors ? "收起手动编辑" : "展开手动编辑"}
                  </Button>
                  <Button
                    size="sm" variant="flat" color="default" radius="full"
                    startContent={<Download size={14} />}
                    isLoading={extracting !== null}
                    onPress={async () => {
                      for (const p of ["bilibili", "douyin", "kuaishou", "qzone"]) {
                        await extractCookie(p);
                      }
                    }}
                  >
                    逐个尝试
                  </Button>
                </div>
              </div>
              {showCookieAdvanced && (
                <div className="flex items-center justify-end gap-2 rounded-xl border border-warning/20 bg-warning/5 px-3 py-2">
                  <span className="text-xs text-default-400">失败时允许自动关闭浏览器重试</span>
                  <Switch size="sm" isSelected={cookieAllowClose} onValueChange={setCookieAllowClose} />
                </div>
              )}

              {/* Bilibili */}
              <div className="space-y-2 rounded-xl bg-content2/50 p-3">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">Bilibili</span>
                    {cookieStatus.bilibili && (
                      <Chip size="sm" variant="flat" color={cookieStatus.bilibili.ok ? "success" : "danger"}>
                        {cookieStatus.bilibili.msg}
                      </Chip>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    <Button
                      size="sm"
                      variant="flat"
                      color="secondary"
                      radius="full"
                      isLoading={biliQrLoading}
                      onPress={startBilibiliQr}
                    >
                      QR Login
                    </Button>
                    <Button
                      size="sm"
                      variant="flat"
                      color="primary"
                      radius="full"
                      startContent={<Download size={14} />}
                      isLoading={extracting === "bilibili"}
                      onPress={() => extractCookie("bilibili")}
                    >
                      Extract Cookie
                    </Button>
                  </div>
                </div>
                {biliQrStatus && (
                  <Chip size="sm" variant="flat" color={biliQrSessionId ? "warning" : (cookieStatus.bilibili?.ok ? "success" : "default")}>
                    {biliQrStatus}
                  </Chip>
                )}
                {biliQrImage && biliQrSessionId && (
                  <div className="w-fit rounded-lg border border-default-200 bg-content1 p-3">
                    <img src={biliQrImage} alt="Bilibili QR code" className="h-44 w-44" />
                    {biliQrUrl && (
                      <a
                        className="mt-2 block text-xs text-primary underline break-all"
                        href={biliQrUrl}
                        target="_blank"
                        rel="noreferrer"
                      >
                        QR link (use this if the image does not render)
                      </a>
                    )}
                  </div>
                )}
                {showCookieManualEditors && (
                  <>
                    <Input
                      label="SESSDATA"
                      size="sm"
                      value={biliSessdata}
                      onValueChange={setBiliSessdata}
                      placeholder="Auto fill or paste manually"
                      classNames={{ inputWrapper: "bg-content1" }}
                    />
                    <Input
                      label="bili_jct"
                      size="sm"
                      value={biliBiliJct}
                      onValueChange={setBiliBiliJct}
                      placeholder="Auto fill or paste manually"
                      classNames={{ inputWrapper: "bg-content1" }}
                    />
                  </>
                )}
              </div>

              {/* Douyin */}
              <div className="space-y-2 rounded-xl bg-content2/50 p-3">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">Douyin</span>
                    {cookieStatus.douyin && (
                      <Chip size="sm" variant="flat" color={cookieStatus.douyin.ok ? "success" : "danger"}>
                        {cookieStatus.douyin.msg}
                      </Chip>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    <Button
                      size="sm"
                      variant="flat"
                      color="secondary"
                      radius="full"
                      startContent={<QrCode size={14} />}
                      isLoading={openingLogin === "douyin"}
                      onPress={() => startPlatformLogin("douyin")}
                    >
                      Open Scan Login
                    </Button>
                    <Button
                      size="sm"
                      variant="flat"
                      color="primary"
                      radius="full"
                      startContent={<Download size={14} />}
                      isLoading={extracting === "douyin"}
                      onPress={() => extractCookie("douyin")}
                    >
                      Extract Cookie
                    </Button>
                  </div>
                </div>
                {renderCookieLoginGuide("douyin", "Open Douyin's official login page, finish scan login, then come back here to extract cookies from the same browser.")}
                {showCookieManualEditors && (
                  <Textarea
                    label="Cookie"
                    size="sm"
                    minRows={1}
                    maxRows={2}
                    value={douyinCookie}
                    onValueChange={setDouyinCookie}
                    placeholder="Auto fill or paste manually"
                    classNames={{ inputWrapper: "bg-content1" }}
                  />
                )}
              </div>

              {/* Kuaishou */}
              <div className="space-y-2 rounded-xl bg-content2/50 p-3">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">Kuaishou</span>
                    {cookieStatus.kuaishou && (
                      <Chip size="sm" variant="flat" color={cookieStatus.kuaishou.ok ? "success" : "danger"}>
                        {cookieStatus.kuaishou.msg}
                      </Chip>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    <Button
                      size="sm"
                      variant="flat"
                      color="secondary"
                      radius="full"
                      startContent={<QrCode size={14} />}
                      isLoading={openingLogin === "kuaishou"}
                      onPress={() => startPlatformLogin("kuaishou")}
                    >
                      Open Scan Login
                    </Button>
                    <Button
                      size="sm"
                      variant="flat"
                      color="primary"
                      radius="full"
                      startContent={<Download size={14} />}
                      isLoading={extracting === "kuaishou"}
                      onPress={() => extractCookie("kuaishou")}
                    >
                      Extract Cookie
                    </Button>
                  </div>
                </div>
                {renderCookieLoginGuide("kuaishou", "Open Kuaishou's official login page, finish scan login, then come back here to extract cookies from the same browser.")}
                {showCookieManualEditors && (
                  <Textarea
                    label="Cookie"
                    size="sm"
                    minRows={1}
                    maxRows={2}
                    value={kuaishouCookie}
                    onValueChange={setKuaishouCookie}
                    placeholder="Auto fill or paste manually"
                    classNames={{ inputWrapper: "bg-content1" }}
                  />
                )}
              </div>

              {/* QZone */}
              <div className="space-y-2 rounded-xl bg-content2/50 p-3">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">QZone</span>
                    {cookieStatus.qzone && (
                      <Chip size="sm" variant="flat" color={cookieStatus.qzone.ok ? "success" : "danger"}>
                        {cookieStatus.qzone.msg}
                      </Chip>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    <Button
                      size="sm"
                      variant="flat"
                      color="secondary"
                      radius="full"
                      startContent={<QrCode size={14} />}
                      isLoading={openingLogin === "qzone"}
                      onPress={() => startPlatformLogin("qzone")}
                    >
                      Open Scan Login
                    </Button>
                    <Button
                      size="sm"
                      variant="flat"
                      color="primary"
                      radius="full"
                      startContent={<Download size={14} />}
                      isLoading={extracting === "qzone"}
                      onPress={() => extractCookie("qzone")}
                    >
                      Extract Cookie
                    </Button>
                  </div>
                </div>
                {renderCookieLoginGuide("qzone", "Open QZone's official login page, finish scan login, confirm the browser reaches your own QZone home page, then extract cookies.")}
                {showCookieManualEditors && (
                  <Textarea
                    label="Cookie"
                    size="sm"
                    minRows={1}
                    maxRows={2}
                    value={qzoneCookie}
                    onValueChange={setQzoneCookie}
                    placeholder="p_skey=xxx; uin=xxx; skey=xxx"
                    classNames={{ inputWrapper: "bg-content1" }}
                  />
                )}
              </div>
            </div>
          )}
          {error && <p className="text-danger text-sm">{error}</p>}

          {/* Navigation */}
          <div className="flex justify-between pt-2">
            <Button
              variant="flat"
              startContent={<ChevronLeft size={16} />}
              onPress={prev}
              isDisabled={step === 0}
            >
              上一步
            </Button>
            {isLast ? (
              <Button
                color="primary"
                endContent={<Rocket size={16} />}
                isLoading={saving}
                onPress={handleSave}
              >
                完成配置
              </Button>
            ) : (
              <Button
                color="primary"
                endContent={<ChevronRight size={16} />}
                onPress={next}
              >
                下一步
              </Button>
            )}
          </div>
        </CardBody>
      </Card>

      <p className="text-[11px] text-default-400 mt-4">
        配置保存后可随时通过 WebUI 或 /yukibot 命令修改
      </p>
    </div>
  );
}
