import { useCallback, useEffect, useState } from "react";
import { Card, CardBody, CardHeader, Button, Textarea, Select, SelectItem, Chip, Switch } from "@heroui/react";
import { RefreshCw, Save, AlertCircle, CheckCircle2, QrCode } from "lucide-react";
import { api } from "../api/client";

type Platform = "bilibili" | "douyin" | "kuaishou" | "qzone";
type Browser = "edge" | "chrome" | "firefox" | "brave" | "chromium";

type CookieCapabilities = {
  browsers?: {
    recommended?: string;
    installed?: string[];
    scan_login_supported?: string[];
  };
  notices?: string[];
  platforms?: {
    bilibili?: { qr_scan?: boolean; browser_extract?: boolean; browser_scan_login?: boolean };
    douyin?: { browser_extract?: boolean; browser_scan_login?: boolean };
    kuaishou?: { browser_extract?: boolean; browser_scan_login?: boolean };
    qzone?: { browser_extract?: boolean; browser_scan_login?: boolean };
  };
};

type LoginGuide = {
  message?: string;
  login_url?: string;
  after_login_url?: string;
  instructions?: string[];
  notes?: string[];
};

const jsonHeaders = () => ({
  "Content-Type": "application/json",
});

export default function CookiesPage() {
  const [platform, setPlatform] = useState<Platform>("bilibili");
  const [browser, setBrowser] = useState<Browser>("edge");
  const [allowClose, setAllowClose] = useState(false);
  const [cookie, setCookie] = useState("");
  const [loading, setLoading] = useState(false);
  const [openingLogin, setOpeningLogin] = useState(false);
  const [msg, setMsg] = useState("");
  const [caps, setCaps] = useState<CookieCapabilities | null>(null);
  const [loginGuide, setLoginGuide] = useState<LoginGuide | null>(null);

  const [biliQrSessionId, setBiliQrSessionId] = useState("");
  const [biliQrImage, setBiliQrImage] = useState("");
  const [biliQrUrl, setBiliQrUrl] = useState("");
  const [biliQrStatus, setBiliQrStatus] = useState("");
  const [biliQrLoading, setBiliQrLoading] = useState(false);

  const loadCapabilities = useCallback(async () => {
    try {
      const res = await fetch("/api/webui/cookies/capabilities");
      const data = await res.json().catch(() => ({} as Record<string, unknown>));
      if (res.ok && data?.data) {
        const capability = data.data as CookieCapabilities;
        setCaps(capability);
        const recommended = String(capability?.browsers?.recommended || "").trim();
        if (recommended) setBrowser(recommended as Browser);
      }
    } catch {
      // ignore capability failures
    }
  }, []);

  useEffect(() => {
    loadCapabilities();
  }, [loadCapabilities]);

  useEffect(() => {
    setLoginGuide(null);
  }, [platform]);

  const handleExtract = async () => {
    setLoading(true);
    setMsg("");
    try {
      const res = await fetch("/api/webui/cookies/extract", {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify({ platform, browser, allow_close: allowClose }),
      });
      const data = await res.json().catch(() => ({} as Record<string, unknown>));
      if (res.ok) {
        setCookie(String(data.cookie || ""));
        setMsg(String(data.message || "提取成功"));
      } else {
        const hint = String(data.hint || "").trim();
        const reason =
          String(data.error || data.message || "").trim() ||
          res.statusText ||
          `HTTP ${res.status}`;
        setMsg(`提取失败: ${hint ? `${reason}（${hint}）` : reason}`);
      }
    } catch (err: any) {
      setMsg(`错误: ${err.message}`);
    } finally {
      setLoading(false);
    }
  };

  const handlePrepareLogin = async () => {
    setOpeningLogin(true);
    setMsg("");
    try {
      const res = await fetch("/api/webui/cookies/prepare-login", {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify({ platform, browser }),
      });
      const data = await res.json().catch(() => ({} as Record<string, unknown>));
      if (!res.ok || !data?.ok) {
        setMsg(String(data?.message || "Failed to open login page"));
        return;
      }
      setLoginGuide({
        message: String(data.message || ""),
        login_url: String(data.login_url || ""),
        after_login_url: String(data.after_login_url || ""),
        instructions: Array.isArray(data.instructions) ? data.instructions.map((item: unknown) => String(item)) : [],
        notes: Array.isArray(data.notes) ? data.notes.map((item: unknown) => String(item)) : [],
      });
      setMsg(String(data.message || "Opened login page"));
    } catch (err: any) {
      setMsg(`Error: ${err.message}`);
    } finally {
      setOpeningLogin(false);
    }
  };

  const handleSave = async () => {
    setLoading(true);
    setMsg("");
    try {
      const res = await fetch("/api/webui/cookies/save", {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify({ platform, cookie }),
      });
      const data = await res.json().catch(() => ({} as Record<string, unknown>));
      if (res.ok) {
        setMsg(String(data.message || "保存成功"));
      } else {
        const reason =
          String(data.error || data.message || "").trim() ||
          res.statusText ||
          `HTTP ${res.status}`;
        setMsg(`保存失败: ${reason}`);
      }
    } catch (err: any) {
      setMsg(`错误: ${err.message}`);
    } finally {
      setLoading(false);
    }
  };

  const startBilibiliQr = useCallback(async () => {
    setBiliQrLoading(true);
    setBiliQrStatus("正在生成二维码...");
    try {
      if (biliQrSessionId) {
        await fetch("/api/webui/cookies/bilibili-qr/cancel", {
          method: "POST",
          headers: jsonHeaders(),
          body: JSON.stringify({ session_id: biliQrSessionId }),
        }).catch(() => undefined);
      }
      const res = await fetch("/api/webui/cookies/bilibili-qr/start", {
        method: "POST",
        headers: jsonHeaders(),
      });
      const data = await res.json().catch(() => ({} as Record<string, unknown>));
      if (!res.ok || !data?.ok) {
        setBiliQrStatus(String(data?.message || "二维码生成失败"));
        setBiliQrLoading(false);
        return;
      }
      setBiliQrSessionId(String(data.session_id || ""));
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
        const res = await fetch(`/api/webui/cookies/bilibili-qr/status?session_id=${encodeURIComponent(biliQrSessionId)}`);
        const data = await res.json().catch(() => ({} as Record<string, unknown>));
        const status = String(data?.status || "");
        if (status === "done" && data?.data) {
          const sess = String((data.data as Record<string, unknown>).sessdata || "");
          const jct = String((data.data as Record<string, unknown>).bili_jct || "");
          setCookie(JSON.stringify({ SESSDATA: sess, bili_jct: jct }, null, 2));
          setBiliQrStatus("扫码登录成功，已回填 Cookie");
          setMsg("B站扫码登录成功");
          setBiliQrSessionId("");
          return;
        }
        if (status === "expired" || status === "error") {
          setBiliQrStatus(String(data?.message || "二维码已失效"));
          setBiliQrSessionId("");
          return;
        }
        setBiliQrStatus(String(data?.message || "等待扫码..."));
      } catch {
        // ignore poll errors
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
    fetch("/api/webui/cookies/bilibili-qr/cancel", {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ session_id: biliQrSessionId }),
    }).catch(() => undefined);
  }, [biliQrSessionId]);

  return (
    <div className="space-y-4 max-w-4xl">
      <div className="flex items-center gap-2">
        <h2 className="text-xl font-bold">Cookie 管理</h2>
        <Chip size="sm" variant="flat" color="warning">实验性功能</Chip>
      </div>

      <Card className="border border-default-400/35 bg-content1/35 backdrop-blur-sm">
        <CardHeader>
          <h3 className="text-lg font-semibold">平台选择</h3>
        </CardHeader>
        <CardBody className="space-y-4">
          <Select
            label="选择平台"
            selectedKeys={[platform]}
            onChange={(e) => setPlatform(e.target.value as Platform)}
          >
            <SelectItem key="bilibili">Bilibili (QR + browser extract)</SelectItem>
            <SelectItem key="douyin">Douyin (scan-login + extract)</SelectItem>
            <SelectItem key="kuaishou">Kuaishou (scan-login + extract)</SelectItem>
            <SelectItem key="qzone">QZone (scan-login + extract)</SelectItem>
          </Select>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <Select
              label="浏览器"
              selectedKeys={[browser]}
              onChange={(e) => setBrowser(e.target.value as Browser)}
            >
              <SelectItem key="edge">Edge</SelectItem>
              <SelectItem key="chrome">Chrome</SelectItem>
              <SelectItem key="brave">Brave</SelectItem>
              <SelectItem key="chromium">Chromium</SelectItem>
              <SelectItem key="firefox">Firefox</SelectItem>
            </Select>
            <div className="flex items-end pb-2">
              <Switch size="sm" isSelected={allowClose} onValueChange={setAllowClose}>
                自动关闭浏览器后重试
              </Switch>
            </div>
          </div>

          {platform === "bilibili" && (
            <div className="flex flex-wrap gap-2">
              <Button
                color="secondary"
                startContent={<QrCode size={16} />}
                isLoading={biliQrLoading}
                onPress={startBilibiliQr}
              >
                生成扫码二维码
              </Button>
              <Button
                color="primary"
                startContent={<RefreshCw size={16} />}
                isLoading={loading}
                onPress={handleExtract}
              >
                浏览器提取
              </Button>
            </div>
          )}

          {platform !== "bilibili" && (
            <div className="flex gap-2">
              <Button
                color="secondary"
                startContent={<QrCode size={16} />}
                isLoading={openingLogin}
                onPress={handlePrepareLogin}
              >
                Open Scan Login
              </Button>
              <Button
                color="primary"
                startContent={<RefreshCw size={16} />}
                isLoading={loading}
                onPress={handleExtract}
              >
                Extract Cookie
              </Button>
            </div>
          )}

          {biliQrStatus && platform === "bilibili" && (
            <Chip size="sm" variant="flat" color={biliQrSessionId ? "warning" : (biliQrStatus.includes("成功") ? "success" : "default")}>
              {biliQrStatus}
            </Chip>
          )}
          {biliQrImage && biliQrSessionId && platform === "bilibili" && (
            <div className="rounded-lg border border-default-200 bg-content1 p-3 w-fit">
              <img src={biliQrImage} alt="B站二维码" className="w-44 h-44" />
              {biliQrUrl && (
                <a
                  className="mt-2 block text-xs text-primary underline break-all"
                  href={biliQrUrl}
                  target="_blank"
                  rel="noreferrer"
                >
                  二维码链接（图片无法显示时使用）
                </a>
              )}
            </div>
          )}

          {caps?.notices?.map((note, idx) => (
            <Chip key={`${idx}-${note}`} size="sm" variant="flat" color="warning">
              {note}
            </Chip>
          ))}

          {platform !== "bilibili" && (
            <div className="space-y-1 text-sm text-default-500">
              <p>{loginGuide?.message || "Open the official login page, finish scan login in the same browser, then extract cookies."}</p>
              {loginGuide?.login_url ? (
                <a className="block text-xs text-primary underline break-all" href={loginGuide.login_url} target="_blank" rel="noreferrer">
                  {loginGuide.login_url}
                </a>
              ) : null}
              {loginGuide?.after_login_url ? (
                <p className="text-xs">After login, confirm the page has reached {loginGuide.after_login_url}.</p>
              ) : null}
              {(loginGuide?.instructions || []).map((line, idx) => (
                <p key={`guide-${idx}`} className="text-xs">{idx + 1}. {line}</p>
              ))}
              {(loginGuide?.notes || []).map((line, idx) => (
                <p key={`note-${idx}`} className="text-xs">Note: {line}</p>
              ))}
            </div>
          )}
          {platform === "bilibili" && (
            <div className="text-sm text-default-500">
              You can scan-login to refill `SESSDATA / bili_jct` directly, or extract them from a browser that is already signed in.
            </div>
          )}

        </CardBody>
      </Card>

      <Card className="border border-default-400/35 bg-content1/35 backdrop-blur-sm">
        <CardHeader>
          <h3 className="text-lg font-semibold">Cookie 内容</h3>
        </CardHeader>
        <CardBody className="space-y-4">
          <Textarea
            label="Cookie 字符串"
            placeholder="提取或手动粘贴 Cookie"
            value={cookie}
            onValueChange={setCookie}
            minRows={8}
            maxRows={16}
          />

          <div className="flex gap-2">
            <Button
              color="success"
              startContent={<Save size={16} />}
              isLoading={loading}
              onPress={handleSave}
              isDisabled={!cookie.trim()}
            >
              保存到配置
            </Button>
          </div>
        </CardBody>
      </Card>

      {msg && (
        <Card className={`border ${msg.includes("成功") ? "border-success" : "border-danger"}`}>
          <CardBody>
            <div className="flex items-center gap-2">
              {msg.includes("成功") ? (
                <CheckCircle2 size={20} className="text-success" />
              ) : (
                <AlertCircle size={20} className="text-danger" />
              )}
              <span>{msg}</span>
            </div>
          </CardBody>
        </Card>
      )}
    </div>
  );
}
