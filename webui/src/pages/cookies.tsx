import { useState } from "react";
import { Card, CardBody, CardHeader, Button, Textarea, Select, SelectItem, Chip } from "@heroui/react";
import { RefreshCw, Save, AlertCircle, CheckCircle2 } from "lucide-react";
import { api } from "../api/client";

type Platform = "bilibili" | "douyin" | "kuaishou";

export default function CookiesPage() {
  const [platform, setPlatform] = useState<Platform>("bilibili");
  const [cookie, setCookie] = useState("");
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState("");

  const handleExtract = async () => {
    setLoading(true);
    setMsg("");
    try {
      const res = await fetch("/api/webui/cookies/extract", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${api.getToken()}`,
        },
        body: JSON.stringify({ platform }),
      });
      if (res.ok) {
        const data = await res.json();
        setCookie(data.cookie || "");
        setMsg(data.message || "提取成功");
      } else {
        setMsg(`提取失败: ${res.statusText}`);
      }
    } catch (err: any) {
      setMsg(`错误: ${err.message}`);
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
    setLoading(true);
    setMsg("");
    try {
      const res = await fetch("/api/webui/cookies/save", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${api.getToken()}`,
        },
        body: JSON.stringify({ platform, cookie }),
      });
      if (res.ok) {
        setMsg("保存成功");
      } else {
        setMsg(`保存失败: ${res.statusText}`);
      }
    } catch (err: any) {
      setMsg(`错误: ${err.message}`);
    } finally {
      setLoading(false);
    }
  };

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
            <SelectItem key="bilibili">B站 (扫码登录)</SelectItem>
            <SelectItem key="douyin">抖音 (浏览器提取)</SelectItem>
            <SelectItem key="kuaishou">快手 (浏览器提取)</SelectItem>
          </Select>

          <div className="flex gap-2">
            <Button
              color="primary"
              startContent={<RefreshCw size={16} />}
              isLoading={loading}
              onPress={handleExtract}
            >
              {platform === "bilibili" ? "扫码登录" : "提取 Cookie"}
            </Button>
          </div>

          {platform === "bilibili" && (
            <div className="text-sm text-default-500">
              点击后将在终端显示二维码，使用 B站 App 扫码登录
            </div>
          )}
          {platform !== "bilibili" && (
            <div className="text-sm text-default-500">
              需要先在浏览器登录对应平台，然后点击提取。Chrome 需要关闭浏览器后重新打开。
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
